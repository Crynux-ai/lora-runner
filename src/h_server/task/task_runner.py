import logging
import os.path
import shutil
import time
from abc import ABC, abstractmethod
from collections import deque
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Deque, List, Optional, Tuple

from anyio import (Condition, create_task_group, fail_after,
                   get_cancelled_exc_class, to_thread)
from anyio.abc import TaskGroup
from celery.result import AsyncResult
from hexbytes import HexBytes
from tenacity import (before_sleep_log, retry, retry_if_exception,
                      stop_after_delay, wait_chain, wait_fixed)
from web3.types import EventData

from h_server import models
from h_server.config import TaskConfig as LocalConfig
from h_server.config import get_config
from h_server.contracts import (Contracts, TxRevertedError, TxWaiter,
                                get_contracts)
from h_server.event_queue import EventQueue, get_event_queue
from h_server.relay import Relay, RelayError, get_relay
from h_server.watcher import EventWatcher, get_watcher
from h_worker.task.error import TaskInvalid

from .state_cache import TaskStateCache, get_task_state_cache
from .utils import make_result_commitments

_logger = logging.getLogger(__name__)


OkCallback = Callable[[bool], Awaitable[None]]
ErrCallback = Callable[[Exception], Awaitable[None]]


class TaskRunner(ABC):
    @abstractmethod
    def __init__(
        self,
        task_id: int,
        task_name: str,
        distributed: bool,
        state_cache: Optional[TaskStateCache] = None,
        queue: Optional[EventQueue] = None,
    ):
        self.task_id = task_id
        self.task_name = task_name
        self.distributed = distributed
        if state_cache is None:
            state_cache = get_task_state_cache()
        self.cache = state_cache
        if queue is None:
            queue = get_event_queue()
        self.queue = queue

        self._state: Optional[models.TaskState] = None

        self._queue_condition = Condition()
        self._deque: Deque[Tuple[int, models.TaskEvent]] = deque()

        self._state_condition = Condition()

    @property
    def state(self) -> models.TaskState:
        assert self._state is not None, "The task runner's state has not been set."
        return self._state

    @state.setter
    def state(self, state: models.TaskState):
        assert self._state is None, "The task runner's state has already been set."
        self._state = state

    @state.deleter
    def state(self):
        assert self._state is not None, "The task runner's state has not been set."
        self._state = None

    @asynccontextmanager
    async def state_context(self):
        try:
            yield
        finally:
            async with self._state_condition:
                with fail_after(10, shield=True):
                    await self.cache.dump(task_state=self.state)
                self._state_condition.notify_all()

    async def wait_for_status(self, status: models.TaskStatus):
        async with self._state_condition:
            while self.state.status != status:
                await self._state_condition.wait()

    async def init(self) -> bool:
        need_dump = False
        try:
            if self._state is None:
                if await self.cache.has(self.task_id):
                    state = await self.cache.load(self.task_id)
                    self.state = state
                else:
                    state = models.TaskState(
                        task_id=self.task_id,
                        round=0,
                        timeout=0,
                        status=models.TaskStatus.Pending,
                    )
                    self.state = state
                    need_dump = True
            # check if the task has successed or aborted
            if self.state.status in [
                models.TaskStatus.Success,
                models.TaskStatus.Aborted,
            ]:
                return False
            # check if the task exists on chain
            task = await self.get_task()
            if task is None:
                # task doesn't exist on chain, abort
                self.state.status = models.TaskStatus.Aborted
                need_dump = True
                return False
            if self.state.timeout != task.timeout:
                self.state.timeout = task.timeout
                need_dump = True
            return True
        finally:
            if self._state is not None and need_dump:
                await self.cache.dump(self.state)

    async def process_event(self, event: models.TaskEvent):
        _logger.debug(f"Process event {event}")
        if event.kind == "TaskCreated":
            assert isinstance(event, models.TaskCreated)
            await self.task_created(event)
            return False
        elif event.kind == "TaskResultReady":
            assert isinstance(event, models.TaskResultReady)
            await self.result_ready(event)
            return False
        elif event.kind == "TaskResultCommitmentsReady":
            assert isinstance(event, models.TaskResultCommitmentsReady)
            await self.commitment_ready(event)
            return False
        elif event.kind == "TaskSuccess":
            assert isinstance(event, models.TaskSuccess)
            await self.task_success(event)
            return True
        if event.kind == "TaskAborted":
            assert isinstance(event, models.TaskAborted)
            await self.task_aborted(event)
            return True
        else:
            raise ValueError(f"Unknown event kind {event.kind}")

    @abstractmethod
    async def task_created(self, event: models.TaskCreated):
        ...

    @abstractmethod
    async def result_ready(self, event: models.TaskResultReady):
        ...

    @abstractmethod
    async def commitment_ready(self, event: models.TaskResultCommitmentsReady):
        ...

    @abstractmethod
    async def task_success(self, event: models.TaskSuccess):
        ...

    @abstractmethod
    async def task_aborted(self, event: models.TaskAborted):
        ...

    @abstractmethod
    async def cleanup(self):
        ...

    @abstractmethod
    async def get_task(self) -> Optional[models.ChainTask]:
        ...

    @abstractmethod
    async def cancel_task(self):
        ...

    async def _run_event(self, ack_id: int, event: models.TaskEvent, tg: TaskGroup):
        try:
            finished = await self.process_event(event)
            await self.queue.ack(ack_id)
            if finished:
                tg.cancel_scope.cancel()
        except get_cancelled_exc_class():
            _logger.debug(f"Task {self.task_id} process event {event.kind} cancelled.")
            self._deque.append((ack_id, event))
            raise
        except Exception:
            _logger.debug(f"Task {self.task_id} process event {event.kind} failed.")
            self._deque.append((ack_id, event))
            raise

    async def run(self):
        try:
            success = await self.init()
            if not success:
                return
            delay = self.state.timeout - time.time()
            if delay <= 0:
                raise TimeoutError
            with fail_after(delay, shield=False):
                async with create_task_group() as tg:
                    while True:
                        ack_id, event = await self.recv()

                        tg.start_soon(self._run_event, ack_id, event, tg)
        except get_cancelled_exc_class():
            raise
        except TimeoutError:
            # cancel task
            async with self.state_context():
                self.state.status = models.TaskStatus.Aborted
            await self.cancel_task()
        finally:
            with fail_after(10, shield=True):
                if self._state is not None and (
                    self.state.status == models.TaskStatus.Aborted
                    or self.state.status == models.TaskStatus.Success
                ):
                    for ack_id, event in self._deque:
                        await self.queue.ack(ack_id)
                        _logger.debug(f"Ack task {self.task_id} event {event.kind}")
                    await self.cleanup()
                else:
                    for ack_id, event in self._deque:
                        await self.queue.no_ack(ack_id)
                        _logger.debug(f"No ack task {self.task_id} event {event.kind}")

    async def recv(self) -> Tuple[int, models.TaskEvent]:
        async with self._queue_condition:
            while len(self._deque) == 0:
                await self._queue_condition.wait()
            ack_id, event = self._deque.popleft()
            return ack_id, event

    async def send(self, ack_id: int, event: models.TaskEvent):
        async with self._queue_condition:
            self._deque.append((ack_id, event))
            self._queue_condition.notify(1)


class InferenceTaskRunner(TaskRunner):
    def __init__(
        self,
        task_id: int,
        task_name: str,
        distributed: bool,
        state_cache: Optional[TaskStateCache] = None,
        queue: Optional[EventQueue] = None,
        contracts: Optional[Contracts] = None,
        relay: Optional[Relay] = None,
        watcher: Optional[EventWatcher] = None,
        local_config: Optional[LocalConfig] = None,
    ) -> None:
        super().__init__(
            task_id=task_id,
            task_name=task_name,
            distributed=distributed,
            state_cache=state_cache,
            queue=queue,
        )
        if contracts is None:
            self.contracts = get_contracts()
        else:
            self.contracts = contracts
        if relay is None:
            self.relay = get_relay()
        else:
            self.relay = relay
        if watcher is None:
            self.watcher = get_watcher()
        else:
            self.watcher = watcher

        if not self.distributed:
            # load task local config only in non-distributed mode
            if local_config is None:
                config = get_config()
                assert (
                    config.task_config is not None
                ), "Default task local config not found in config."
                self.local_config = config.task_config
            else:
                self.local_config = local_config
        else:
            self.local_config = None

        self._cleaned = False

        async def _push_event(event_data: EventData):
            event = models.load_event_from_contracts(event_data)
            await self.queue.put(event)

        self._commitment_watch_id = self.watcher.watch_event(
            "task",
            "TaskResultCommitmentsReady",
            callback=_push_event,
            filter_args={"taskId": self.task_id},
        )
        self._success_watch_id = self.watcher.watch_event(
            "task",
            "TaskSuccess",
            callback=_push_event,
            filter_args={"taskId": self.task_id},
        )
        self._aborted_watch_id = self.watcher.watch_event(
            "task",
            "TaskAborted",
            callback=_push_event,
            filter_args={"taskId": self.task_id},
        )

    async def _call_task_contract_method(self, method: str, *args, **kwargs):
        if (
            len(self.state.waiting_tx_method) == 0
            and len(self.state.waiting_tx_hash) == 0
        ):
            if method == "submitTaskResultCommitment":
                func = self.contracts.task_contract.submit_task_result_commitment
            elif method == "discloseTaskResult":
                func = self.contracts.task_contract.disclose_task_result
            elif method == "reportResultsUploaded":
                func = self.contracts.task_contract.report_results_uploaded
            elif method == "reportTaskError":
                func = self.contracts.task_contract.report_task_error
            else:
                raise ValueError(f"Unsupported task contract method: {method}")
            waiter = await func(*args, **kwargs)
        elif (
            self.state.waiting_tx_method == method
            and len(self.state.waiting_tx_hash) > 0
        ):
            waiter = TxWaiter(
                w3=self.contracts.w3,
                method=self.state.waiting_tx_method,
                tx_hash=HexBytes(self.state.waiting_tx_hash),
            )
        else:
            raise ValueError(
                f"Error state waiting tx method: {self.state.waiting_tx_method}, "
                f"waiting tx hash: {self.state.waiting_tx_hash} in report error"
            )

        await waiter.wait()
        async with self.state_context():
            self.state.waiting_tx_hash = b""
            self.state.waiting_tx_method = ""

    async def _report_error(self):
        async with self.state_context():
            self.state.status = models.TaskStatus.Aborted

        try:
            await self._call_task_contract_method(
                "reportTaskError", task_id=self.task_id, round=self.state.round
            )
        except TxRevertedError as e:
            _logger.error(
                f"Report error of task {self.task_id} failed due to {e.reason}"
            )

    async def get_task(self):
        task = await self.contracts.task_contract.get_task(self.task_id)
        # task not exist
        if task.id == 0 or task.id != self.task_id:
            return None
        return task

    async def cancel_task(self):
        try:
            await self.contracts.task_contract.cancel_task(self.task_id)
            _logger.info(f"Task {self.task_id} timeout. Cancel the task.")
        except TxRevertedError as e:
            _logger.error(f"Cancel task {self.task_id} failed due to {e.reason}")
        except get_cancelled_exc_class():
            raise
        except Exception as e:
            _logger.debug(f"Cancel task {self.task_id} failed")

    async def task_created(self, event: models.TaskCreated):
        await self.wait_for_status(models.TaskStatus.Pending)

        async with self.state_context():
            self.state.round = event.round

        def should_retry(e: BaseException) -> bool:
            if isinstance(e, RelayError) and (
                "Task not found" in e.message or "Task not ready" in e.message
            ):
                return True
            return False

        @retry(
            stop=stop_after_delay(1800),
            wait=wait_chain(*[wait_fixed(1) for _ in range(30)] + [wait_fixed(10)]),
            retry=retry_if_exception(should_retry),
            before_sleep=before_sleep_log(_logger, logging.ERROR, exc_info=True),
            reraise=True,
        )
        async def get_task():
            return await self.relay.get_task(event.task_id)

        task = await get_task()

        if self.distributed:

            def run_distributed_task():
                from h_server.celery_app import get_celery

                celery = get_celery()
                kwargs = {
                    "task_id": task.task_id,
                    "task_args": task.task_args,
                    "distributed": True,
                }
                res: AsyncResult = celery.send_task(
                    self.task_name,
                    kwargs=kwargs,
                )
                res.get()

            await to_thread.run_sync(run_distributed_task, cancellable=True)
            async with self.state_context():
                self.state.status = models.TaskStatus.Executing

        else:

            def run_local_task():
                import h_worker.task as h_task
                from h_worker.task.utils import get_image_hash

                assert self.local_config is not None
                proxy = None
                if self.local_config.proxy is not None:
                    proxy = self.local_config.proxy.model_dump()

                task_func = getattr(h_task, self.task_name)
                kwargs = dict(
                    task_id=task.task_id,
                    task_args=task.task_args,
                    distributed=False,
                    result_url="",
                    output_dir=self.local_config.output_dir,
                    hf_cache_dir=self.local_config.hf_cache_dir,
                    external_cache_dir=self.local_config.external_cache_dir,
                    script_dir=self.local_config.script_dir,
                    inference_logs_dir=self.local_config.inference_logs_dir,
                    proxy=proxy,
                )

                task_func(**kwargs)

                image_dir = os.path.join(
                    self.local_config.output_dir, str(task.task_id)
                )
                image_files = sorted(os.listdir(image_dir))
                image_paths = [os.path.join(image_dir, file) for file in image_files]
                hashes = [get_image_hash(path) for path in image_paths]
                return models.TaskResultReady(
                    task_id=self.task_id,
                    hashes=hashes,
                    files=image_paths,
                )

            try:
                next_event = await to_thread.run_sync(run_local_task, cancellable=True)
                async with self.state_context():
                    self.state.status = models.TaskStatus.Executing
                await self.queue.put(next_event)
            except TaskInvalid as e:
                _logger.exception(e)
                _logger.error("Task error, report error to the chain.")
                with fail_after(delay=60, shield=True):
                    await self._report_error()
                return True

    async def result_ready(self, event: models.TaskResultReady):
        await self.wait_for_status(models.TaskStatus.Executing)

        async with self.state_context():
            if len(self.state.result) == 0:
                result, commitment, nonce = make_result_commitments(event.hashes)
                try:
                    await self._call_task_contract_method(
                        "submitTaskResultCommitment",
                        task_id=self.task_id,
                        round=self.state.round,
                        commitment=commitment,
                        nonce=nonce,
                    )
                except TxRevertedError as e:
                    # all other nodes report error
                    if "Task is aborted" in e.reason:
                        await self._report_error()
                        return
                self.state.result = result
            _logger.info(f"Task {self.task_id} result 0x{self.state.result.hex()}")
            self.state.status = models.TaskStatus.ResultUploaded
            self.state.files = event.files

    async def commitment_ready(self, event: models.TaskResultCommitmentsReady):
        await self.wait_for_status(models.TaskStatus.ResultUploaded)

        async with self.state_context():
            assert (
                len(self.state.result) > 0
            ), "Task result not found when receive event TaskResultCommitmentsReady."
            if not self.state.disclosed:
                await self._call_task_contract_method(
                    "discloseTaskResult",
                    task_id=self.task_id,
                    round=self.state.round,
                    result=self.state.result,
                )
                self.state.disclosed = True
            self.state.status = models.TaskStatus.Disclosed

    async def task_success(self, event: models.TaskSuccess):
        await self.wait_for_status(models.TaskStatus.Disclosed)

        async with self.state_context():
            if event.result_node == self.contracts.account:
                await self.relay.upload_task_result(self.task_id, self.state.files)
                await self._call_task_contract_method(
                    "reportResultsUploaded",
                    task_id=self.task_id,
                    round=self.state.round,
                )

            self.state.status = models.TaskStatus.Success

    async def task_aborted(self, event: models.TaskAborted):
        async with self.state_context():
            self.state.status = models.TaskStatus.Aborted

    async def cleanup(self):
        if not self._cleaned:
            self.watcher.unwatch_event(self._commitment_watch_id)
            self.watcher.unwatch_event(self._success_watch_id)
            self.watcher.unwatch_event(self._aborted_watch_id)

            def delete_result_files(files: List[str]):
                if len(files) > 0:
                    dirname = os.path.dirname(files[0])
                    if os.path.exists(dirname):
                        shutil.rmtree(dirname)

            with fail_after(10, shield=True):
                await to_thread.run_sync(delete_result_files, self.state.files)

            del self.state
            self._cleaned = True


class MockTaskRunner(TaskRunner):
    def __init__(
        self,
        task_id: int,
        task_name: str,
        distributed: bool,
        state_cache: Optional[TaskStateCache] = None,
        queue: Optional[EventQueue] = None,
        timeout: int = 900,
    ):
        super().__init__(
            task_id=task_id,
            task_name=task_name,
            distributed=distributed,
            state_cache=state_cache,
            queue=queue,
        )

        self._timeout = timeout

    async def get_task(self):
        return models.ChainTask(
            id=self.task_id,
            creator="",
            task_hash=b"",
            data_hash=b"",
            is_success=False,
            selected_nodes=[],
            commitments=[],
            nonces=[],
            results=[],
            result_disclosed_rounds=[],
            result_node="",
            aborted=False,
            timeout=self._timeout + int(time.time()),
        )

    async def cancel_task(self):
        pass

    async def task_created(self, event: models.TaskCreated):
        await self.wait_for_status(models.TaskStatus.Pending)

        async with self.state_context():
            self.state.round = event.round
            self.state.status = models.TaskStatus.Executing

    async def result_ready(self, event: models.TaskResultReady):
        await self.wait_for_status(models.TaskStatus.Executing)

        async with self.state_context():
            self.state.files = event.files
            self.state.result = b"".join([bytes.fromhex(h[2:]) for h in event.hashes])
            self.state.status = models.TaskStatus.ResultUploaded

    async def commitment_ready(self, event: models.TaskResultCommitmentsReady):
        await self.wait_for_status(models.TaskStatus.ResultUploaded)

        async with self.state_context():
            self.state.status = models.TaskStatus.Disclosed
            self.state.disclosed = True

    async def task_success(self, event: models.TaskSuccess):
        await self.wait_for_status(models.TaskStatus.Disclosed)

        async with self.state_context():
            self.state.status = models.TaskStatus.Success

    async def task_aborted(self, event: models.TaskAborted):
        async with self.state_context():
            self.state.status = models.TaskStatus.Aborted

    async def cleanup(self):
        del self.state
