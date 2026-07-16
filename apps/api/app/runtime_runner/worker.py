from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from ..artifacts.runner_contract import runtime_result_object_key
from .config import RuntimeRunnerSettings
from .execution import RuntimeExecutionError, RuntimeExecutionOutput, RuntimeExecutionService
from .queue import (
    RunnerTerminalStatus,
    RuntimeDispatchWorkItem,
    RuntimeRunnerQueue,
    RuntimeRunnerQueueError,
)
from .storage import RuntimeResultStorageError, RuntimeResultWriter

logger = logging.getLogger("astrbot.runtime_runner")


class _LeaseLost(RuntimeError):
    pass


class RuntimeRunnerWorker:
    def __init__(
        self,
        *,
        queue: RuntimeRunnerQueue,
        result_writer: RuntimeResultWriter,
        executor: RuntimeExecutionService,
        settings: RuntimeRunnerSettings,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self.queue = queue
        self.result_writer = result_writer
        self.executor = executor
        self.settings = settings
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._stop = asyncio.Event()
        self._active: set[asyncio.Task[None]] = set()
        self._last_orphan_cleanup = 0.0

    def request_stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> int:
        await self._maybe_cleanup_orphans(force=self._last_orphan_cleanup == 0.0)
        work_items = await self.queue.claim(
            limit=self.settings.claim_limit,
            lease_seconds=self.settings.lease_seconds,
        )
        if not work_items:
            return 0
        await asyncio.gather(*(self._process_work(work) for work in work_items))
        return len(work_items)

    async def run_forever(self) -> None:
        await self._maybe_cleanup_orphans(force=True)
        try:
            while not self._stop.is_set():
                await self._reap_finished()
                await self._maybe_cleanup_orphans()
                capacity = self.settings.claim_limit - len(self._active)
                if capacity > 0:
                    try:
                        work_items = await self.queue.claim(
                            limit=capacity,
                            lease_seconds=self.settings.lease_seconds,
                        )
                    except Exception as exc:
                        logger.warning("Runtime dispatch claim failed: %s", type(exc).__name__)
                        work_items = ()
                    for work in work_items:
                        self._active.add(asyncio.create_task(self._process_work(work)))
                await self._wait_for_activity()
        finally:
            await self._drain_active()

    async def _process_work(self, work: RuntimeDispatchWorkItem) -> None:
        try:
            output = await self._execute_with_heartbeat(work)
            result = output.result
            content = result.model_dump_json().encode("utf-8")
            renewed = await self.queue.renew(
                work,
                lease_seconds=self.settings.lease_seconds,
            )
            if not renewed:
                raise _LeaseLost
            for private_object in output.private_objects:
                stored = await self.result_writer.put_result(
                    private_object.key,
                    private_object.content,
                    max_bytes=private_object.max_bytes,
                )
                if stored.sha256 != private_object.sha256:
                    raise RuntimeResultStorageError(
                        "runtime_result_sha256_mismatch",
                        "Runtime private object storage returned a different digest",
                    )
            result_key = runtime_result_object_key(
                work.request,
                work.attempt,
                result.result_sha256,
            )
            await self.result_writer.put_result(
                result_key,
                content,
                max_bytes=work.request.limits.max_result_bytes,
            )
            await self.queue.complete_result(work, result)
        except _LeaseLost:
            await self._abort_and_cleanup(work)
            logger.warning("Runtime dispatch lease lost: %s", work.dispatch_id)
        except TimeoutError:
            await self._abort_and_cleanup(work)
            await self._complete_failure(
                work,
                status=RunnerTerminalStatus.TIMED_OUT,
                error_code="runtime_execution_timed_out",
                error_message="Runtime execution exceeded its configured timeout",
            )
        except asyncio.CancelledError:
            await self._abort_and_cleanup(work)
            await self._complete_failure(
                work,
                status=RunnerTerminalStatus.CANCELLED,
                error_code="runtime_runner_shutdown",
                error_message="Runtime execution was cancelled during runner shutdown",
            )
        except RuntimeExecutionError as exc:
            await self._abort_and_cleanup(work)
            await self._complete_failure(
                work,
                status=RunnerTerminalStatus.FAILED,
                error_code=exc.code,
                error_message="Runtime execution failed in the isolated executor",
            )
        except RuntimeResultStorageError as exc:
            await self._abort_and_cleanup(work)
            await self._complete_failure(
                work,
                status=RunnerTerminalStatus.FAILED,
                error_code=exc.code,
                error_message=str(exc),
            )
        except RuntimeRunnerQueueError as exc:
            await self._abort_and_cleanup(work)
            logger.warning(
                "Runtime dispatch completion rejected: %s (%s)",
                work.dispatch_id,
                exc.code,
            )
        except Exception as exc:
            await self._abort_and_cleanup(work)
            logger.warning(
                "Runtime execution failed: %s (%s)",
                work.dispatch_id,
                type(exc).__name__,
            )
            await self._complete_failure(
                work,
                status=RunnerTerminalStatus.FAILED,
                error_code="runtime_execution_failed",
                error_message="Runtime execution failed unexpectedly",
            )

    async def _execute_with_heartbeat(
        self,
        work: RuntimeDispatchWorkItem,
    ) -> RuntimeExecutionOutput:
        execution = asyncio.create_task(self.executor.execute(work))
        heartbeat = asyncio.create_task(self._wait_for_lease_loss(work))
        try:
            done, _ = await asyncio.wait(
                {execution, heartbeat},
                timeout=work.request.limits.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                execution.cancel()
                with suppress(asyncio.CancelledError):
                    await execution
                raise TimeoutError
            if heartbeat in done:
                execution.cancel()
                with suppress(asyncio.CancelledError):
                    await execution
                raise _LeaseLost
            result = execution.result()
            return (
                result
                if isinstance(result, RuntimeExecutionOutput)
                else RuntimeExecutionOutput(result=result)
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _wait_for_lease_loss(self, work: RuntimeDispatchWorkItem) -> None:
        interval = self.heartbeat_interval_seconds or max(1.0, self.settings.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.queue.renew(
                    work,
                    lease_seconds=self.settings.lease_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "Runtime lease renewal failed: %s (%s)",
                    work.dispatch_id,
                    type(exc).__name__,
                )
                return
            if not renewed:
                return

    async def _complete_failure(
        self,
        work: RuntimeDispatchWorkItem,
        *,
        status: RunnerTerminalStatus,
        error_code: str,
        error_message: str,
    ) -> None:
        try:
            await self.queue.complete_failure(
                work,
                status=status,
                error_code=error_code,
                error_message=error_message,
            )
        except RuntimeRunnerQueueError:
            logger.warning("Runtime failure completion lost lease: %s", work.dispatch_id)
        except Exception as exc:
            logger.warning(
                "Runtime failure completion failed: %s (%s)",
                work.dispatch_id,
                type(exc).__name__,
            )

    async def _abort_and_cleanup(self, work: RuntimeDispatchWorkItem) -> None:
        for operation in (self.executor.abort, self.executor.cleanup_dispatch):
            try:
                await operation(work)
            except Exception as exc:
                logger.warning(
                    "Runtime resource cleanup failed: %s (%s)",
                    work.dispatch_id,
                    type(exc).__name__,
                )

    async def _maybe_cleanup_orphans(self, *, force: bool = False) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if not force and now - self._last_orphan_cleanup < self.settings.orphan_cleanup_seconds:
            return
        try:
            removed = await self.executor.cleanup_orphans()
            logger.info("Runtime orphan cleanup completed: removed=%d", removed)
        except Exception as exc:
            logger.warning("Runtime orphan cleanup failed: %s", type(exc).__name__)
        finally:
            self._last_orphan_cleanup = now

    async def _wait_for_activity(self) -> None:
        stop_waiter = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait(
                {*self._active, stop_waiter},
                timeout=self.settings.poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await stop_waiter

    async def _reap_finished(self) -> None:
        finished = {task for task in self._active if task.done()}
        self._active.difference_update(finished)
        if finished:
            await asyncio.gather(*finished, return_exceptions=True)

    async def _drain_active(self) -> None:
        if not self._active:
            return
        done, pending = await asyncio.wait(
            self._active,
            timeout=self.settings.shutdown_grace_seconds,
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._active.clear()
