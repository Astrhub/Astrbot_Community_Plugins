"""Regression tests covering all PR #77 review findings (P1-1..P1-5 and P2-1..P2-3)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.artifacts.storage import create_artifact_storage
from app.auth import hash_password
from app.config import load_settings
from app.main import create_app
from app.object_storage import (
    ImgBedObjectStorageDriver,
    StorageManager,
    StorageObjectRecord,
    StorageObjectStatus,
    StorageProfileStatus,
    StoragePurpose,
)
from app.object_storage.models import StorageMigrationStatus
from app.store import InMemoryMarketStore, LocalFileMarketStore


def _make_app(
    *,
    store: InMemoryMarketStore | None = None,
    env: dict[str, str] | None = None,
    imgbed_servers: dict[str, httpx.AsyncBaseTransport] | None = None,
) -> tuple[TestClient, Any]:
    active_store = store or InMemoryMarketStore()
    active_store.create_internal_admin("coreadmin", hash_password("corepass123"))
    base_env = {
        "OAUTH_CREDENTIAL_KEY": "review-fixes-master-key-123456",
        **(env or {}),
    }
    settings = load_settings(base_env)
    app = create_app(settings=settings, store=active_store)
    if imgbed_servers is not None:
        app.state.storage_manager._imgbed_client_factory = lambda profile, _cfg: httpx.AsyncClient(
            transport=imgbed_servers[profile.id]
        )
    client = TestClient(app, follow_redirects=False)
    login = client.post(
        "/v1/auth/internal/login",
        json={"username": "coreadmin", "password": "corepass123"},
    )
    assert login.status_code == 200
    return client, app


def test_p1_1_legacy_upgrade_backfills_existing_files_and_keeps_unmigrated_reads_working(
    tmp_path: Path,
) -> None:
    """P1-1: Pre-existing legacy disk files backfill on startup, stay readable before migration, and migrate cleanly."""
    legacy_root = tmp_path / "legacy-artifacts"
    q_file = legacy_root / "quarantine" / "artifacts" / "art-old" / "source.zip"
    c_file = legacy_root / "content" / "artifacts" / "art-old" / "files" / "f1.txt"
    p_file = legacy_root / "published" / "alice" / "old" / "1.0.0" / "old-1.0.0-11223344.zip"
    for path, content in (
        (q_file, b"PK\x03\x04LEGACY-QUARANTINE-BYTES"),
        (c_file, b"print('legacy scanned text')\n"),
        (p_file, b"PK\x03\x04LEGACY-PUBLISHED-ZIP-BYTES"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    client, app = _make_app(
        env={
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_STORAGE_BACKEND": "local",
            "ARTIFACT_LOCAL_ROOT": str(legacy_root),
            "ARTIFACT_CDN_BASE_URL": "https://plugins.eloina.cn/packages",
        }
    )
    manager: StorageManager = app.state.storage_manager

    # Trigger initialization (backfills all 3 legacy objects into market_storage_objects).
    profiles_res = client.get("/v1/admin/storage/profiles")
    assert profiles_res.status_code == 200

    async def verify_backfill() -> None:
        objs = await manager.repository.list_objects(status=StorageObjectStatus.ACTIVE)
        by_locator = {(o.purpose, o.namespace, o.key): o for o in objs}
        q_rec = by_locator[
            (StoragePurpose.ARTIFACT_QUARANTINE, "quarantine", "artifacts/art-old/source.zip")
        ]
        c_rec = by_locator[
            (StoragePurpose.ARTIFACT_QUARANTINE, "content", "artifacts/art-old/files/f1.txt")
        ]
        p_rec = by_locator[
            (
                StoragePurpose.ARTIFACT_PUBLISHED,
                "",
                "alice/old/1.0.0/old-1.0.0-11223344.zip",
            )
        ]
        assert q_rec.sha256 == hashlib.sha256(b"PK\x03\x04LEGACY-QUARANTINE-BYTES").hexdigest()
        assert c_rec.sha256 == hashlib.sha256(b"print('legacy scanned text')\n").hexdigest()
        assert p_rec.sha256 == hashlib.sha256(b"PK\x03\x04LEGACY-PUBLISHED-ZIP-BYTES").hexdigest()

    asyncio.run(verify_backfill())

    # Historical /packages URL is immediately readable even before switching profiles.
    hist_before = client.get("/packages/alice/old/1.0.0/old-1.0.0-11223344.zip")
    assert hist_before.status_code == 200
    assert hist_before.content == b"PK\x03\x04LEGACY-PUBLISHED-ZIP-BYTES"

    # Create target SQLite profiles for quarantine & published.
    for pid, db_name in (("sqlite-q", "q.sqlite3"), ("sqlite-pub", "pub.sqlite3")):
        res = client.post(
            "/v1/admin/storage/profiles",
            json={
                "id": pid,
                "name": pid,
                "backend": "local",
                "config": {"mode": "sqlite", "db_path": str(tmp_path / db_name)},
            },
        )
        assert res.status_code == 201, res.text

    # Switch quarantine WITHOUT running migration yet -> ArtifactStorage facade STILL reads unmigrated files from legacy!
    rebind_q = client.put(
        "/v1/admin/storage/bindings/artifact-quarantine",
        json={"profile_id": "sqlite-q", "run_immediately": False},
    )
    assert rebind_q.status_code == 200, rebind_q.text
    assert rebind_q.json()["migration"]["total_count"] == 2

    async def verify_unmigrated_facade_reads() -> None:
        facade = create_artifact_storage(
            app.state.settings.artifacts,
            object_storage=manager.service,
        )
        downloaded = tmp_path / "dl-quarantine.zip"
        await facade.download_quarantine("artifacts/art-old/source.zip", downloaded)
        assert downloaded.read_bytes() == b"PK\x03\x04LEGACY-QUARANTINE-BYTES"
        scanned = await facade.read_text_content("artifacts/art-old/files/f1.txt", max_bytes=1024)
        assert scanned == b"print('legacy scanned text')\n"

    asyncio.run(verify_unmigrated_facade_reads())

    # Complete quarantine migration, delete old files, and confirm facade reads from sqlite-q.
    mig_q_id = rebind_q.json()["migration"]["id"]
    mig_q_after = asyncio.run(manager.run_migration(mig_q_id))
    assert mig_q_after.status == StorageMigrationStatus.COMPLETED
    q_file.unlink()
    c_file.unlink()
    asyncio.run(verify_unmigrated_facade_reads())

    # Switch published with run_immediately=True -> delete old file -> /packages serves from sqlite-pub!
    rebind_pub = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "sqlite-pub", "run_immediately": True},
    )
    assert rebind_pub.status_code == 200, rebind_pub.text
    assert rebind_pub.json()["migration"]["status"] == "completed"
    p_file.unlink()
    hist_after = client.get("/packages/alice/old/1.0.0/old-1.0.0-11223344.zip")
    assert hist_after.status_code == 200
    assert hist_after.content == b"PK\x03\x04LEGACY-PUBLISHED-ZIP-BYTES"


def test_p1_2_and_p1_3_generation_check_cancels_stale_migration_and_blocks_target_retirement(
    tmp_path: Path,
) -> None:
    """P1-2 & P1-3: Rebinding A->B then B->C blocks retiring B while M1 is queued, cancels stale M1 without writing to B, and migrates A->C cleanly."""
    client, app = _make_app()
    manager: StorageManager = app.state.storage_manager

    for pid in ("prof-a", "prof-b", "prof-c"):
        res = client.post(
            "/v1/admin/storage/profiles",
            json={
                "id": pid,
                "name": pid,
                "backend": "local",
                "config": {"mode": "sqlite", "db_path": str(tmp_path / f"{pid}.sqlite3")},
            },
        )
        assert res.status_code == 201

    bind_a = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "prof-a"},
    )
    assert bind_a.status_code == 200

    pkg_key = "alice/gen/1.0.0/gen-1.0.0-11223344.zip"
    pkg_bytes = b"PK\x03\x04GENERATION-GUARD-TEST"

    async def seed_on_a() -> str:
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=pkg_key,
            data=pkg_bytes,
            content_type="application/zip",
        )
        rec = await manager.repository.find_active_object_by_purpose_and_key(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=pkg_key,
        )
        assert rec is not None
        return rec.id

    obj_id = asyncio.run(seed_on_a())

    # 1. Rebind A -> B (gen 2, M1 queued, not executed yet).
    rebind_b = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "prof-b", "run_immediately": False},
    )
    assert rebind_b.status_code == 200
    m1_id = rebind_b.json()["migration"]["id"]

    # 2. Before M1 runs, rebind B -> C (gen 3, M2 queued and picks up A's unmigrated object!).
    rebind_c = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "prof-c", "run_immediately": False},
    )
    assert rebind_c.status_code == 200
    m2_id = rebind_c.json()["migration"]["id"]

    # P1-3: B now has 0 bindings and 0 registered objects, BUT M1 still targets B -> retire & delete MUST fail (409)!
    retire_b_blocked = client.post("/v1/admin/storage/profiles/prof-b/retire")
    assert retire_b_blocked.status_code == 409
    assert retire_b_blocked.json()["code"] == "storage_profile_has_unfinished_migrations"
    delete_b_blocked = client.delete("/v1/admin/storage/profiles/prof-b")
    assert delete_b_blocked.status_code == 409
    assert delete_b_blocked.json()["code"] == "storage_profile_has_unfinished_migrations"

    # P1-2: Running stale M1 MUST detect gen mismatch, cancel M1, write nothing to B, and leave obj on A!
    m1_after = asyncio.run(manager.run_migration(m1_id))
    assert m1_after.status == StorageMigrationStatus.CANCELLED

    async def verify_b_untouched_and_obj_still_on_a() -> None:
        obj = await manager.repository.get_object(obj_id)
        assert obj is not None
        assert obj.profile_id == "prof-a"
        b_driver = manager.service.driver_for_profile("prof-b")
        assert await b_driver.list_objects() == []

    asyncio.run(verify_b_untouched_and_obj_still_on_a())

    # Now run M2 (gen 3): migrates obj straight from A -> C!
    m2_after = asyncio.run(manager.run_migration(m2_id))
    assert m2_after.status == StorageMigrationStatus.COMPLETED
    assert m2_after.completed_count == 1
    pkg_res = client.get(f"/packages/{pkg_key}")
    assert pkg_res.status_code == 200
    assert pkg_res.content == pkg_bytes

    # After M1 is cancelled and M2 is completed, retiring B now succeeds!
    retire_b_ok = client.post("/v1/admin/storage/profiles/prof-b/retire")
    assert retire_b_ok.status_code == 200
    assert retire_b_ok.json()["status"] == "retired"


def test_p1_4_pending_object_crash_recovery_and_retry_reuse(tmp_path: Path) -> None:
    """P1-4: Mid-write crash leaving pending rows neither blocks retry nor leaves unretirable orphans."""
    client, app = _make_app()
    manager: StorageManager = app.state.storage_manager

    res = client.post(
        "/v1/admin/storage/profiles",
        json={
            "id": "local-crash",
            "name": "Crash Test",
            "backend": "local",
            "config": {"mode": "filesystem", "root": str(tmp_path / "crash-fs")},
        },
    )
    assert res.status_code == 201
    bind = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "local-crash"},
    )
    assert bind.status_code == 200

    async def exercise_pending_recovery() -> None:
        # Case A: Simulated crash left a PENDING record with no backend file; retry reuses SAME record ID!
        pending_retry = StorageObjectRecord(
            id="stobj_pending_retry_1",
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            profile_id="local-crash",
            backend="local",
            namespace="",
            key="alice/retry/1.0.0/retry-1.0.0-aabbccdd.zip",
            size_bytes=0,
            sha256="",
            content_type="application/zip",
            status=StorageObjectStatus.PENDING,
            created_at="2020-01-01T00:00:00Z",
            updated_at="2020-01-01T00:00:00Z",
        )
        await manager.repository.upsert_object(pending_retry)

        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key="alice/retry/1.0.0/retry-1.0.0-aabbccdd.zip",
            data=b"RECOVERED-ON-RETRY",
            content_type="application/zip",
        )
        all_for_key = [
            o
            for o in await manager.repository.list_objects(profile_id="local-crash")
            if o.key == "alice/retry/1.0.0/retry-1.0.0-aabbccdd.zip"
        ]
        assert len(all_for_key) == 1
        assert all_for_key[0].id == "stobj_pending_retry_1"
        assert all_for_key[0].status == StorageObjectStatus.ACTIVE

        # Case B: Stale PENDING without backend file (orphan) + stale PENDING whose backend file already landed.
        orphan_pending = StorageObjectRecord(
            id="stobj_pending_orphan",
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            profile_id="local-crash",
            backend="local",
            namespace="",
            key="alice/orphan/1.0.0/orphan-1.0.0-11112222.zip",
            size_bytes=0,
            sha256="",
            content_type="application/zip",
            status=StorageObjectStatus.PENDING,
            created_at="2020-01-01T00:00:00Z",
            updated_at="2020-01-01T00:00:00Z",
        )
        await manager.repository.upsert_object(orphan_pending)

        already_written_key = "alice/written/1.0.0/written-1.0.0-33334444.zip"
        driver = manager.service.driver_for_profile("local-crash")
        await driver.put_object(already_written_key, b"ALREADY-ON-DISK", if_absent=True)
        written_pending = StorageObjectRecord(
            id="stobj_pending_written",
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            profile_id="local-crash",
            backend="local",
            namespace="",
            key=already_written_key,
            size_bytes=0,
            sha256="",
            content_type="application/zip",
            status=StorageObjectStatus.PENDING,
            created_at="2020-01-01T00:00:00Z",
            updated_at="2020-01-01T00:00:00Z",
        )
        await manager.repository.upsert_object(written_pending)

        # Simulate restart recovery.
        summary = await manager.recover_stale_pending_objects(max_age_seconds=60)
        assert summary == {"activated": 1, "deleted": 1}
        assert await manager.repository.get_object("stobj_pending_orphan") is None
        promoted = await manager.repository.get_object("stobj_pending_written")
        assert promoted is not None
        assert promoted.status == StorageObjectStatus.ACTIVE
        assert promoted.sha256 == hashlib.sha256(b"ALREADY-ON-DISK").hexdigest()

    asyncio.run(exercise_pending_recovery())


def test_p1_5_atomic_rebind_rolls_back_on_persistence_failure(tmp_path: Path) -> None:
    """P1-5: Persistence failure during rebind leaves old binding and active source profile untouched."""
    state_file = tmp_path / "atomic-state.json"
    store = LocalFileMarketStore(state_file, 3600)
    store.connect()
    client, app = _make_app(store=store)
    manager: StorageManager = app.state.storage_manager

    for pid in ("atomic-a", "atomic-b"):
        res = client.post(
            "/v1/admin/storage/profiles",
            json={
                "id": pid,
                "name": pid,
                "backend": "local",
                "config": {"mode": "filesystem", "root": str(tmp_path / pid)},
            },
        )
        assert res.status_code == 201

    bind_atomic = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "atomic-a"},
    )
    assert bind_atomic.status_code == 200

    async def seed_and_fail_rebind() -> None:
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key="alice/atomic/1.0.0/atomic-1.0.0-12345678.zip",
            data=b"ATOMIC-OBJECT",
            content_type="application/zip",
        )

        original_persist = store.persist_object_storage

        def failing_persist() -> None:
            raise OSError("Simulated disk full during rebind commit")

        store.persist_object_storage = failing_persist  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError, match="Simulated disk full"):
                await manager.bind_purpose("artifact-published", "atomic-b", actor_id="core-1")
        finally:
            store.persist_object_storage = original_persist  # type: ignore[method-assign]

        binding = await manager.repository.get_binding(StoragePurpose.ARTIFACT_PUBLISHED)
        assert binding is not None
        assert binding.profile_id == "atomic-a"
        assert binding.generation == 1
        assert (
            manager.service.get_binding(StoragePurpose.ARTIFACT_PUBLISHED).profile_id == "atomic-a"
        )  # type: ignore[union-attr]
        profile_a = await manager.repository.get_profile("atomic-a")
        assert profile_a is not None
        assert profile_a.status == StorageProfileStatus.ACTIVE
        assert await manager.repository.list_migrations() == []

    asyncio.run(seed_and_fail_rebind())


class _PaginatedImgBedTransport(httpx.AsyncBaseTransport):
    """ImgBed mock with >1000 objects, start/count pagination, FileSizeBytes + decimal MB FileSize."""

    def __init__(self, *, total_files: int, allow_delete: bool) -> None:
        self.allow_delete = allow_delete
        self.uploaded_files: list[str] = []
        self.list_requests: list[tuple[int, int]] = []
        self.records: list[dict[str, Any]] = []
        for idx in range(total_files):
            name = f"astrhub/big/file-{idx:04d}.zip"
            if idx == 0:
                meta: dict[str, Any] = {"FileSize": "0.50", "FileSizeBytes": 524288}
            elif idx == 1:
                meta = {"FileSize": "1.25"}  # Decimal MB string only (no FileSizeBytes)
            elif idx == 2:
                meta = {"FileSize": 4096}  # Integer byte compatibility fallback
            else:
                meta = {"FileSize": "0.01", "FileSizeBytes": 1024 + idx}
            self.records.append({"name": name, "metadata": meta})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        method = request.method.upper()
        if path == "/" and method == "HEAD":
            return httpx.Response(200, request=request)
        if path == "/upload" and method == "POST":
            self.uploaded_files.append(str(request.url))
            return httpx.Response(
                200, json=[{"src": "/file/astrhub/big/leaked.png"}], request=request
            )
        if path == "/api/manage/list" and method == "GET":
            start = int(request.url.params.get("start", "0"))
            count = int(request.url.params.get("count", "1000"))
            self.list_requests.append((start, count))
            page = self.records[start : start + count]
            return httpx.Response(
                200,
                json={
                    "files": page,
                    "totalCount": len(self.records),
                    "returnedCount": len(page),
                },
                request=request,
            )
        return httpx.Response(404, request=request)


def test_p2_1_and_p2_2_imgbed_pagination_size_parsing_and_zero_leak_probe() -> None:
    """P2-1 & P2-2: ImgBed paginates beyond 1,000 objects, parses FileSizeBytes & decimal MB, and skips mutating probe when delete scope is absent."""
    transport = _PaginatedImgBedTransport(total_files=1205, allow_delete=False)
    client = httpx.AsyncClient(transport=transport)
    driver = ImgBedObjectStorageDriver(
        profile_id="imgbed-big",
        base_url="https://imgbed.example.com",
        token="tok-list",
        upload_folder="astrhub/big",
        scopes=("upload", "list"),
        client=client,
    )

    async def verify_driver() -> None:
        try:
            listed = await driver.list_objects()
            assert len(listed) == 1205
            assert transport.list_requests == [(0, 1000), (1000, 1000)]
            by_key = {entry.key: entry.size_bytes for entry in listed}
            assert by_key["file-0000.zip"] == 524288
            assert by_key["file-0001.zip"] == round(1.25 * 1024 * 1024)
            assert by_key["file-0002.zip"] == 4096
            assert by_key["file-1204.zip"] == 1024 + 1204
        finally:
            await client.aclose()

    asyncio.run(verify_driver())

    # P2-2: Upload-only ImgBed profile health test leaks ZERO remote files across repeated tests.
    upload_only_transport = _PaginatedImgBedTransport(total_files=0, allow_delete=False)
    api_client, _ = _make_app(imgbed_servers={"imgbed-upload-only": upload_only_transport})
    create_res = api_client.post(
        "/v1/admin/storage/profiles",
        json={
            "id": "imgbed-upload-only",
            "name": "Avatar Upload Only",
            "backend": "imgbed",
            "config": {
                "base_url": "https://imgbed.example.com",
                "upload_folder": "astrhub/avatars",
                "scopes": ["upload"],
            },
            "secret": {"token": "tok-upload-only"},
        },
    )
    assert create_res.status_code == 201

    for _ in range(3):
        probe_res = api_client.post("/v1/admin/storage/profiles/imgbed-upload-only/test")
        assert probe_res.status_code == 200
        payload = probe_res.json()
        assert payload["ok"] is True
        assert payload["checks"]["write_probe_skipped_no_delete_scope"] is True
        assert payload["checks"]["delete"] is False
        assert payload["checks"]["plugin_zip_ready"] is False
    assert upload_only_transport.uploaded_files == []


def test_p2_3_decommission_persistence_failure_preserves_secret_and_active_driver(
    tmp_path: Path,
) -> None:
    """P2-3: Persistence failure during retire/delete leaves active profile, encrypted secret, and driver intact."""
    state_file = tmp_path / "decomm-state.json"
    store = LocalFileMarketStore(state_file, 3600)
    store.connect()
    client, app = _make_app(store=store)
    manager: StorageManager = app.state.storage_manager

    # Create a WebDAV profile with an encrypted secret (no real network call needed for create).
    create_res = client.post(
        "/v1/admin/storage/profiles",
        json={
            "id": "dav-rollback",
            "name": "Rollback WebDAV",
            "backend": "webdav",
            "config": {
                "base_url": "https://dav.example.com/dav",
                "root_path": "pkg",
                "auth_type": "bearer",
            },
            "secret": {"token": "secret-token-must-survive"},
        },
    )
    assert create_res.status_code == 201

    async def verify_rollback() -> None:
        before = await manager.repository.get_profile("dav-rollback")
        assert before is not None and before.secret_ref
        original_secret_ref = before.secret_ref

        original_persist = store.persist_object_storage

        def failing_persist() -> None:
            raise OSError("Simulated DB failure on decommission")

        store.persist_object_storage = failing_persist  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError, match="Simulated DB failure"):
                await manager.retire_profile("dav-rollback", actor_id="core-1")
            with pytest.raises(OSError, match="Simulated DB failure"):
                await manager.delete_profile("dav-rollback", actor_id="core-1")
        finally:
            store.persist_object_storage = original_persist  # type: ignore[method-assign]

        after = await manager.repository.get_profile("dav-rollback")
        assert after is not None
        assert after.status == StorageProfileStatus.ACTIVE
        assert after.secret_ref == original_secret_ref
        # Encrypted secret is STILL intact and decryptable, and driver is STILL active!
        decrypted = await manager._load_secret(after)
        assert decrypted["token"] == "secret-token-must-survive"
        assert manager.service.driver_for_profile("dav-rollback") is not None

    asyncio.run(verify_rollback())
