from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.object_storage.manager import StorageManager
from app.object_storage.service import LEGACY_PUBLISHED_PROFILE_ID
from app.store import LocalFileMarketStore


def test_avatar_binding_bootstrap_survives_storage_manager_restart(tmp_path: Path) -> None:
    store = LocalFileMarketStore(tmp_path / "market-state.json")
    store.connect()
    base_settings = load_settings()
    settings = base_settings.with_updates(
        artifacts=replace(
            base_settings.artifacts,
            enabled=True,
            local_root=str(tmp_path / "objects"),
        )
    )

    async def verify_restart() -> None:
        first = StorageManager(store=store, settings=settings)
        await first.initialize()
        await first.close()

        second = StorageManager(store=store, settings=settings)
        await second.initialize()
        bindings = await second.list_bindings()
        avatar = next(item for item in bindings if item["purpose"] == "avatar")
        assert avatar["profile_id"] == LEGACY_PUBLISHED_PROFILE_ID
        await second.close()

    try:
        asyncio.run(verify_restart())
    finally:
        store.close()
