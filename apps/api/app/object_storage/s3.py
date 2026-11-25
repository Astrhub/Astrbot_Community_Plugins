"""S3 / R2 object storage driver implementing the unified ObjectStorageDriver contract."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterable, Mapping
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from .models import (
    ObjectListingEntry,
    ObjectRange,
    ObjectStorageError,
    StorageBackend,
    StorageCapabilities,
    StoredObject,
    build_public_url,
    guess_content_type,
    slice_object_range,
    validate_expected_sha256,
    validate_expected_size,
    validate_namespace,
    validate_object_key,
    validate_profile_id,
    validate_range_params,
)

COPY_CHUNK_SIZE = 1024 * 1024


class S3ObjectStorageDriver:
    backend = StorageBackend.S3

    def __init__(
        self,
        *,
        profile_id: str,
        bucket: str,
        endpoint_url: str = "",
        region: str = "auto",
        access_key_id: str = "",
        secret_access_key: str = "",
        public_base_url: str = "",
        client: Any | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.bucket = str(bucket or "").strip()
        if not self.bucket:
            raise ObjectStorageError("s3_bucket_missing", "S3 bucket is required")
        self.endpoint_url = str(endpoint_url or "").strip()
        self.region = str(region or "auto").strip() or "auto"
        self.access_key_id = str(access_key_id or "")
        self.secret_access_key = str(secret_access_key or "")
        self.public_base_url = str(public_base_url or "").strip().rstrip("/")
        self.client = client or self._create_client()

    def _create_client(self) -> Any:
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url or None,
            region_name=self.region,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
        )

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            conditional_create=True,
            range_read=True,
            public_read=bool(self.public_base_url),
            image_variants=False,
            list_objects=True,
        )

    def _full_key(self, key: str, namespace: str = "") -> tuple[str, str]:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        return normalized_ns, f"{normalized_ns}/{key}" if normalized_ns else key

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
        normalized_ns, object_key = self._full_key(key, namespace)
        resolved_type = guess_content_type(key, content_type)
        extra_metadata = {str(k): str(v) for k, v in (metadata or {}).items()}
        if isinstance(data, (bytes, bytearray, memoryview)):
            payload = bytes(data)
            if max_bytes is not None and len(payload) > max_bytes:
                raise ObjectStorageError("object_too_large", "Object exceeds size limit")
            sha256 = hashlib.sha256(payload).hexdigest()
            validate_expected_sha256(expected_sha256, sha256)
            await asyncio.to_thread(
                self._put_body,
                object_key,
                payload,
                sha256=sha256,
                content_type=resolved_type,
                if_absent=if_absent,
                extra_metadata=extra_metadata,
            )
            size = len(payload)
        else:
            temporary, size, sha256 = await _stream_to_temporary(data, max_bytes, expected_sha256)
            try:
                await asyncio.to_thread(
                    self._put_file,
                    object_key,
                    temporary,
                    sha256=sha256,
                    content_type=resolved_type,
                    if_absent=if_absent,
                    extra_metadata=extra_metadata,
                )
            finally:
                temporary.unlink(missing_ok=True)
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=normalized_ns,
            key=key,
            size_bytes=size,
            sha256=sha256,
            content_type=resolved_type,
            public_url=self._optional_public_url(object_key),
            metadata={"sha256": sha256, **extra_metadata},
        )

    async def stat_object(self, key: str, *, namespace: str = "") -> StoredObject | None:
        normalized_ns, object_key = self._full_key(key, namespace)
        return await asyncio.to_thread(self._head_object, object_key, key, normalized_ns)

    async def read_object(
        self,
        key: str,
        *,
        namespace: str = "",
        max_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> bytes:
        normalized_ns, object_key = self._full_key(key, namespace)
        stored = await asyncio.to_thread(self._head_object, object_key, key, normalized_ns)
        if stored is None:
            raise ObjectStorageError("object_missing", "Object not found")
        if max_bytes is not None and stored.size_bytes > max_bytes:
            raise ObjectStorageError("object_too_large", "Object exceeds limit")
        if expected_sha256 and stored.sha256:
            validate_expected_sha256(expected_sha256, stored.sha256)
        content = await self._get_object_bytes(object_key, max_bytes=max_bytes)
        validate_expected_size(stored.size_bytes, len(content))
        sha256 = hashlib.sha256(content).hexdigest()
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
        validate_range_params(start_byte, max_bytes, max_object_bytes)
        normalized_ns, object_key = self._full_key(key, namespace)
        stored = await asyncio.to_thread(self._head_object, object_key, key, normalized_ns)
        if stored is None:
            raise ObjectStorageError("object_missing", "Object not found")
        if max_object_bytes is not None and stored.size_bytes > max_object_bytes:
            raise ObjectStorageError("object_too_large", "Object exceeds limit")
        validate_expected_size(expected_size_bytes, stored.size_bytes)
        if expected_sha256 and stored.sha256:
            validate_expected_sha256(expected_sha256, stored.sha256)
        end_byte = min(stored.size_bytes, start_byte + max_bytes)
        if start_byte > stored.size_bytes:
            raise ObjectStorageError("object_range_invalid", "Object range is invalid")
        if start_byte == end_byte:
            return ObjectRange(
                content=b"",
                size_bytes=stored.size_bytes,
                sha256=stored.sha256 or expected_sha256,
                content_type=stored.content_type or guess_content_type(key),
                start_byte=start_byte,
                end_byte=end_byte,
                truncated=end_byte < stored.size_bytes,
            )
        if expected_sha256 and not stored.sha256:
            content = await self._get_object_bytes(object_key, max_bytes=max_object_bytes)
            validate_expected_size(expected_size_bytes, len(content))
            sha256 = hashlib.sha256(content).hexdigest()
            validate_expected_sha256(expected_sha256, sha256)
            return slice_object_range(
                content,
                sha256=sha256,
                content_type=stored.content_type or guess_content_type(key),
                start_byte=start_byte,
                max_bytes=max_bytes,
            )
        try:
            response = await asyncio.to_thread(
                self.client.get_object,
                Bucket=self.bucket,
                Key=object_key,
                Range=f"bytes={start_byte}-{end_byte - 1}",
            )
            body = response["Body"]
            try:
                content = await asyncio.to_thread(body.read)
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectStorageError("object_missing", "Object not found") from exc
            raise
        total_size = stored.size_bytes
        content_range = str(response.get("ContentRange") or response.get("content-range") or "")
        if "/" in content_range:
            raw_total = content_range.rsplit("/", 1)[1]
            if raw_total.isdigit():
                total_size = int(raw_total)
        validate_expected_size(expected_size_bytes, total_size)
        expected_length = min(total_size, start_byte + max_bytes) - start_byte
        if len(content) != expected_length:
            raise ObjectStorageError(
                "s3_range_length_mismatch",
                "S3 range response length does not match the requested byte slice",
            )
        return ObjectRange(
            content=content,
            size_bytes=total_size,
            sha256=stored.sha256 or expected_sha256,
            content_type=stored.content_type or guess_content_type(key),
            start_byte=start_byte,
            end_byte=start_byte + len(content),
            truncated=start_byte + len(content) < total_size,
        )

    async def download_to_path(
        self,
        key: str,
        destination: Path,
        *,
        namespace: str = "",
    ) -> StoredObject:
        normalized_ns, object_key = self._full_key(key, namespace)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(
                self.client.download_file,
                self.bucket,
                object_key,
                str(destination),
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectStorageError("object_missing", "Object not found") from exc
            raise
        except KeyError as exc:
            raise ObjectStorageError("object_missing", "Object not found") from exc
        size, sha256 = await asyncio.to_thread(_file_size_and_sha256, destination)
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=normalized_ns,
            key=key,
            size_bytes=size,
            sha256=sha256,
            content_type=guess_content_type(key),
            public_url=self._optional_public_url(object_key),
        )

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
        temporary = _temporary_path(prefix="s3-copy-", suffix=".tmp")
        try:
            source = await self.download_to_path(
                source_key,
                temporary,
                namespace=source_namespace,
            )
            validate_expected_sha256(expected_sha256, source.sha256)
            normalized_dst_ns, target_object_key = self._full_key(target_key, target_namespace)
            resolved_type = guess_content_type(target_key, content_type)
            await asyncio.to_thread(
                self._put_file,
                target_object_key,
                temporary,
                sha256=source.sha256,
                content_type=resolved_type,
                if_absent=if_absent,
                extra_metadata={},
            )
            return StoredObject(
                profile_id=self.profile_id,
                backend=self.backend.value,
                namespace=normalized_dst_ns,
                key=target_key,
                size_bytes=source.size_bytes,
                sha256=source.sha256,
                content_type=resolved_type,
                public_url=self._optional_public_url(target_object_key),
                metadata={"sha256": source.sha256},
            )
        finally:
            temporary.unlink(missing_ok=True)

    async def delete_object(self, key: str, *, namespace: str = "") -> None:
        _, object_key = self._full_key(key, namespace)
        await asyncio.to_thread(
            self.client.delete_object,
            Bucket=self.bucket,
            Key=object_key,
        )

    def public_url(self, key: str, *, namespace: str = "") -> str:
        _, object_key = self._full_key(key, namespace)
        return build_public_url(self.public_base_url, object_key)

    async def list_objects(
        self,
        *,
        namespace: str = "",
        prefix: str = "",
    ) -> list[ObjectListingEntry]:
        normalized_ns = validate_namespace(namespace)
        base_prefix = f"{normalized_ns}/" if normalized_ns else ""
        full_prefix = f"{base_prefix}{prefix}"
        return await asyncio.to_thread(self._list_sync, normalized_ns, base_prefix, full_prefix)

    def _optional_public_url(self, key: str) -> str:
        if not self.public_base_url:
            return ""
        return build_public_url(self.public_base_url, key)

    async def _get_object_bytes(self, object_key: str, *, max_bytes: int | None) -> bytes:
        try:
            response = await asyncio.to_thread(
                self.client.get_object,
                Bucket=self.bucket,
                Key=object_key,
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectStorageError("object_missing", "Object not found") from exc
            raise
        body = response["Body"]
        try:
            if max_bytes is None:
                content = await asyncio.to_thread(body.read)
            else:
                content = await asyncio.to_thread(body.read, max_bytes + 1)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
        if max_bytes is not None and len(content) > max_bytes:
            raise ObjectStorageError("object_too_large", "Object exceeds limit")
        return content

    def _put_file(
        self,
        object_key: str,
        path: Path,
        *,
        sha256: str,
        content_type: str,
        if_absent: bool,
        extra_metadata: Mapping[str, str],
    ) -> None:
        with path.open("rb") as handle:
            self._put_body(
                object_key,
                handle,
                sha256=sha256,
                content_type=content_type,
                if_absent=if_absent,
                extra_metadata=extra_metadata,
            )

    def _put_body(
        self,
        object_key: str,
        body: Any,
        *,
        sha256: str,
        content_type: str,
        if_absent: bool,
        extra_metadata: Mapping[str, str],
    ) -> None:
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": object_key,
            "Body": body,
            "ContentType": content_type,
            "Metadata": {**extra_metadata, "sha256": sha256},
        }
        if if_absent:
            kwargs["IfNoneMatch"] = "*"
        try:
            self.client.put_object(**kwargs)
        except ClientError as exc:
            if not if_absent or not _is_precondition_conflict(exc):
                raise
            existing = self._head_object(object_key, object_key, "")
            if not existing or existing.sha256 != sha256:
                raise ObjectStorageError(
                    "object_key_conflict", "Object already exists with different content"
                ) from exc

    def _head_object(
        self,
        object_key: str,
        logical_key: str,
        namespace: str,
    ) -> StoredObject | None:
        try:
            result = self.client.head_object(Bucket=self.bucket, Key=object_key)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise
        metadata = dict(result.get("Metadata") or {})
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=namespace,
            key=logical_key,
            size_bytes=int(result.get("ContentLength") or 0),
            sha256=str(metadata.get("sha256") or ""),
            content_type=str(result.get("ContentType") or guess_content_type(logical_key)),
            public_url=self._optional_public_url(object_key),
            metadata=metadata,
        )

    def _list_sync(
        self,
        namespace: str,
        base_prefix: str,
        full_prefix: str,
    ) -> list[ObjectListingEntry]:
        entries: list[ObjectListingEntry] = []
        continuation_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket}
            if full_prefix:
                kwargs["Prefix"] = full_prefix
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                raw_key = str(item.get("Key") or "")
                if not raw_key or not raw_key.startswith(base_prefix):
                    continue
                logical_key = raw_key[len(base_prefix) :]
                if not logical_key:
                    continue
                entries.append(
                    ObjectListingEntry(
                        namespace=namespace,
                        key=logical_key,
                        size_bytes=int(item.get("Size") or 0),
                        content_type=guess_content_type(logical_key),
                    )
                )
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                break
        entries.sort(key=lambda entry: entry.key)
        return entries


def _is_not_found(exc: ClientError) -> bool:
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return status == 404 or code in {"404", "NoSuchKey", "NotFound"}


def _is_precondition_conflict(exc: ClientError) -> bool:
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return status in {409, 412} or code in {"ConditionalRequestConflict", "PreconditionFailed"}


def _temporary_path(*, prefix: str, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(descriptor)
    return Path(name)


async def _stream_to_temporary(
    stream: AsyncIterable[bytes],
    max_bytes: int | None,
    expected_sha256: str,
) -> tuple[Path, int, str]:
    temporary = _temporary_path(prefix="s3-upload-", suffix=".tmp")
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


def _file_size_and_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()
