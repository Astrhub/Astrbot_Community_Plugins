"""SQLite chunked object storage driver implementing ObjectStorageDriver."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
from collections.abc import AsyncIterable, Iterable, Mapping
from pathlib import Path

from .models import (
    ObjectListingEntry,
    ObjectRange,
    ObjectStorageError,
    StorageBackend,
    StorageCapabilities,
    StoredObject,
    build_public_url,
    guess_content_type,
    utc_now_iso,
    validate_expected_sha256,
    validate_expected_size,
    validate_namespace,
    validate_object_key,
    validate_profile_id,
    validate_range_params,
)

DEFAULT_CHUNK_SIZE = 256 * 1024

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_blob_objects (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT '',
    object_key TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content_type TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    chunk_size INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(profile_id, namespace, object_key)
);

CREATE TABLE IF NOT EXISTS storage_blob_chunks (
    object_id TEXT NOT NULL REFERENCES storage_blob_objects(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    data BLOB NOT NULL,
    size_bytes INTEGER NOT NULL,
    PRIMARY KEY(object_id, chunk_index)
);
"""


def validate_sqlite_db_path(
    db_path: str | Path,
    *,
    forbidden_roots: tuple[Path, ...] = (),
    forbidden_files: tuple[Path, ...] = (),
) -> Path:
    raw = str(db_path or "").strip()
    if not raw:
        raise ObjectStorageError("sqlite_path_missing", "SQLite database path is required")
    resolved = Path(raw).expanduser().resolve()
    if resolved.suffix.lower() == ".json":
        raise ObjectStorageError(
            "sqlite_path_invalid", "SQLite object store cannot reuse a JSON state file"
        )
    for forbidden_file in forbidden_files:
        if resolved == forbidden_file.resolve():
            raise ObjectStorageError(
                "sqlite_path_invalid",
                "SQLite object store cannot overwrite market state storage file",
            )
    for root in forbidden_roots:
        resolved_root = root.resolve()
        if resolved == resolved_root or resolved_root in resolved.parents:
            raise ObjectStorageError(
                "sqlite_path_inside_repo",
                "SQLite object store file must be placed outside the source repository",
            )
    return resolved


class SqliteObjectStorageDriver:
    backend = StorageBackend.LOCAL

    def __init__(
        self,
        *,
        profile_id: str,
        db_path: str | Path,
        public_base_url: str = "",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.db_path = validate_sqlite_db_path(db_path)
        self.public_base_url = str(public_base_url or "").strip().rstrip("/")
        self.chunk_size = max(1024, int(chunk_size))
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()
        except (OSError, sqlite3.Error) as exc:
            raise ObjectStorageError(
                "sqlite_storage_unavailable",
                f"SQLite object storage is unavailable: {exc}",
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SQLITE_SCHEMA)

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            conditional_create=True,
            range_read=True,
            public_read=bool(self.public_base_url),
            image_variants=False,
            list_objects=True,
        )

    async def put_object(
        self,
        key: str,
        data: bytes | AsyncIterable[bytes],
        *,
        namespace: str = "",
        content_type: str = "",
        max_bytes: int | None = None,
        expected_sha256: str = "",
        if_absent: bool = True,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        resolved_type = guess_content_type(key, content_type)
        meta_dict = {str(k): str(v) for k, v in (metadata or {}).items()}
        if isinstance(data, (bytes, bytearray, memoryview)):
            payload = bytes(data)
            if max_bytes is not None and len(payload) > max_bytes:
                raise ObjectStorageError("object_too_large", "Object exceeds size limit")
            sha256 = hashlib.sha256(payload).hexdigest()
            validate_expected_sha256(expected_sha256, sha256)
            return await asyncio.to_thread(
                self._put_bytes_sync,
                normalized_ns,
                key,
                payload,
                sha256,
                resolved_type,
                meta_dict,
                if_absent,
            )
        temporary, size, sha256 = await _stream_to_temporary(data, max_bytes, expected_sha256)
        try:
            return await asyncio.to_thread(
                self._put_file_sync,
                normalized_ns,
                key,
                temporary,
                size,
                sha256,
                resolved_type,
                meta_dict,
                if_absent,
            )
        finally:
            temporary.unlink(missing_ok=True)

    async def stat_object(self, key: str, *, namespace: str = "") -> StoredObject | None:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        return await asyncio.to_thread(self._stat_sync, normalized_ns, key)

    async def read_object(
        self,
        key: str,
        *,
        namespace: str = "",
        max_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> bytes:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        return await asyncio.to_thread(
            self._read_sync,
            normalized_ns,
            key,
            max_bytes,
            expected_sha256,
        )

    async def read_range(
        self,
        key: str,
        *,
        namespace: str = "",
        start_byte: int,
        max_bytes: int,
        max_object_bytes: int | None = None,
        expected_size_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> ObjectRange:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        validate_range_params(start_byte, max_bytes, max_object_bytes)
        return await asyncio.to_thread(
            self._read_range_sync,
            normalized_ns,
            key,
            start_byte,
            max_bytes,
            max_object_bytes,
            expected_size_bytes,
            expected_sha256,
        )

    async def download_to_path(
        self,
        key: str,
        destination: Path,
        *,
        namespace: str = "",
    ) -> StoredObject:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        destination.parent.mkdir(parents=True, exist_ok=True)
        return await asyncio.to_thread(self._download_sync, normalized_ns, key, destination)

    async def copy_object(
        self,
        source_key: str,
        target_key: str,
        *,
        source_namespace: str = "",
        target_namespace: str = "",
        expected_sha256: str = "",
        content_type: str = "",
        if_absent: bool = True,
    ) -> StoredObject:
        validate_object_key(source_key)
        validate_object_key(target_key)
        src_ns = validate_namespace(source_namespace)
        dst_ns = validate_namespace(target_namespace)
        return await asyncio.to_thread(
            self._copy_sync,
            src_ns,
            source_key,
            dst_ns,
            target_key,
            expected_sha256,
            content_type,
            if_absent,
        )

    async def delete_object(self, key: str, *, namespace: str = "") -> None:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        await asyncio.to_thread(self._delete_sync, normalized_ns, key)

    def public_url(self, key: str, *, namespace: str = "") -> str:
        normalized_ns = validate_namespace(namespace)
        full_key = f"{normalized_ns}/{key}" if normalized_ns else key
        return build_public_url(self.public_base_url, full_key)

    async def list_objects(
        self,
        *,
        namespace: str = "",
        prefix: str = "",
    ) -> list[ObjectListingEntry]:
        normalized_ns = validate_namespace(namespace)
        return await asyncio.to_thread(self._list_sync, normalized_ns, prefix)

    def _optional_public_url(self, key: str, namespace: str) -> str:
        if not self.public_base_url:
            return ""
        full_key = f"{namespace}/{key}" if namespace else key
        return build_public_url(self.public_base_url, full_key)

    def _row_to_stored(self, row: sqlite3.Row) -> StoredObject:
        metadata = json.loads(row["metadata_json"] or "{}")
        namespace = str(row["namespace"])
        key = str(row["object_key"])
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=namespace,
            key=key,
            size_bytes=int(row["size_bytes"]),
            sha256=str(row["sha256"]),
            content_type=str(row["content_type"]),
            public_url=self._optional_public_url(key, namespace),
            metadata=metadata,
        )

    def _put_bytes_sync(
        self,
        namespace: str,
        key: str,
        payload: bytes,
        sha256: str,
        content_type: str,
        metadata: dict[str, str],
        if_absent: bool,
    ) -> StoredObject:
        chunks = [
            payload[offset : offset + self.chunk_size]
            for offset in range(0, len(payload), self.chunk_size)
        ]
        return self._write_chunks_sync(
            namespace=namespace,
            key=key,
            size_bytes=len(payload),
            sha256=sha256,
            content_type=content_type,
            metadata=metadata,
            chunks=chunks,
            if_absent=if_absent,
        )

    def _put_file_sync(
        self,
        namespace: str,
        key: str,
        file_path: Path,
        size_bytes: int,
        sha256: str,
        content_type: str,
        metadata: dict[str, str],
        if_absent: bool,
    ) -> StoredObject:
        def chunk_iter():
            with file_path.open("rb") as handle:
                while part := handle.read(self.chunk_size):
                    yield part

        return self._write_chunks_sync(
            namespace=namespace,
            key=key,
            size_bytes=size_bytes,
            sha256=sha256,
            content_type=content_type,
            metadata=metadata,
            chunks=chunk_iter(),
            if_absent=if_absent,
        )

    def _write_chunks_sync(
        self,
        *,
        namespace: str,
        key: str,
        size_bytes: int,
        sha256: str,
        content_type: str,
        metadata: dict[str, str],
        chunks: Iterable[bytes],
        if_absent: bool,
    ) -> StoredObject:
        now = utc_now_iso()
        object_id = f"sqobj_{secrets.token_hex(12)}"
        metadata_json = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM storage_blob_objects
                 WHERE profile_id = ? AND namespace = ? AND object_key = ?
                """,
                (self.profile_id, namespace, key),
            ).fetchone()
            if existing is not None:
                if if_absent:
                    if str(existing["sha256"]) == sha256:
                        return self._row_to_stored(existing)
                    raise ObjectStorageError(
                        "object_key_conflict", "Object already exists with different content"
                    )
                conn.execute("DELETE FROM storage_blob_objects WHERE id = ?", (existing["id"],))

            chunk_list = list(chunks)
            conn.execute(
                """
                INSERT INTO storage_blob_objects (
                    id, profile_id, namespace, object_key, size_bytes, sha256,
                    content_type, metadata_json, chunk_size, chunk_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    object_id,
                    self.profile_id,
                    namespace,
                    key,
                    size_bytes,
                    sha256,
                    content_type,
                    metadata_json,
                    self.chunk_size,
                    len(chunk_list),
                    now,
                    now,
                ),
            )
            for index, chunk in enumerate(chunk_list):
                conn.execute(
                    """
                    INSERT INTO storage_blob_chunks (object_id, chunk_index, data, size_bytes)
                    VALUES (?, ?, ?, ?)
                    """,
                    (object_id, index, sqlite3.Binary(chunk), len(chunk)),
                )
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=namespace,
            key=key,
            size_bytes=size_bytes,
            sha256=sha256,
            content_type=content_type,
            public_url=self._optional_public_url(key, namespace),
            metadata=metadata,
        )

    def _stat_sync(self, namespace: str, key: str) -> StoredObject | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM storage_blob_objects
                 WHERE profile_id = ? AND namespace = ? AND object_key = ?
                """,
                (self.profile_id, namespace, key),
            ).fetchone()
        return self._row_to_stored(row) if row is not None else None

    def _read_sync(
        self,
        namespace: str,
        key: str,
        max_bytes: int | None,
        expected_sha256: str,
    ) -> bytes:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM storage_blob_objects
                 WHERE profile_id = ? AND namespace = ? AND object_key = ?
                """,
                (self.profile_id, namespace, key),
            ).fetchone()
            if row is None:
                raise ObjectStorageError("object_missing", "Object not found")
            size_bytes = int(row["size_bytes"])
            if max_bytes is not None and size_bytes > max_bytes:
                raise ObjectStorageError("object_too_large", "Object exceeds limit")
            chunk_rows = conn.execute(
                "SELECT data FROM storage_blob_chunks WHERE object_id = ? ORDER BY chunk_index ASC",
                (row["id"],),
            ).fetchall()
        content = b"".join(bytes(part["data"]) for part in chunk_rows)
        validate_expected_size(size_bytes, len(content))
        sha256 = hashlib.sha256(content).hexdigest()
        validate_expected_sha256(str(row["sha256"]), sha256)
        validate_expected_sha256(expected_sha256, sha256)
        return content

    def _read_range_sync(
        self,
        namespace: str,
        key: str,
        start_byte: int,
        max_bytes: int,
        max_object_bytes: int | None,
        expected_size_bytes: int | None,
        expected_sha256: str,
    ) -> ObjectRange:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM storage_blob_objects
                 WHERE profile_id = ? AND namespace = ? AND object_key = ?
                """,
                (self.profile_id, namespace, key),
            ).fetchone()
            if row is None:
                raise ObjectStorageError("object_missing", "Object not found")
            size_bytes = int(row["size_bytes"])
            if max_object_bytes is not None and size_bytes > max_object_bytes:
                raise ObjectStorageError("object_too_large", "Object exceeds limit")
            validate_expected_size(expected_size_bytes, size_bytes)
            if start_byte > size_bytes:
                raise ObjectStorageError("object_range_invalid", "Object range is invalid")
            stored_sha = str(row["sha256"])
            validate_expected_sha256(expected_sha256, stored_sha)

            end_byte = min(size_bytes, start_byte + max_bytes)
            if start_byte == end_byte:
                slice_bytes = b""
            else:
                chunk_size = int(row["chunk_size"])
                start_chunk = start_byte // chunk_size
                end_chunk = (end_byte - 1) // chunk_size
                chunk_rows = conn.execute(
                    """
                    SELECT chunk_index, data FROM storage_blob_chunks
                     WHERE object_id = ? AND chunk_index BETWEEN ? AND ?
                     ORDER BY chunk_index ASC
                    """,
                    (row["id"], start_chunk, end_chunk),
                ).fetchall()
                pieces: list[bytes] = []
                for chunk_row in chunk_rows:
                    idx = int(chunk_row["chunk_index"])
                    raw = bytes(chunk_row["data"])
                    chunk_start = idx * chunk_size
                    local_start = max(0, start_byte - chunk_start)
                    local_end = min(len(raw), end_byte - chunk_start)
                    pieces.append(raw[local_start:local_end])
                slice_bytes = b"".join(pieces)
        return ObjectRange(
            content=slice_bytes,
            size_bytes=size_bytes,
            sha256=stored_sha,
            content_type=str(row["content_type"]),
            start_byte=start_byte,
            end_byte=end_byte,
            truncated=end_byte < size_bytes,
        )

    def _download_sync(self, namespace: str, key: str, destination: Path) -> StoredObject:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM storage_blob_objects
                 WHERE profile_id = ? AND namespace = ? AND object_key = ?
                """,
                (self.profile_id, namespace, key),
            ).fetchone()
            if row is None:
                raise ObjectStorageError("object_missing", "Object not found")
            digest = hashlib.sha256()
            written = 0
            with destination.open("wb") as handle:
                cursor = conn.execute(
                    """
                    SELECT data FROM storage_blob_chunks
                     WHERE object_id = ?
                     ORDER BY chunk_index ASC
                    """,
                    (row["id"],),
                )
                for chunk_row in cursor:
                    raw = bytes(chunk_row["data"])
                    written += len(raw)
                    digest.update(raw)
                    handle.write(raw)
        validate_expected_size(int(row["size_bytes"]), written)
        validate_expected_sha256(str(row["sha256"]), digest.hexdigest())
        return self._row_to_stored(row)

    def _copy_sync(
        self,
        source_namespace: str,
        source_key: str,
        target_namespace: str,
        target_key: str,
        expected_sha256: str,
        content_type: str,
        if_absent: bool,
    ) -> StoredObject:
        now = utc_now_iso()
        new_id = f"sqobj_{secrets.token_hex(12)}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            src = conn.execute(
                """
                SELECT * FROM storage_blob_objects
                 WHERE profile_id = ? AND namespace = ? AND object_key = ?
                """,
                (self.profile_id, source_namespace, source_key),
            ).fetchone()
            if src is None:
                raise ObjectStorageError("object_missing", "Source object not found")
            src_sha = str(src["sha256"])
            validate_expected_sha256(expected_sha256, src_sha)
            existing = conn.execute(
                """
                SELECT * FROM storage_blob_objects
                 WHERE profile_id = ? AND namespace = ? AND object_key = ?
                """,
                (self.profile_id, target_namespace, target_key),
            ).fetchone()
            if existing is not None:
                if if_absent:
                    if str(existing["sha256"]) == src_sha:
                        return self._row_to_stored(existing)
                    raise ObjectStorageError(
                        "object_key_conflict", "Object already exists with different content"
                    )
                if str(existing["id"]) == str(src["id"]):
                    return self._row_to_stored(existing)
                conn.execute("DELETE FROM storage_blob_objects WHERE id = ?", (existing["id"],))
            resolved_type = guess_content_type(target_key, content_type or str(src["content_type"]))
            conn.execute(
                """
                INSERT INTO storage_blob_objects (
                    id, profile_id, namespace, object_key, size_bytes, sha256,
                    content_type, metadata_json, chunk_size, chunk_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    self.profile_id,
                    target_namespace,
                    target_key,
                    int(src["size_bytes"]),
                    src_sha,
                    resolved_type,
                    str(src["metadata_json"]),
                    int(src["chunk_size"]),
                    int(src["chunk_count"]),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO storage_blob_chunks (object_id, chunk_index, data, size_bytes)
                SELECT ?, chunk_index, data, size_bytes
                  FROM storage_blob_chunks
                 WHERE object_id = ?
                """,
                (new_id, src["id"]),
            )
            created = conn.execute(
                "SELECT * FROM storage_blob_objects WHERE id = ?",
                (new_id,),
            ).fetchone()
        return self._row_to_stored(created)

    def _delete_sync(self, namespace: str, key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM storage_blob_objects
                 WHERE profile_id = ? AND namespace = ? AND object_key = ?
                """,
                (self.profile_id, namespace, key),
            )

    def _list_sync(self, namespace: str, prefix: str) -> list[ObjectListingEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT namespace, object_key, size_bytes, sha256, content_type
                  FROM storage_blob_objects
                 WHERE profile_id = ? AND namespace = ?
                 ORDER BY object_key ASC
                """,
                (self.profile_id, namespace),
            ).fetchall()
        return [
            ObjectListingEntry(
                namespace=str(row["namespace"]),
                key=str(row["object_key"]),
                size_bytes=int(row["size_bytes"]),
                sha256=str(row["sha256"]),
                content_type=str(row["content_type"]),
            )
            for row in rows
            if not prefix or str(row["object_key"]).startswith(prefix)
        ]


async def _stream_to_temporary(
    stream: AsyncIterable[bytes],
    max_bytes: int | None,
    expected_sha256: str,
) -> tuple[Path, int, str]:
    descriptor, name = tempfile.mkstemp(prefix="sqlite-obj-", suffix=".tmp")
    os.close(descriptor)
    temporary = Path(name)
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in stream:
                if not chunk:
                    continue
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise ObjectStorageError("object_too_large", "Object exceeds size limit")
                digest.update(chunk)
                await asyncio.to_thread(handle.write, chunk)
        sha256 = digest.hexdigest()
        validate_expected_sha256(expected_sha256, sha256)
        return temporary, size, sha256
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
