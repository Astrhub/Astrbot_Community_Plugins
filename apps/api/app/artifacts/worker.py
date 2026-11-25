from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from ..config import load_settings
from ..store import PgRedisMarketStore
from ..object_storage.manager import StorageManager
from .runtime import build_artifact_runtime

LOGGER = logging.getLogger(__name__)


async def run_worker() -> None:
    settings = load_settings()
    errors = settings.artifacts.validation_errors(settings.database_url)
    if not settings.artifacts.enabled:
        raise RuntimeError("ARTIFACTS_ENABLED must be true for the artifact worker")
    if errors:
        raise RuntimeError("Invalid artifact configuration: " + ", ".join(errors))
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is required for the current market store")

    store = PgRedisMarketStore(
        settings.database_url,
        settings.redis_url,
        session_ttl_seconds=settings.session_max_age_seconds,
    )
    await store.connect()
    storage_manager = StorageManager(store=store, settings=settings)
    runtime = None
    try:
        await storage_manager.initialize()
        runtime = build_artifact_runtime(
            settings,
            store,
            worker_execution_enabled=True,
            object_storage=storage_manager.service,
            storage_refresh=storage_manager.refresh,
        )
        await runtime.start(store)
        if not runtime.available or runtime.job_runner is None:
            raise RuntimeError(
                "Artifact runtime is unavailable: " + ", ".join(runtime.configuration_errors)
            )

        loop = asyncio.get_running_loop()
        for name in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(name, runtime.job_runner.stop)
        LOGGER.info("Artifact worker started")
        await runtime.job_runner.run_forever()
    finally:
        if runtime is not None:
            await runtime.close()
        await storage_manager.close()
        await store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
