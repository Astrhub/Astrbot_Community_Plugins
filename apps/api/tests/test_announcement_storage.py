from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.object_storage.models import (
    StorageBackend,
    StorageBinding,
    StorageProfile,
    StoragePurpose,
    utc_now_iso,
)
from app.object_storage.manager import StorageManager
from app.object_storage.repository import create_object_storage_repository
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


def test_existing_published_profile_backfills_avatar_when_artifacts_disabled(
    tmp_path: Path,
) -> None:
    store = LocalFileMarketStore(tmp_path / "market-state.json")
    store.connect()
    base_settings = load_settings()
    settings = base_settings.with_updates(
        artifacts=replace(base_settings.artifacts, enabled=False),
    )

    async def verify_backfill() -> None:
        repository = create_object_storage_repository(store)
        await repository.upsert_profile(
            StorageProfile(
                id="existing-published",
                name="Existing published",
                backend=StorageBackend.LOCAL,
                config={"mode": "filesystem", "root": str(tmp_path / "objects")},
            )
        )
        await repository.upsert_binding(
            StorageBinding(
                purpose=StoragePurpose.ARTIFACT_PUBLISHED,
                profile_id="existing-published",
                generation=1,
                updated_by="test",
                updated_at=utc_now_iso(),
            )
        )
        manager = StorageManager(store=store, settings=settings)
        await manager.initialize()
        avatar = await repository.get_binding(StoragePurpose.AVATAR)
        assert avatar is not None
        assert avatar.profile_id == "existing-published"
        driver = manager.service.driver_for_purpose(StoragePurpose.AVATAR)
        assert driver.profile_id == "existing-published"
        await manager.close()

    try:
        asyncio.run(verify_backfill())
    finally:
        store.close()
