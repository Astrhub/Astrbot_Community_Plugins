from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from collections.abc import Sequence
from contextlib import suppress

from .config import RuntimeRunnerConfigurationError, load_runtime_runner_settings
from .execution import RuntimeExecutionError, build_runtime_execution_service
from .queue import RuntimeRunnerQueue
from .repository import PgRuntimeRunnerRepository
from .storage import LocalRuntimeResultWriter
from .worker import RuntimeRunnerWorker


async def run_runtime_runner() -> None:
    settings = load_runtime_runner_settings()
    repository = await PgRuntimeRunnerRepository.connect(settings.database_url)
    executor = None
    try:
        executor = build_runtime_execution_service(settings)
        queue = RuntimeRunnerQueue(repository, runner_id=settings.runner_id)
        writer = LocalRuntimeResultWriter(settings.result_root)
        worker = RuntimeRunnerWorker(
            queue=queue,
            result_writer=writer,
            executor=executor,
            settings=settings,
        )
        _install_signal_handlers(worker)
        await worker.run_forever()
    finally:
        if executor is not None:
            await executor.close()
        await repository.close()


def _install_signal_handlers(worker: RuntimeRunnerWorker) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, worker.request_stop)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime-runner")
    parser.add_argument("--check-config", action="store_true")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if arguments.check_config:
        try:
            settings = load_runtime_runner_settings()
        except RuntimeRunnerConfigurationError as exc:
            print(json.dumps({"configured": False, "errors": exc.errors}, ensure_ascii=True))
            return 2
        print(json.dumps(settings.public_summary(), ensure_ascii=True, sort_keys=True))
        return 0
    try:
        asyncio.run(run_runtime_runner())
    except (RuntimeRunnerConfigurationError, RuntimeExecutionError) as exc:
        logging.getLogger("astrbot.runtime_runner").error("Runtime runner unavailable: %s", exc)
        return 2
    return 0
