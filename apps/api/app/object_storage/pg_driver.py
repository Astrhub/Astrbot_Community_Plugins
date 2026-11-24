"""PostgreSQL chunked object storage driver implementing ObjectStorageDriver."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import AsyncIterable, Mapping
from pathlib import Path
from typing import Any

from .models import (
    ObjectListingEntry,
    ObjectRange,
    ObjectStorageError,
    StorageBackend,
    StorageCapabilities,
    StoredObject,
    build_public_url,
    guess_content_type,
    validate_expected_sha256,
    validate_expected_size,
    validate_namespace,
    validate_object_key,
    validate_profile_id,
    validate_range_params,
)

DEFAULT_CHUNK_SIZE = 256 * 1024
SAFE_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def validate_pg_schema(schema: str) -> str:
    cleaned = str(schema or "public").strip() or "public"
    if not SAFE_SQL_IDENTIFIER.fullmatch(cleaned):
        raise ObjectStorageError("invalid_pg_schema", f"Invalid PostgreSQL schema: {schema}")
    return cleaned


class PostgresObjectStorageDriver:
    """Stores objects as chunked bytea rows inside a PostgreSQL database or schema."""

    backend = StorageBackend.LOCAL

    def __init__(
        self,
        *,
        profile_id: str,
        pool: Any | None = None,
        dsn: str = "",
        schema: str = "public",
        public_base_url: str = "",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self._pool = pool
        self._owns_pool = False
        self.dsn = str(dsn or "").strip()
        if self._pool is None and not self.dsn:
            raise ObjectStorageError(
                "pg_connection_unconfigured",
                "PostgreSQL object storage requires either an active pool or a DSN",
            )
        self.schema = validate_pg_schema(schema)
        self.public_base_url = str(public_base_url or "").strip().rstrip("/")
        self.chunk_size = max(1024, int(chunk_size))
        self._schema_ready = False

    @property
    def _objects_table(self) -> str:
        return f'"{self.schema}"."market_storage_blob_objects"'

    @property
    def _chunks_table(self) -> str:
        return f'"{self.schema}"."market_storage_blob_chunks"'

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            conditional_create=True,
            range_read=True,
            public_read=bool(self.public_base_url),
            image_variants=False,
            list_objects=True,
        )

    async def close(self) -> None:
        if self._owns_pool and self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._owns_pool = False
            self._schema_ready = False

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=5)
            self._owns_pool = True
        if not self._schema_ready:
            async with self._pool.acquire() as conn:
                if self.schema != "public":
                    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._objects_table} (
                        id text PRIMARY KEY,
                        profile_id text NOT NULL,
                        namespace text NOT NULL DEFAULT '',
                        object_key text NOT NULL,
                        size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
                        sha256 text NOT NULL CHECK (length(sha256) = 64),
                        content_type text NOT NULL DEFAULT 'application/octet-stream',
                        metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                        chunk_size integer NOT NULL CHECK (chunk_size > 0),
                        chunk_count integer NOT NULL CHECK (chunk_count >= 0),
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        UNIQUE (profile_id, namespace, object_key)
                    )
                    """
                )
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._chunks_table} (
                        object_id text NOT NULL REFERENCES {self._objects_table}(id) ON DELETE CASCADE,
                        chunk_index integer NOT NULL CHECK (chunk_index >= 0),
                        data bytea NOT NULL,
                        size_bytes integer NOT NULL CHECK (size_bytes >= 0),
                        PRIMARY KEY (object_id, chunk_index)
                    )
                    """
                )
            self._schema_ready = True
        return self._pool

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
            chunks = [
                payload[offset : offset + self.chunk_size]
                for offset in range(0, len(payload), self.chunk_size)
            ]
            return await self._store_chunks(
                namespace=normalized_ns,
                key=key,
                size_bytes=len(payload),
                sha256=sha256,
                content_type=resolved_type,
                metadata=meta_dict,
                chunks=chunks,
                if_absent=if_absent,
            )
        temporary, size, sha256 = await _stream_to_temporary(data, max_bytes, expected_sha256)
        try:
            chunks = await asyncio.to_thread(_read_file_chunks, temporary, self.chunk_size)
            return await self._store_chunks(
                namespace=normalized_ns,
                key=key,
                size_bytes=size,
                sha256=sha256,
                content_type=resolved_type,
                metadata=meta_dict,
                chunks=chunks,
                if_absent=if_absent,
            )
        finally:
            temporary.unlink(missing_ok=True)

    async def stat_object(self, key: str, *, namespace: str = "") -> StoredObject | None:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT * FROM {self._objects_table}
                 WHERE profile_id = $1 AND namespace = $2 AND object_key = $3
                """,
                self.profile_id,
                normalized_ns,
                key,
            )
        return self._row_to_stored(row) if row is not None else None

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
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT * FROM {self._objects_table}
                 WHERE profile_id = $1 AND namespace = $2 AND object_key = $3
                """,
                self.profile_id,
                normalized_ns,
                key,
            )
            if row is None:
                raise ObjectStorageError("object_missing", "Object not found")
            size_bytes = int(row["size_bytes"])
            if max_bytes is not None and size_bytes > max_bytes:
                raise ObjectStorageError("object_too_large", "Object exceeds limit")
            chunk_rows = await conn.fetch(
                f"""
                SELECT data FROM {self._chunks_table}
                 WHERE object_id = $1
                 ORDER BY chunk_index ASC
                """,
                row["id"],
            )
        content = b"".join(bytes(part["data"]) for part in chunk_rows)
        validate_expected_size(size_bytes, len(content))
        sha256 = hashlib.sha256(content).hexdigest()
        validate_expected_sha256(str(row["sha256"]), sha256)
        validate_expected_sha256(expected_sha256, sha256)
        return content

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
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT * FROM {self._objects_table}
                 WHERE profile_id = $1 AND namespace = $2 AND object_key = $3
                """,
                self.profile_id,
                normalized_ns,
                key,
            )
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
                chunk_rows = await conn.fetch(
                    f"""
                    SELECT chunk_index, data FROM {self._chunks_table}
                     WHERE object_id = $1 AND chunk_index BETWEEN $2 AND $3
                     ORDER BY chunk_index ASC
                    """,
                    row["id"],
                    start_chunk,
                    end_chunk,
                )
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
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT * FROM {self._objects_table}
                 WHERE profile_id = $1 AND namespace = $2 AND object_key = $3
                """,
                self.profile_id,
                normalized_ns,
                key,
            )
            if row is None:
                raise ObjectStorageError("object_missing", "Object not found")
            chunk_rows = await conn.fetch(
                f"""
                SELECT data FROM {self._chunks_table}
                 WHERE object_id = $1
                 ORDER BY chunk_index ASC
                """,
                row["id"],
            )
        written, digest = await asyncio.to_thread(
            _write_chunks_to_file,
            destination,
            [bytes(part["data"]) for part in chunk_rows],
        )
        validate_expected_size(int(row["size_bytes"]), written)
        validate_expected_sha256(str(row["sha256"]), digest)
        return self._row_to_stored(row)

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
        pool = await self._ensure_pool()
        new_id = f"pgobj_{secrets.token_hex(12)}"
        async with pool.acquire() as conn:
            async with conn.transaction():
                src = await conn.fetchrow(
                    f"""
                    SELECT * FROM {self._objects_table}
                     WHERE profile_id = $1 AND namespace = $2 AND object_key = $3
                    """,
                    self.profile_id,
                    src_ns,
                    source_key,
                )
                if src is None:
                    raise ObjectStorageError("object_missing", "Source object not found")
                src_sha = str(src["sha256"])
                validate_expected_sha256(expected_sha256, src_sha)
                existing = await conn.fetchrow(
                    f"""
                    SELECT * FROM {self._objects_table}
                     WHERE profile_id = $1 AND namespace = $2 AND object_key = $3
                    """,
                    self.profile_id,
                    dst_ns,
                    target_key,
                )
                if existing is not None:
                    if if_absent:
                        if str(existing["sha256"]) == src_sha:
                            return self._row_to_stored(existing)
                        raise ObjectStorageError(
                            "object_key_conflict",
                            "Object already exists with different content",
                        )
                    if str(existing["id"]) == str(src["id"]):
                        return self._row_to_stored(existing)
                    await conn.execute(
                        f"DELETE FROM {self._objects_table} WHERE id = $1",
                        existing["id"],
                    )
                resolved_type = guess_content_type(
                    target_key,
                    content_type or str(src["content_type"]),
                )
                raw_meta = src["metadata"]
                meta_json = (
                    raw_meta if isinstance(raw_meta, str) else json.dumps(dict(raw_meta or {}))
                )
                created = await conn.fetchrow(
                    f"""
                    INSERT INTO {self._objects_table} (
                        id, profile_id, namespace, object_key, size_bytes, sha256,
                        content_type, metadata, chunk_size, chunk_count
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                    RETURNING *
                    """,
                    new_id,
                    self.profile_id,
                    dst_ns,
                    target_key,
                    int(src["size_bytes"]),
                    src_sha,
                    resolved_type,
                    meta_json,
                    int(src["chunk_size"]),
                    int(src["chunk_count"]),
                )
                await conn.execute(
                    f"""
                    INSERT INTO {self._chunks_table} (object_id, chunk_index, data, size_bytes)
                    SELECT $1, chunk_index, data, size_bytes
                      FROM {self._chunks_table}
                     WHERE object_id = $2
                    """,
                    new_id,
                    src["id"],
                )
        return self._row_to_stored(created)

    async def delete_object(self, key: str, *, namespace: str = "") -> None:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                DELETE FROM {self._objects_table}
                 WHERE profile_id = $1 AND namespace = $2 AND object_key = $3
                """,
                self.profile_id,
                normalized_ns,
                key,
            )

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
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT namespace, object_key, size_bytes, sha256, content_type
                  FROM {self._objects_table}
                 WHERE profile_id = $1 AND namespace = $2
                 ORDER BY object_key ASC
                """,
                self.profile_id,
                normalized_ns,
            )
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

    async def _store_chunks(
        self,
        *,
        namespace: str,
        key: str,
        size_bytes: int,
        sha256: str,
        content_type: str,
        metadata: dict[str, str],
        chunks: list[bytes],
        if_absent: bool,
    ) -> StoredObject:
        pool = await self._ensure_pool()
        object_id = f"pgobj_{secrets.token_hex(12)}"
        metadata_json = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    f"""
                    SELECT * FROM {self._objects_table}
                     WHERE profile_id = $1 AND namespace = $2 AND object_key = $3
                    """,
                    self.profile_id,
                    namespace,
                    key,
                )
                if existing is not None:
                    if if_absent:
                        if str(existing["sha256"]) == sha256:
                            return self._row_to_stored(existing)
                        raise ObjectStorageError(
                            "object_key_conflict",
                            "Object already exists with different content",
                        )
                    await conn.execute(
                        f"DELETE FROM {self._objects_table} WHERE id = $1",
                        existing["id"],
                    )
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO {self._objects_table} (
                        id, profile_id, namespace, object_key, size_bytes, sha256,
                        content_type, metadata, chunk_size, chunk_count
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                    RETURNING *
                    """,
                    object_id,
                    self.profile_id,
                    namespace,
                    key,
                    size_bytes,
                    sha256,
                    content_type,
                    metadata_json,
                    self.chunk_size,
                    len(chunks),
                )
                for index, chunk in enumerate(chunks):
                    await conn.execute(
                        f"""
                        INSERT INTO {self._chunks_table} (object_id, chunk_index, data, size_bytes)
                        VALUES ($1, $2, $3, $4)
                        """,
                        object_id,
                        index,
                        chunk,
                        len(chunk),
                    )
        return self._row_to_stored(row)

    def _row_to_stored(self, row: Mapping[str, Any]) -> StoredObject:
        raw_meta = row.get("metadata")
        if isinstance(raw_meta, str):
            metadata = json.loads(raw_meta or "{}")
        elif isinstance(raw_meta, Mapping):
            metadata = {str(k): str(v) for k, v in raw_meta.items()}
        else:
            metadata = {}
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

    def _optional_public_url(self, key: str, namespace: str) -> str:
        if not self.public_base_url:
            return ""
        full_key = f"{namespace}/{key}" if namespace else key
        return build_public_url(self.public_base_url, full_key)


async def _stream_to_temporary(
    stream: AsyncIterable[bytes],
    max_bytes: int | None,
    expected_sha256: str,
) -> tuple[Path, int, str]:
    descriptor, name = tempfile.mkstemp(prefix="pg-obj-", suffix=".tmp")
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


def _read_file_chunks(path: Path, chunk_size: int) -> list[bytes]:
    chunks: list[bytes] = []
    with path.open("rb") as handle:
        while part := handle.read(chunk_size):
            chunks.append(part)
    return chunks


def _write_chunks_to_file(destination: Path, chunks: list[bytes]) -> tuple[int, str]:
    digest = hashlib.sha256()
    written = 0
    with destination.open("wb") as handle:
        for chunk in chunks:
            written += len(chunk)
            digest.update(chunk)
            handle.write(chunk)
    return written, digest.hexdigest()
