"""Stage 2 tests for object storage DB drivers, management APIs, migrations, and delivery."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import load_settings
from app.main import create_app
from app.object_storage import (
    LocalFilesystemDriver,
    ObjectStorageError,
    PostgresObjectStorageDriver,
    SqliteObjectStorageDriver,
    StorageManager,
    StoragePurpose,
    validate_pg_schema,
    validate_sqlite_db_path,
)
from app.object_storage.models import StorageMigrationItemStatus, StorageMigrationStatus
from app.store import InMemoryMarketStore, LocalFileMarketStore


class FakePgBlobPool:
    """In-memory asyncpg-compatible pool implementing the SQL subset used by PostgresObjectStorageDriver."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[int, bytes]] = {}
        self.schemas: set[str] = {"public"}

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_FakePgConn]:
        yield _FakePgConn(self)

    async def close(self) -> None:
        return None


class _FakePgConn:
    def __init__(self, pool: FakePgBlobPool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        objs_snapshot = {k: dict(v) for k, v in self._pool.objects.items()}
        chunks_snapshot = {k: dict(v) for k, v in self._pool.chunks.items()}
        try:
            yield None
        except Exception:
            self._pool.objects = objs_snapshot
            self._pool.chunks = chunks_snapshot
            raise

    async def execute(self, sql: str, *args: Any) -> str:
        normalized = " ".join(sql.split())
        if normalized.startswith("CREATE SCHEMA IF NOT EXISTS"):
            schema = normalized.split('"')[1]
            self._pool.schemas.add(schema)
            return "CREATE SCHEMA"
        if normalized.startswith("CREATE TABLE IF NOT EXISTS"):
            return "CREATE TABLE"
        if normalized.startswith("DELETE FROM") and "WHERE id = $1" in normalized:
            object_id = str(args[0])
            self._pool.objects.pop(object_id, None)
            self._pool.chunks.pop(object_id, None)
            return "DELETE 1"
        if normalized.startswith("DELETE FROM") and "WHERE profile_id = $1" in normalized:
            profile_id, namespace, key = str(args[0]), str(args[1]), str(args[2])
            for oid, row in list(self._pool.objects.items()):
                if (
                    row["profile_id"] == profile_id
                    and row["namespace"] == namespace
                    and row["object_key"] == key
                ):
                    self._pool.objects.pop(oid, None)
                    self._pool.chunks.pop(oid, None)
            return "DELETE"
        if "INSERT INTO" in normalized and "market_storage_blob_chunks" in normalized:
            if "SELECT $1, chunk_index" in normalized:
                new_id, src_id = str(args[0]), str(args[1])
                self._pool.chunks[new_id] = dict(self._pool.chunks.get(src_id, {}))
                return "INSERT"
            object_id, idx, data = str(args[0]), int(args[1]), bytes(args[2])
            self._pool.chunks.setdefault(object_id, {})[idx] = data
            return "INSERT 1"
        raise AssertionError(f"Unexpected SQL execute: {normalized}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT * FROM") and "WHERE profile_id = $1" in normalized:
            profile_id, namespace, key = str(args[0]), str(args[1]), str(args[2])
            for row in self._pool.objects.values():
                if (
                    row["profile_id"] == profile_id
                    and row["namespace"] == namespace
                    and row["object_key"] == key
                ):
                    return dict(row)
            return None
        if normalized.startswith("INSERT INTO") and "market_storage_blob_objects" in normalized:
            row = {
                "id": str(args[0]),
                "profile_id": str(args[1]),
                "namespace": str(args[2]),
                "object_key": str(args[3]),
                "size_bytes": int(args[4]),
                "sha256": str(args[5]),
                "content_type": str(args[6]),
                "metadata": str(args[7]),
                "chunk_size": int(args[8]),
                "chunk_count": int(args[9]),
            }
            self._pool.objects[row["id"]] = row
            return dict(row)
        raise AssertionError(f"Unexpected SQL fetchrow: {normalized}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        normalized = " ".join(sql.split())
        if "FROM" in normalized and "market_storage_blob_chunks" in normalized:
            object_id = str(args[0])
            bucket = self._pool.chunks.get(object_id, {})
            if "BETWEEN $2 AND $3" in normalized:
                start_idx, end_idx = int(args[1]), int(args[2])
                return [
                    {"chunk_index": idx, "data": bucket[idx]}
                    for idx in sorted(bucket)
                    if start_idx <= idx <= end_idx
                ]
            return [{"data": bucket[idx]} for idx in sorted(bucket)]
        if "SELECT namespace, object_key, size_bytes" in normalized:
            profile_id, namespace = str(args[0]), str(args[1])
            matches = [
                row
                for row in self._pool.objects.values()
                if row["profile_id"] == profile_id and row["namespace"] == namespace
            ]
            matches.sort(key=lambda r: str(r["object_key"]))
            return [dict(row) for row in matches]
        raise AssertionError(f"Unexpected SQL fetch: {normalized}")


class FakeS3Client:
    """Minimal in-memory boto3-like client for S3 profile CRUD & health tests."""

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, tuple[bytes, str]]] = {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        raw = body.read() if hasattr(body, "read") else bytes(body)
        if kwargs.get("IfNoneMatch") == "*" and key in self.buckets.get(bucket, {}):
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "Exists"}},
                "PutObject",
            )
        self.buckets.setdefault(bucket, {})[key] = (
            raw,
            str(kwargs.get("ContentType") or "application/octet-stream"),
        )
        return {}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key = str(kwargs["Bucket"]), str(kwargs["Key"])
        entry = self.buckets.get(bucket, {}).get(key)
        if entry is None:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        data, content_type = entry
        return {"ContentLength": len(data), "ContentType": content_type, "Metadata": {}}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key = str(kwargs["Bucket"]), str(kwargs["Key"])
        entry = self.buckets.get(bucket, {}).get(key)
        if entry is None:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Missing"}}, "GetObject")
        data, content_type = entry
        if "Range" in kwargs:
            rng = str(kwargs["Range"]).removeprefix("bytes=")
            start_s, end_s = rng.split("-", 1)
            start = int(start_s)
            end = int(end_s) if end_s else len(data) - 1
            sliced = data[start : end + 1]
            return {
                "Body": _FakeBody(sliced),
                "ContentLength": len(sliced),
                "ContentRange": f"bytes {start}-{min(end, len(data) - 1)}/{len(data)}",
                "ContentType": content_type,
                "Metadata": {},
            }
        return {
            "Body": _FakeBody(data),
            "ContentLength": len(data),
            "ContentType": content_type,
            "Metadata": {},
        }

    def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        src_bucket = str(kwargs["CopySource"]["Bucket"])
        src_key = str(kwargs["CopySource"]["Key"])
        dst_bucket = str(kwargs["Bucket"])
        dst_key = str(kwargs["Key"])
        entry = self.buckets.get(src_bucket, {}).get(src_key)
        if entry is None:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Missing"}}, "CopyObject")
        self.buckets.setdefault(dst_bucket, {})[dst_key] = entry
        return {}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.buckets.get(str(kwargs["Bucket"]), {}).pop(str(kwargs["Key"]), None)
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        bucket = str(kwargs["Bucket"])
        prefix = str(kwargs.get("Prefix") or "")
        contents = [
            {"Key": key, "Size": len(data)}
            for key, (data, _) in sorted(self.buckets.get(bucket, {}).items())
            if not prefix or key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + amount]
        self._pos += len(chunk)
        return chunk

    def iter_chunks(self, chunk_size: int = 1024) -> Any:
        for offset in range(0, len(self._data), chunk_size):
            yield self._data[offset : offset + chunk_size]

    def close(self) -> None:
        return None


async def _chunk_stream(payload: bytes, size: int = 333) -> AsyncIterator[bytes]:
    for offset in range(0, len(payload), size):
        yield payload[offset : offset + size]


@pytest.mark.parametrize("driver_kind", ["filesystem", "sqlite", "postgresql"])
def test_local_drivers_contract_streaming_range_copy_and_delete(
    tmp_path: Path,
    driver_kind: str,
) -> None:
    if driver_kind == "filesystem":
        driver: Any = LocalFilesystemDriver(
            profile_id="contract-fs",
            root=tmp_path / "fs",
            public_base_url="https://cdn.example.com/packages",
        )
    elif driver_kind == "sqlite":
        driver = SqliteObjectStorageDriver(
            profile_id="contract-sqlite",
            db_path=tmp_path / "blobs.sqlite3",
            public_base_url="https://cdn.example.com/packages",
            chunk_size=1024,
        )
    else:
        driver = PostgresObjectStorageDriver(
            profile_id="contract-pg",
            pool=FakePgBlobPool(),
            schema="blob_store",
            public_base_url="https://cdn.example.com/packages",
            chunk_size=1024,
        )

    payload = (b"AstrBotChunkData" * 200) + b"TAIL-END!"
    payload_sha = hashlib.sha256(payload).hexdigest()

    async def exercise() -> None:
        stored = await driver.put_object(
            "alice/demo/1.0.0/demo.zip",
            _chunk_stream(payload),
            namespace="published",
            content_type="application/zip",
            expected_sha256=payload_sha,
            if_absent=True,
        )
        assert stored.size_bytes == len(payload)
        assert stored.sha256 == payload_sha
        assert stored.public_url.endswith("/published/alice/demo/1.0.0/demo.zip")

        # Idempotent put with same content passes under if_absent=True.
        same = await driver.put_object(
            "alice/demo/1.0.0/demo.zip",
            payload,
            namespace="published",
            if_absent=True,
        )
        assert same.sha256 == payload_sha

        # Different content fails with object_key_conflict under if_absent=True.
        with pytest.raises(ObjectStorageError) as conflict_exc:
            await driver.put_object(
                "alice/demo/1.0.0/demo.zip",
                b"different-content",
                namespace="published",
                if_absent=True,
            )
        assert conflict_exc.value.code == "object_key_conflict"

        # Multi-chunk Range slice across 1024-byte chunk boundary (bytes 1000..2100).
        ranged = await driver.read_range(
            "alice/demo/1.0.0/demo.zip",
            namespace="published",
            start_byte=1000,
            max_bytes=1100,
            max_object_bytes=len(payload),
            expected_size_bytes=len(payload),
            expected_sha256=payload_sha,
        )
        assert ranged.content == payload[1000:2100]
        assert ranged.truncated is True

        # download_to_path verifies full integrity.
        dl_path = tmp_path / f"{driver_kind}_downloaded.zip"
        dl_info = await driver.download_to_path(
            "alice/demo/1.0.0/demo.zip",
            dl_path,
            namespace="published",
        )
        assert dl_info.sha256 == payload_sha
        assert dl_path.read_bytes() == payload

        # Copy to another namespace/key & overwrite test.
        copied = await driver.copy_object(
            "alice/demo/1.0.0/demo.zip",
            "alice/demo/1.0.0/demo-copy.zip",
            source_namespace="published",
            target_namespace="published",
            expected_sha256=payload_sha,
            if_absent=True,
        )
        assert copied.sha256 == payload_sha
        assert (
            await driver.read_object("alice/demo/1.0.0/demo-copy.zip", namespace="published")
            == payload
        )

        # Overwrite with if_absent=False succeeds.
        replaced = await driver.put_object(
            "alice/demo/1.0.0/demo-copy.zip",
            b"updated-v2",
            namespace="published",
            if_absent=False,
        )
        assert replaced.size_bytes == len(b"updated-v2")
        assert (
            await driver.read_object("alice/demo/1.0.0/demo-copy.zip", namespace="published")
            == b"updated-v2"
        )

        listed = await driver.list_objects(namespace="published", prefix="alice/demo/")
        assert sorted(entry.key for entry in listed) == [
            "alice/demo/1.0.0/demo-copy.zip",
            "alice/demo/1.0.0/demo.zip",
        ]

        await driver.delete_object("alice/demo/1.0.0/demo-copy.zip", namespace="published")
        assert (
            await driver.stat_object("alice/demo/1.0.0/demo-copy.zip", namespace="published")
            is None
        )

    asyncio.run(exercise())


def test_sqlite_and_pg_path_and_schema_validators(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    json_state = tmp_path / "market-data.json"

    with pytest.raises(ObjectStorageError) as repo_exc:
        validate_sqlite_db_path(repo_root / "blobs.sqlite3", forbidden_roots=(repo_root,))
    assert repo_exc.value.code == "sqlite_path_inside_repo"

    with pytest.raises(ObjectStorageError) as json_exc:
        validate_sqlite_db_path(tmp_path / "blobs.json")
    assert json_exc.value.code == "sqlite_path_invalid"

    with pytest.raises(ObjectStorageError) as same_file_exc:
        validate_sqlite_db_path(json_state, forbidden_files=(json_state,))
    assert same_file_exc.value.code == "sqlite_path_invalid"

    with pytest.raises(ObjectStorageError) as schema_exc:
        validate_pg_schema("public; DROP TABLE users")
    assert schema_exc.value.code == "invalid_pg_schema"
    assert validate_pg_schema("storage_blobs_1") == "storage_blobs_1"


def _make_storage_app(
    tmp_path: Path, *, s3_client: FakeS3Client | None = None
) -> tuple[TestClient, Any]:
    store = InMemoryMarketStore()
    store.create_internal_admin("coreadmin", hash_password("corepass123"))
    store.create_internal_user("regularuser", hash_password("userpass123"), "user")
    dev_user = store.create_internal_user("devuser", hash_password("devpass123"), "user")
    store.upsert_oauth_identity(
        user_id=dev_user["id"],
        provider="github",
        instance_url="https://github.com",
        provider_user_id="55501",
        provider_login="devuser_gh",
    )
    store.create_internal_user("adminuser", hash_password("adminpass123"), "admin")

    settings = load_settings().with_updates(
        oauth_credential_key="test-master-key-for-storage-stage2",
    )
    app = create_app(settings=settings, store=store)
    if s3_client is not None:
        app.state.storage_manager._s3_client_factory = lambda _profile, _cfg: s3_client
    return TestClient(app, follow_redirects=False), app


def _login(client: TestClient, username: str, password: str) -> None:
    res = client.post(
        "/v1/auth/internal/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200, res.text


def test_storage_admin_authz_cache_headers_and_openapi_filtering(tmp_path: Path) -> None:
    client, _ = _make_storage_app(tmp_path)

    # Guest denied & OpenAPI excludes /v1/admin/storage/*
    guest_res = client.get("/v1/admin/storage/profiles")
    assert guest_res.status_code == 401
    assert guest_res.headers.get("cache-control") == "private, no-store"
    public_openapi = client.get("/openapi.json").json()
    assert "/v1/admin/storage/profiles" not in public_openapi["paths"]
    assert "StorageProfileCreatePayload" not in public_openapi.get("components", {}).get(
        "schemas", {}
    )

    # Regular user, developer, and normal admin are all forbidden (403) and cannot see OpenAPI routes.
    for username, password in (
        ("regularuser", "userpass123"),
        ("devuser", "devpass123"),
        ("adminuser", "adminpass123"),
    ):
        _login(client, username, password)
        res = client.get("/v1/admin/storage/profiles")
        assert res.status_code == 403
        assert res.headers.get("cache-control") == "private, no-store"
        schema = client.get("/openapi.json").json()
        assert "/v1/admin/storage/profiles" not in schema["paths"]
        assert "StorageProfileCreatePayload" not in schema.get("components", {}).get("schemas", {})

    # Core admin succeeds and sees storage admin endpoints in OpenAPI.
    _login(client, "coreadmin", "corepass123")
    res = client.get("/v1/admin/storage/profiles")
    assert res.status_code == 200
    assert res.headers.get("cache-control") == "private, no-store"
    core_schema = client.get("/openapi.json").json()
    assert "/v1/admin/storage/profiles" in core_schema["paths"]
    assert "/v1/admin/storage/bindings/{purpose}" in core_schema["paths"]
    assert "/v1/admin/storage/migrations/{migration_id}" in core_schema["paths"]


def test_profile_crud_secret_encryption_preservation_health_probe_and_local_persistence(
    tmp_path: Path,
) -> None:
    fake_s3 = FakeS3Client()
    client, app = _make_storage_app(tmp_path, s3_client=fake_s3)
    _login(client, "coreadmin", "corepass123")

    # Create S3 profile with encrypted credentials.
    create_s3 = client.post(
        "/v1/admin/storage/profiles",
        json={
            "id": "r2-main",
            "name": "R2 Primary",
            "backend": "s3",
            "config": {
                "endpoint_url": "https://example.r2.cloudflarestorage.com",
                "region": "auto",
                "bucket": "pkg-bucket",
                "public_base_url": "https://cdn.example.com/packages",
            },
            "secret": {
                "access_key_id": "AKIA_SECRET_1",
                "secret_access_key": "SK_SECRET_1",
            },
        },
    )
    assert create_s3.status_code == 201, create_s3.text
    created = create_s3.json()
    assert created["id"] == "r2-main"
    assert created["has_secret"] is True
    assert "AKIA_SECRET_1" not in create_s3.text
    assert "SK_SECRET_1" not in create_s3.text

    # Edit profile leaving secret omitted -> original credentials stay intact.
    patch_s3 = client.patch(
        "/v1/admin/storage/profiles/r2-main",
        json={"name": "R2 Primary Renamed"},
    )
    assert patch_s3.status_code == 200, patch_s3.text
    assert patch_s3.json()["name"] == "R2 Primary Renamed"
    assert patch_s3.json()["has_secret"] is True

    # Run connection test -> passes all checks and cleans up probe object.
    test_res = client.post("/v1/admin/storage/profiles/r2-main/test")
    assert test_res.status_code == 200, test_res.text
    test_payload = test_res.json()
    assert test_payload["ok"] is True
    assert test_payload["checks"] == {
        "write": True,
        "read": True,
        "range": True,
        "delete": True,
        "public_url": True,
    }
    assert fake_s3.buckets.get("pkg-bucket", {}) == {}

    # LocalFileMarketStore persistence across restarts.
    state_file = tmp_path / "market-state.json"
    local_store_1 = LocalFileMarketStore(state_file, 3600)
    local_store_1.connect()
    settings = load_settings().with_updates(
        storage_mode="local",
        local_data_path=str(state_file),
        oauth_credential_key="persist-master-key",
    )
    manager_1 = StorageManager(store=local_store_1, settings=settings)

    async def persist_and_reload() -> None:
        await manager_1.create_profile(
            profile_id="sqlite-archive",
            name="SQLite Archive",
            backend="local",
            config={"mode": "sqlite", "db_path": str(tmp_path / "archive.sqlite3")},
            actor_id="core-1",
        )
        await manager_1.bind_purpose("artifact-published", "sqlite-archive", actor_id="core-1")
        local_store_2 = LocalFileMarketStore(state_file, 3600)
        local_store_2.connect()
        manager_2 = StorageManager(store=local_store_2, settings=settings)
        profiles = await manager_2.list_profiles()
        bindings = await manager_2.list_bindings()
        assert any(p["id"] == "sqlite-archive" for p in profiles)
        pub_binding = next(b for b in bindings if b["purpose"] == "artifact-published")
        assert pub_binding["profile_id"] == "sqlite-archive"

    asyncio.run(persist_and_reload())


def test_end_to_end_rebind_migration_pause_retry_range_aliases_and_decommission_gates(
    tmp_path: Path,
) -> None:
    client, app = _make_storage_app(tmp_path)
    _login(client, "coreadmin", "corepass123")
    manager: StorageManager = app.state.storage_manager

    fs_a_root = tmp_path / "fs-a"
    sqlite_b_path = tmp_path / "sqlite-b.sqlite3"
    sqlite_c_path = tmp_path / "sqlite-c.sqlite3"

    # 1. Create profiles: local-fs-a, sqlite-b, sqlite-c, and local-q (for quarantine).
    for payload in (
        {
            "id": "local-fs-a",
            "name": "Local FS A",
            "backend": "local",
            "config": {
                "mode": "filesystem",
                "root": str(fs_a_root),
                "public_base_url": "https://old-cdn.example.com/packages",
            },
        },
        {
            "id": "sqlite-b",
            "name": "SQLite B",
            "backend": "local",
            "config": {"mode": "sqlite", "db_path": str(sqlite_b_path)},
        },
        {
            "id": "sqlite-c",
            "name": "SQLite C",
            "backend": "local",
            "config": {"mode": "sqlite", "db_path": str(sqlite_c_path)},
        },
        {
            "id": "local-q",
            "name": "Local Quarantine",
            "backend": "local",
            "config": {"mode": "filesystem", "root": str(tmp_path / "fs-q")},
        },
    ):
        res = client.post("/v1/admin/storage/profiles", json=payload)
        assert res.status_code == 201, res.text

    assert (
        client.put(
            "/v1/admin/storage/bindings/artifact-published",
            json={"profile_id": "local-fs-a"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/v1/admin/storage/bindings/artifact-quarantine",
            json={"profile_id": "local-q"},
        ).status_code
        == 200
    )

    # 2. Seed published objects (alpha, beta, gamma) + one private quarantine object + one orphan file.
    alpha_bytes = b"PK\x03\x04ALPHA-PLUGIN-ARCHIVE-0123456789" * 10
    beta_bytes = b"PK\x03\x04BETA-PLUGIN-ARCHIVE-9876543210" * 10
    gamma_bytes = b"PK\x03\x04GAMMA-V1-OLD-ARCHIVE" * 5
    gamma_v2_bytes = b"PK\x03\x04GAMMA-V2-CONCURRENT-REPLACEMENT" * 5
    quarantine_bytes = b"PRIVATE-QUARANTINE-PAYLOAD"

    alpha_key = "alice/alpha/1.0.0/alpha-1.0.0-aabbccdd.zip"
    beta_key = "alice/beta/1.0.0/beta-1.0.0-11223344.zip"
    gamma_key = "alice/gamma/1.0.0/gamma-1.0.0-55667788.zip"

    async def seed_objects() -> tuple[str, str, str, str]:
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=alpha_key,
            data=alpha_bytes,
            content_type="application/zip",
        )
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=beta_key,
            data=beta_bytes,
            content_type="application/zip",
        )
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=gamma_key,
            data=gamma_bytes,
            content_type="application/zip",
        )
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_QUARANTINE,
            key="artifacts/art-1/source.zip",
            data=quarantine_bytes,
            content_type="application/zip",
        )
        objs = await manager.repository.list_objects()
        by_key = {obj.key: obj.id for obj in objs}
        return (
            by_key[alpha_key],
            by_key[beta_key],
            by_key[gamma_key],
            by_key["artifacts/art-1/source.zip"],
        )

    alpha_id, beta_id, gamma_id, quarantine_id = asyncio.run(seed_objects())

    # Place an unregistered orphan file on local-fs-a to verify reconciliation report & decommission gate.
    orphan_path = fs_a_root / "orphan" / "untracked.zip"
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(b"untracked-bytes")

    # Retiring local-fs-a while bound fails with 409.
    assert client.post("/v1/admin/storage/profiles/local-fs-a/retire").status_code == 409

    # 3. Switch artifact-published binding from local-fs-a -> sqlite-b (without running immediately).
    rebind_res = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "sqlite-b", "run_immediately": False},
    )
    assert rebind_res.status_code == 200, rebind_res.text
    migration = rebind_res.json()["migration"]
    assert migration is not None
    migration_id = migration["id"]
    assert migration["total_count"] == 3
    assert any(u["key"] == "orphan/untracked.zip" for u in migration["unknown_objects"])

    # Source profile transitioned to draining.
    profiles = {p["id"]: p for p in client.get("/v1/admin/storage/profiles").json()["items"]}
    assert profiles["local-fs-a"]["status"] == "draining"

    # New writes immediately route to sqlite-b and get same-origin public URL.
    async def write_new_to_sqlite_b() -> str:
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key="alice/fresh/2.0.0/fresh-2.0.0-deadbeef.zip",
            data=b"FRESH-ON-SQLITE-B",
            content_type="application/zip",
        )
        rec = await manager.repository.find_active_object_by_purpose_and_key(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key="alice/fresh/2.0.0/fresh-2.0.0-deadbeef.zip",
        )
        assert rec is not None
        assert rec.profile_id == "sqlite-b"
        assert rec.public_url == f"/v1/storage/public/{rec.id}"
        return rec.id

    fresh_id = asyncio.run(write_new_to_sqlite_b())
    assert client.get(f"/v1/storage/public/{fresh_id}").content == b"FRESH-ON-SQLITE-B"

    # 4. Pause & Resume test: paused migration does not process items.
    assert client.post(f"/v1/admin/storage/migrations/{migration_id}/pause").status_code == 200
    paused_state = asyncio.run(manager.run_migration(migration_id))
    assert paused_state.status == StorageMigrationStatus.PAUSED
    assert paused_state.completed_count == 0
    assert client.post(f"/v1/admin/storage/migrations/{migration_id}/resume").status_code == 200

    # 5. Simulate concurrent replacement of gamma before migration runs, and transient failure on beta.
    async def simulate_concurrent_replace_and_transient_error() -> None:
        # Replace gamma directly onto sqlite-b (newer version), simulating concurrent update.
        stored_v2 = await manager.service.put_object(
            profile_id="sqlite-b",
            key=gamma_key,
            data=gamma_v2_bytes,
            content_type="application/zip",
            register=False,
        )
        current_gamma = await manager.repository.get_object(gamma_id)
        assert current_gamma is not None
        from dataclasses import replace

        await manager.repository.upsert_object(
            replace(
                current_gamma,
                profile_id="sqlite-b",
                size_bytes=stored_v2.size_bytes,
                sha256=stored_v2.sha256,
                public_url=f"/v1/storage/public/{gamma_id}",
            )
        )

        # Inject a one-time failure when copying beta_key.
        original_copy = manager.service.copy_object
        fail_once = {"triggered": False}

        async def flaky_copy(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("source_key") == beta_key and not fail_once["triggered"]:
                fail_once["triggered"] = True
                raise ObjectStorageError("transient_io_error", "Simulated transient copy failure")
            return await original_copy(*args, **kwargs)

        manager.service.copy_object = flaky_copy  # type: ignore[method-assign]
        try:
            result = await manager.run_migration(migration_id)
        finally:
            manager.service.copy_object = original_copy  # type: ignore[method-assign]

        assert result.status == StorageMigrationStatus.COMPLETED_WITH_ERRORS
        assert result.completed_count == 1  # alpha
        assert result.skipped_count == 1  # gamma
        assert result.failed_count == 1  # beta

    asyncio.run(simulate_concurrent_replace_and_transient_error())

    # While beta failed, beta is STILL readable from source profile (zero data loss!).
    assert client.get(f"/v1/storage/public/{beta_id}").content == beta_bytes
    # Gamma kept newer concurrent v2 payload (never overwritten by old v1!).
    assert client.get(f"/v1/storage/public/{gamma_id}").content == gamma_v2_bytes

    # 6. Retry failed items -> ONLY beta retries; alpha stays attempts == 1.
    # Because orphan/untracked.zip is still in unknown_objects, status stays completed_with_errors.
    retry_res = client.post(f"/v1/admin/storage/migrations/{migration_id}/retry-failed")
    assert retry_res.status_code == 200, retry_res.text
    final_mig = asyncio.run(manager.run_migration(migration_id))
    assert final_mig.status == StorageMigrationStatus.COMPLETED_WITH_ERRORS
    assert final_mig.completed_count == 2
    assert final_mig.skipped_count == 1
    assert final_mig.failed_count == 0
    assert len(final_mig.unknown_objects) == 1

    detail = client.get(f"/v1/admin/storage/migrations/{migration_id}").json()
    items_by_key = {entry["source_key"]: entry for entry in detail["items"]}
    assert items_by_key[alpha_key]["attempts"] == 1
    assert items_by_key[alpha_key]["status"] == StorageMigrationItemStatus.COMPLETED.value
    assert items_by_key[beta_key]["attempts"] == 2
    assert items_by_key[beta_key]["status"] == StorageMigrationItemStatus.COMPLETED.value
    assert items_by_key[gamma_key]["status"] == StorageMigrationItemStatus.SKIPPED.value

    # 7. Verify same-origin public delivery & legacy /packages/{key} alias + Range + headers + isolation.
    # Delete local-fs-a alpha file on disk to PROVE delivery reads from migrated sqlite-b!
    (fs_a_root / alpha_key).unlink()

    pub_res = client.get(f"/v1/storage/public/{alpha_id}")
    assert pub_res.status_code == 200
    assert pub_res.content == alpha_bytes
    assert pub_res.headers["cache-control"] == "public, max-age=3600"
    assert pub_res.headers["accept-ranges"] == "bytes"
    assert pub_res.headers["x-content-type-options"] == "nosniff"
    assert pub_res.headers["etag"] == f'"{hashlib.sha256(alpha_bytes).hexdigest()}"'

    head_res = client.head(f"/packages/{alpha_key}")
    assert head_res.status_code == 200
    assert head_res.headers["content-length"] == str(len(alpha_bytes))
    assert head_res.headers["cache-control"] == "public, max-age=3600"

    range_res = client.get(f"/packages/{alpha_key}", headers={"Range": "bytes=4-19"})
    assert range_res.status_code == 206
    assert range_res.content == alpha_bytes[4:20]
    assert range_res.headers["content-range"] == f"bytes 4-19/{len(alpha_bytes)}"
    assert range_res.headers["cache-control"] == "public, max-age=3600"

    bad_range = client.get(f"/packages/{alpha_key}", headers={"Range": "bytes=999999-1000000"})
    assert bad_range.status_code == 416

    # Quarantine private objects & path traversal MUST return 404.
    assert client.get(f"/v1/storage/public/{quarantine_id}").status_code == 404
    assert client.get("/packages/artifacts/art-1/source.zip").status_code == 404
    assert client.get("/packages/../etc/passwd").status_code == 404
    assert client.get("/packages/nonexistent/file.zip").status_code == 404

    # 8. Decommission gate: local-fs-a STILL blocked because migration holds unknown_objects!
    retire_blocked = client.post("/v1/admin/storage/profiles/local-fs-a/retire")
    assert retire_blocked.status_code == 409
    assert retire_blocked.json()["code"] in {
        "storage_profile_has_unfinished_migrations",
        "storage_profile_has_unknown_objects",
    }

    # Import unknown object via explicit admin action -> migration completes cleanly!
    import_res = client.post(
        f"/v1/admin/storage/migrations/{migration_id}/import-unknown",
        json={"run_immediately": True},
    )
    assert import_res.status_code == 200, import_res.text
    assert import_res.headers["cache-control"] == "private, no-store"
    assert import_res.json()["status"] == "completed"
    assert import_res.json()["unknown_objects"] == []
    orphan_pkg_res = client.get("/packages/orphan/untracked.zip")
    assert orphan_pkg_res.status_code == 200
    assert orphan_pkg_res.content == b"untracked-bytes"

    # 9. Clean switch sqlite-b -> sqlite-c (zero unknown objects) -> retire & delete sqlite-b succeed!
    clean_rebind = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "sqlite-c", "run_immediately": True},
    )
    assert clean_rebind.status_code == 200, clean_rebind.text
    assert clean_rebind.json()["migration"]["status"] == "completed"
    assert clean_rebind.json()["migration"]["unknown_objects"] == []

    # Legacy /packages/{alpha_key} now seamlessly serves from sqlite-c!
    assert client.get(f"/packages/{alpha_key}").content == alpha_bytes

    retire_ok = client.post("/v1/admin/storage/profiles/sqlite-b/retire")
    assert retire_ok.status_code == 200, retire_ok.text
    assert retire_ok.json()["status"] == "retired"
    # Retiring does NOT delete underlying sqlite file (no automatic remote deletion).
    assert sqlite_b_path.exists()

    delete_ok = client.delete("/v1/admin/storage/profiles/sqlite-b")
    assert delete_ok.status_code == 200, delete_ok.text
    assert delete_ok.json()["status"] == "removed"


def test_storage_manager_skips_unwritable_legacy_local_root_without_crashing(
    tmp_path: Path,
) -> None:
    blocker_file = tmp_path / "not-a-directory"
    blocker_file.write_text("occupied", encoding="utf-8")
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_STORAGE_BACKEND": "local",
            "ARTIFACT_LOCAL_ROOT": str(blocker_file / "artifacts"),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.com",
        }
    )
    store = InMemoryMarketStore()
    app = create_app(settings=settings, store=store)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
