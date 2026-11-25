"""WebDAV object storage driver with SSRF guards, capability probing, and CAS fallback."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import re
import socket
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

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

DEFAULT_WEBDAV_TIMEOUT_SECONDS = 30.0
STREAM_CHUNK_SIZE = 256 * 1024
PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
ALLOWED_AUTH_TYPES = frozenset({"basic", "bearer", "none"})
CAS_SUFFIX_PATTERN = re.compile(r"^(.+)--([a-f0-9]{12})$")
CONTENT_RANGE_PATTERN = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")
PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop>"
    "<d:resourcetype/>"
    "<d:getcontentlength/>"
    "<d:getcontenttype/>"
    "<d:getetag/>"
    "</d:prop>"
    "</d:propfind>"
)


@dataclass(frozen=True, slots=True)
class WebDavProbeResult:
    """Result of probing real WebDAV server capabilities on a dedicated test object."""

    write: bool
    read: bool
    range_read: bool
    conditional_create: bool
    overwrite_protection: bool
    delete: bool
    public_url: bool
    message: str


def validate_webdav_base_url(
    value: Any,
    *,
    allow_private_network: bool = False,
) -> str:
    """Validate WebDAV server base URL without embedded credentials or unsafe private hosts."""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ObjectStorageError("webdav_base_url_missing", "WebDAV base URL is required")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ObjectStorageError("webdav_base_url_invalid", "Invalid WebDAV base URL") from exc
    allowed_schemes = {"https", "http"} if allow_private_network else {"https"}
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        if parsed.scheme == "http" and not allow_private_network:
            raise ObjectStorageError(
                "webdav_https_required",
                "WebDAV base URL must use HTTPS unless allow_private_network is explicitly enabled",
            )
        raise ObjectStorageError(
            "webdav_base_url_invalid",
            "WebDAV base URL must be a valid absolute URL",
        )
    if parsed.username or parsed.password:
        raise ObjectStorageError(
            "webdav_credentials_in_url_forbidden",
            "WebDAV credentials must not be embedded in the URL",
        )
    if parsed.query or parsed.fragment:
        raise ObjectStorageError(
            "webdav_base_url_invalid",
            "WebDAV base URL cannot contain query parameters or fragments",
        )
    if not allow_private_network:
        _reject_private_host(parsed.hostname)
    return raw


def validate_webdav_root_path(value: Any) -> str:
    """Normalize a safe relative WebDAV root directory prefix (empty means server root)."""
    raw = str(value or "").strip().strip("/")
    if not raw:
        return ""
    if "\\" in raw or any(ord(ch) < 32 for ch in raw):
        raise ObjectStorageError("webdav_root_path_invalid", "Invalid WebDAV root path")
    parts = raw.split("/")
    if len(parts) > 16:
        raise ObjectStorageError("webdav_root_path_invalid", "WebDAV root path is too deep")
    for part in parts:
        if not part or part in {".", ".."} or len(part) > 128:
            raise ObjectStorageError("webdav_root_path_invalid", "Invalid WebDAV root path segment")
    return "/".join(parts)


def validate_webdav_auth_type(value: Any) -> str:
    cleaned = str(value or "basic").strip().lower() or "basic"
    if cleaned not in ALLOWED_AUTH_TYPES:
        raise ObjectStorageError("webdav_auth_type_invalid", f"Unsupported WebDAV auth: {cleaned}")
    return cleaned


def content_addressed_key(key: str, sha256: str) -> str:
    """Derive a deterministic content-hashed key for WebDAV servers without conditional PUT."""
    validate_object_key(key)
    digest12 = sha256.strip().lower()[:12]
    if len(digest12) != 12 or any(ch not in "0123456789abcdef" for ch in digest12):
        raise ObjectStorageError("invalid_sha256", "Expected a valid SHA-256 digest")
    parent, sep, name = key.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        base_stem = name
        suffix = ""
    else:
        base_stem = stem
        suffix = f".{ext}"
    existing_match = CAS_SUFFIX_PATTERN.fullmatch(base_stem)
    if existing_match and existing_match.group(2) == digest12:
        return key
    hashed_name = f"{base_stem}--{digest12}{suffix}"
    return f"{parent}{sep}{hashed_name}" if sep else hashed_name


def _logical_stem_and_ext(filename: str) -> tuple[str, str]:
    stem, dot, ext = filename.rpartition(".")
    if not dot or not stem:
        return filename, ""
    return stem, f".{ext}"


def _matches_logical_key(candidate_key: str, logical_key: str) -> str | None:
    """Return '' for exact match, 12-hex digest for CAS sibling match, or None."""
    if candidate_key == logical_key:
        return ""
    cand_parent, _, cand_name = candidate_key.rpartition("/")
    log_parent, _, log_name = logical_key.rpartition("/")
    if cand_parent != log_parent:
        return None
    log_stem, log_ext = _logical_stem_and_ext(log_name)
    cand_stem, cand_ext = _logical_stem_and_ext(cand_name)
    if cand_ext != log_ext:
        return None
    match = CAS_SUFFIX_PATTERN.fullmatch(cand_stem)
    if match and match.group(1) == log_stem:
        return match.group(2)
    return None


def _logical_key_from_cas(candidate_key: str) -> str:
    parent, _, name = candidate_key.rpartition("/")
    stem, ext = _logical_stem_and_ext(name)
    match = CAS_SUFFIX_PATTERN.fullmatch(stem)
    if match is None:
        return candidate_key
    logical_name = f"{match.group(1)}{ext}"
    return f"{parent}/{logical_name}" if parent else logical_name


def _reject_private_host(hostname: str) -> None:
    lowered = hostname.strip().lower().rstrip(".")
    if lowered == "localhost" or any(lowered.endswith(sfx) for sfx in PRIVATE_HOST_SUFFIXES):
        raise ObjectStorageError(
            "webdav_private_host_forbidden",
            "Private or loopback WebDAV hosts require explicit allow_private_network=true",
        )
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return
    if not address.is_global:
        raise ObjectStorageError(
            "webdav_private_host_forbidden",
            "Non-global IP WebDAV hosts require explicit allow_private_network=true",
        )


async def _verify_dns_global(hostname: str, port: int) -> None:
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            0,
            socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ObjectStorageError("webdav_dns_failed", f"WebDAV host lookup failed: {exc}") from exc
    if not records:
        raise ObjectStorageError("webdav_dns_failed", "WebDAV host did not resolve")
    for record in records[:64]:
        addr_str = str(record[4][0])
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError as exc:
            raise ObjectStorageError(
                "webdav_private_host_forbidden", "Invalid resolved IP"
            ) from exc
        if not addr.is_global:
            raise ObjectStorageError(
                "webdav_private_host_forbidden",
                "WebDAV hostname resolved to a private or non-global IP address",
            )


class WebDavObjectStorageDriver:
    """ObjectStorageDriver backed by a standard WebDAV server over httpx."""

    backend = StorageBackend.WEBDAV

    def __init__(
        self,
        *,
        profile_id: str,
        base_url: str,
        root_path: str = "",
        auth_type: str = "basic",
        username: str = "",
        password: str = "",
        token: str = "",
        public_base_url: str = "",
        allow_private_network: bool = False,
        verify_tls: bool = True,
        conditional_create_supported: bool = True,
        range_read_supported: bool = True,
        timeout_seconds: float = DEFAULT_WEBDAV_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.allow_private_network = bool(allow_private_network)
        self.base_url = validate_webdav_base_url(
            base_url,
            allow_private_network=self.allow_private_network,
        )
        self.root_path = validate_webdav_root_path(root_path)
        self.auth_type = validate_webdav_auth_type(auth_type)
        self._username = str(username or "")
        self._password = str(password or "")
        self._token = str(token or "")
        self.public_base_url = str(public_base_url or "").strip().rstrip("/")
        self.verify_tls = bool(verify_tls)
        self.conditional_create_supported = bool(conditional_create_supported)
        self.range_read_supported = bool(range_read_supported)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._external_client = client is not None
        self._client = client
        self._created_dirs: set[str] = set()
        self._cas_physical_keys: dict[tuple[str, str], str] = {}
        self._parsed_base = urlsplit(self.base_url)

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            conditional_create=self.conditional_create_supported,
            range_read=self.range_read_supported,
            public_read=bool(self.public_base_url),
            image_variants=False,
            list_objects=True,
        )

    async def close(self) -> None:
        if not self._external_client and self._client is not None:
            await self._client.aclose()
            self._client = None

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
        temporary, size_bytes, sha256 = await _stage_input_to_temp(data, max_bytes, expected_sha256)
        try:
            if if_absent and not self.conditional_create_supported:
                return await self._put_with_cas_fallback(
                    key=key,
                    namespace=normalized_ns,
                    temporary=temporary,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    content_type=resolved_type,
                    metadata=metadata,
                )
            if if_absent:
                existing = await self._check_existing_key(
                    key=key,
                    namespace=normalized_ns,
                    expected_sha256=sha256,
                )
                if existing is not None:
                    return existing

            await self._ensure_parent_dirs(key, normalized_ns)
            url = self._url_for_object(key, normalized_ns)
            headers = self._auth_headers(
                extra={
                    "Content-Type": resolved_type,
                    "Content-Length": str(size_bytes),
                    **({"If-None-Match": "*"} if if_absent else {}),
                }
            )
            response = await self._request(
                "PUT",
                url,
                headers=headers,
                content=_file_chunk_stream(temporary),
            )
            if response.status_code == 412 and if_absent:
                existing = await self._check_existing_key(
                    key=key,
                    namespace=normalized_ns,
                    expected_sha256=sha256,
                )
                if existing is not None:
                    return existing
                raise ObjectStorageError(
                    "object_key_conflict",
                    "Object already exists with different content",
                )
            if response.status_code not in {200, 201, 204}:
                raise _status_error("webdav_put_failed", response)
            if not self.conditional_create_supported:
                await self._verify_remote_digest(
                    key=key,
                    namespace=normalized_ns,
                    expected_size=size_bytes,
                    expected_sha256=sha256,
                )
            return StoredObject(
                profile_id=self.profile_id,
                backend=self.backend.value,
                namespace=normalized_ns,
                key=key,
                size_bytes=size_bytes,
                sha256=sha256,
                content_type=resolved_type,
                public_url=self._optional_public_url(key, normalized_ns),
                metadata={str(k): str(v) for k, v in (metadata or {}).items()},
            )
        finally:
            temporary.unlink(missing_ok=True)

    async def _resolve_physical_key(
        self,
        key: str,
        namespace: str,
        *,
        expected_sha256: str = "",
    ) -> str:
        if self.conditional_create_supported:
            return key
        cache_key = (namespace, key)
        cached = self._cas_physical_keys.get(cache_key)
        if cached is not None:
            return cached

        exact_url = self._url_for_object(key, namespace)
        exact_response = await self._request("HEAD", exact_url, headers=self._auth_headers())
        if exact_response.status_code in {200, 204}:
            self._cas_physical_keys[cache_key] = key
            return key
        if exact_response.status_code != 404:
            raise _status_error("webdav_stat_failed", exact_response)

        parent_key, _, _ = key.rpartition("/")
        parent_rel = _join_segments(self.root_path, namespace, parent_key)
        base_rel = _join_segments(self.root_path, namespace)
        candidates: list[str] = []
        siblings = await self._propfind_depth_one(parent_rel)
        expected_prefix = expected_sha256.strip().lower()[:12]
        for rel_path, is_collection, _, _ in siblings:
            if is_collection:
                continue
            candidate_key = _strip_prefix_segments(rel_path, base_rel)
            match_kind = _matches_logical_key(candidate_key, key)
            if not match_kind:
                continue
            if expected_prefix and match_kind != expected_prefix:
                continue
            candidates.append(candidate_key)
        if len(candidates) > 1:
            raise ObjectStorageError(
                "object_key_conflict",
                "Multiple WebDAV CAS objects match the logical key",
            )
        if candidates:
            self._cas_physical_keys[cache_key] = candidates[0]
            return candidates[0]
        return key

    async def stat_object(self, key: str, *, namespace: str = "") -> StoredObject | None:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        physical_key = await self._resolve_physical_key(key, normalized_ns)
        url = self._url_for_object(physical_key, normalized_ns)
        response = await self._request("HEAD", url, headers=self._auth_headers())
        if response.status_code == 404:
            return None
        if response.status_code not in {200, 204}:
            raise _status_error("webdav_stat_failed", response)
        size_raw = response.headers.get("content-length", "0").strip()
        size_bytes = int(size_raw) if size_raw.isdigit() else 0
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        sha_header = _extract_sha256_header(response.headers)
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=normalized_ns,
            key=key,
            size_bytes=size_bytes,
            sha256=sha_header,
            content_type=content_type or guess_content_type(key),
            public_url=self._optional_public_url(physical_key, normalized_ns),
        )

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
        physical_key = await self._resolve_physical_key(
            key,
            normalized_ns,
            expected_sha256=expected_sha256,
        )
        url = self._url_for_object(physical_key, normalized_ns)
        await self._guard_runtime_dns()
        client = await self._ensure_client()
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        try:
            async with client.stream(
                "GET",
                url,
                headers=self._auth_headers(),
                follow_redirects=False,
            ) as response:
                if response.status_code == 404:
                    raise ObjectStorageError("object_missing", "Object not found")
                if response.status_code != 200:
                    raise _status_error("webdav_get_failed", response)
                async for chunk in response.aiter_bytes(STREAM_CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise ObjectStorageError("object_too_large", "Object exceeds limit")
                    digest.update(chunk)
                    chunks.append(chunk)
        except ObjectStorageError:
            raise
        except httpx.HTTPError as exc:
            raise ObjectStorageError("webdav_network_error", f"WebDAV GET failed: {exc}") from exc
        content = b"".join(chunks)
        validate_expected_sha256(expected_sha256, digest.hexdigest())
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
        stat = await self.stat_object(key, namespace=normalized_ns)
        if stat is None:
            raise ObjectStorageError("object_missing", "Object not found")
        if max_object_bytes is not None and stat.size_bytes > max_object_bytes:
            raise ObjectStorageError("object_too_large", "Object exceeds limit")
        validate_expected_size(expected_size_bytes, stat.size_bytes)
        if start_byte > stat.size_bytes:
            raise ObjectStorageError("object_range_invalid", "Object range is invalid")
        if stat.sha256 and expected_sha256:
            validate_expected_sha256(expected_sha256, stat.sha256)
        end_byte = min(stat.size_bytes, start_byte + max_bytes)
        if start_byte == end_byte:
            return ObjectRange(
                content=b"",
                size_bytes=stat.size_bytes,
                sha256=stat.sha256 or expected_sha256,
                content_type=stat.content_type or guess_content_type(key),
                start_byte=start_byte,
                end_byte=end_byte,
                truncated=end_byte < stat.size_bytes,
            )

        physical_key = await self._resolve_physical_key(
            key,
            normalized_ns,
            expected_sha256=expected_sha256,
        )
        url = self._url_for_object(physical_key, normalized_ns)
        range_header = f"bytes={start_byte}-{end_byte - 1}"
        response = await self._request(
            "GET",
            url,
            headers=self._auth_headers(extra={"Range": range_header}),
        )
        if response.status_code == 404:
            raise ObjectStorageError("object_missing", "Object not found")
        if response.status_code == 416:
            raise ObjectStorageError("object_range_invalid", "Object range is invalid")
        if response.status_code == 206:
            body = response.content
            total_size = _parse_content_range_total(
                response.headers.get("content-range", ""),
                fallback=stat.size_bytes,
            )
            if max_object_bytes is not None and total_size > max_object_bytes:
                raise ObjectStorageError("object_too_large", "Object exceeds limit")
            validate_expected_size(expected_size_bytes, total_size)
            expected_len = min(total_size, start_byte + max_bytes) - start_byte
            if len(body) != expected_len:
                raise ObjectStorageError(
                    "webdav_range_length_mismatch",
                    "WebDAV range response length does not match requested byte slice",
                )
            header_sha = _extract_sha256_header(response.headers) or stat.sha256
            if header_sha and expected_sha256:
                validate_expected_sha256(expected_sha256, header_sha)
            resolved_end = start_byte + len(body)
            return ObjectRange(
                content=body,
                size_bytes=total_size,
                sha256=header_sha or expected_sha256,
                content_type=stat.content_type or guess_content_type(key),
                start_byte=start_byte,
                end_byte=resolved_end,
                truncated=resolved_end < total_size,
            )
        if response.status_code == 200:
            body = response.content
            if max_object_bytes is not None and len(body) > max_object_bytes:
                raise ObjectStorageError("object_too_large", "Object exceeds limit")
            validate_expected_size(expected_size_bytes, len(body))
            sha256 = hashlib.sha256(body).hexdigest()
            validate_expected_sha256(expected_sha256, sha256)
            return slice_object_range(
                body,
                sha256=sha256,
                content_type=stat.content_type or guess_content_type(key),
                start_byte=start_byte,
                max_bytes=max_bytes,
            )
        raise _status_error("webdav_range_failed", response)

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
        physical_key = await self._resolve_physical_key(key, normalized_ns)
        url = self._url_for_object(physical_key, normalized_ns)
        await self._guard_runtime_dns()
        client = await self._ensure_client()
        descriptor, temp_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=".webdav-dl-",
            suffix=".tmp",
        )
        os.close(descriptor)
        temporary = Path(temp_name)
        digest = hashlib.sha256()
        written = 0
        content_type = guess_content_type(key)
        try:
            async with client.stream(
                "GET",
                url,
                headers=self._auth_headers(),
                follow_redirects=False,
            ) as response:
                if response.status_code == 404:
                    raise ObjectStorageError("object_missing", "Object not found")
                if response.status_code != 200:
                    raise _status_error("webdav_get_failed", response)
                resp_ct = response.headers.get("content-type", "").split(";")[0].strip()
                if resp_ct:
                    content_type = resp_ct
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes(STREAM_CHUNK_SIZE):
                        if not chunk:
                            continue
                        written += len(chunk)
                        digest.update(chunk)
                        await asyncio.to_thread(handle.write, chunk)
            os.replace(temporary, destination)
        except ObjectStorageError:
            temporary.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            raise ObjectStorageError("webdav_network_error", f"WebDAV GET failed: {exc}") from exc
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=normalized_ns,
            key=key,
            size_bytes=written,
            sha256=digest.hexdigest(),
            content_type=content_type,
            public_url=self._optional_public_url(physical_key, normalized_ns),
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
        validate_object_key(source_key)
        validate_object_key(target_key)
        descriptor, temp_name = tempfile.mkstemp(prefix="webdav-copy-", suffix=".tmp")
        os.close(descriptor)
        temporary = Path(temp_name)
        try:
            source = await self.download_to_path(
                source_key,
                temporary,
                namespace=source_namespace,
            )
            validate_expected_sha256(expected_sha256, source.sha256)
            return await self.put_object(
                target_key,
                _file_chunk_stream(temporary),
                namespace=target_namespace,
                content_type=content_type or source.content_type,
                expected_sha256=source.sha256,
                if_absent=if_absent,
            )
        finally:
            temporary.unlink(missing_ok=True)

    async def delete_object(self, key: str, *, namespace: str = "") -> None:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        physical_key = await self._resolve_physical_key(key, normalized_ns)
        url = self._url_for_object(physical_key, normalized_ns)
        response = await self._request("DELETE", url, headers=self._auth_headers())
        if response.status_code in {200, 202, 204, 404}:
            self._cas_physical_keys.pop((normalized_ns, key), None)
            return
        raise _status_error("webdav_delete_failed", response)

    def public_url(self, key: str, *, namespace: str = "") -> str:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        physical_key = self._cas_physical_keys.get((normalized_ns, key), key)
        full_key = f"{normalized_ns}/{physical_key}" if normalized_ns else physical_key
        return build_public_url(self.public_base_url, full_key)

    def public_url_for_sha256(
        self,
        key: str,
        *,
        namespace: str = "",
        sha256: str,
    ) -> str:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        physical_key = (
            key if self.conditional_create_supported else content_addressed_key(key, sha256)
        )
        self._cas_physical_keys[(normalized_ns, key)] = physical_key
        full_key = f"{normalized_ns}/{physical_key}" if normalized_ns else physical_key
        return build_public_url(self.public_base_url, full_key)

    async def list_objects(
        self,
        *,
        namespace: str = "",
        prefix: str = "",
    ) -> list[ObjectListingEntry]:
        normalized_ns = validate_namespace(namespace)
        base_rel = _join_segments(self.root_path, normalized_ns)
        files = await self._propfind_recursive(base_rel)
        entries: list[ObjectListingEntry] = []
        for rel_path, size_bytes, content_type in files:
            key = _strip_prefix_segments(rel_path, base_rel)
            if not self.conditional_create_supported:
                key = _logical_key_from_cas(key)
            if not key:
                continue
            if prefix and not key.startswith(prefix):
                continue
            entries.append(
                ObjectListingEntry(
                    namespace=normalized_ns,
                    key=key,
                    size_bytes=size_bytes,
                    content_type=content_type or guess_content_type(key),
                )
            )
        entries.sort(key=lambda item: item.key)
        return entries

    async def probe_capabilities(self, probe_prefix: str = "__healthcheck__/") -> WebDavProbeResult:
        """Exercise real WebDAV create, read, 206 Range, If-None-Match overwrite protection, and delete."""
        token = os.urandom(8).hex()
        probe_key = f"{probe_prefix}probe-{token}.txt"
        payload_1 = b"astrhub-storage-webdav-probe-0123456789"
        payload_2 = b"astrhub-storage-webdav-overwrite-check!"
        sha_1 = hashlib.sha256(payload_1).hexdigest()
        checks = {
            "write": False,
            "read": False,
            "range_read": False,
            "conditional_create": False,
            "overwrite_protection": False,
            "delete": False,
            "public_url": False,
        }
        message = "WebDAV 能力探测与读写校验通过"
        cleanup_keys: list[str] = [probe_key]
        try:
            await self._ensure_parent_dirs(probe_key, "")
            probe_url = self._url_for_object(probe_key, "")

            # 1. Create via PUT with If-None-Match: *
            put_res = await self._request(
                "PUT",
                probe_url,
                headers=self._auth_headers(
                    extra={"Content-Type": "text/plain; charset=utf-8", "If-None-Match": "*"}
                ),
                content=payload_1,
            )
            if put_res.status_code not in {200, 201, 204}:
                raise _status_error("webdav_probe_write_failed", put_res)
            checks["write"] = True

            # 2. Read back and verify SHA-256
            read_back = await self.read_object(probe_key, expected_sha256=sha_1)
            checks["read"] = read_back == payload_1

            # 3. Verify true HTTP 206 Range support
            range_res = await self._request(
                "GET",
                probe_url,
                headers=self._auth_headers(extra={"Range": "bytes=8-14"}),
            )
            checks["range_read"] = range_res.status_code == 206 and range_res.content == b"storage"
            self.range_read_supported = checks["range_read"]

            # 4. Probe conditional overwrite protection (PUT same key with If-None-Match: *)
            overwrite_res = await self._request(
                "PUT",
                probe_url,
                headers=self._auth_headers(
                    extra={"Content-Type": "text/plain; charset=utf-8", "If-None-Match": "*"}
                ),
                content=payload_2,
            )
            if overwrite_res.status_code == 412:
                verify_untouched = await self.read_object(probe_key, expected_sha256=sha_1)
                checks["conditional_create"] = verify_untouched == payload_1
                checks["overwrite_protection"] = checks["conditional_create"]
                self.conditional_create_supported = checks["conditional_create"]
            else:
                # Server ignored If-None-Match; switch driver to CAS fallback and verify conflict check!
                checks["conditional_create"] = False
                self.conditional_create_supported = False
                cas_probe_key = f"{probe_prefix}cas-{token}.txt"
                stored_cas = await self.put_object(
                    cas_probe_key,
                    payload_1,
                    content_type="text/plain; charset=utf-8",
                    if_absent=True,
                )
                cleanup_keys.append(stored_cas.key)
                try:
                    await self.put_object(
                        cas_probe_key,
                        payload_2,
                        content_type="text/plain; charset=utf-8",
                        if_absent=True,
                    )
                    checks["overwrite_protection"] = False
                except ObjectStorageError as conflict_exc:
                    checks["overwrite_protection"] = conflict_exc.code == "object_key_conflict"

            # 5. Public URL check (optional; empty means same-origin delivery)
            if self.public_base_url:
                url = self.public_url(probe_key)
                checks["public_url"] = url.startswith(("http://", "https://"))
            else:
                checks["public_url"] = True
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
        finally:
            all_deleted = True
            for key_to_clean in cleanup_keys:
                try:
                    await self.delete_object(key_to_clean)
                    if await self.stat_object(key_to_clean) is not None:
                        all_deleted = False
                except Exception as cleanup_exc:
                    all_deleted = False
                    if message == "WebDAV 能力探测与读写校验通过":
                        message = f"测试对象清理失败：{cleanup_exc}"
            checks["delete"] = all_deleted

        if not (
            checks["write"]
            and checks["read"]
            and checks["range_read"]
            and checks["overwrite_protection"]
            and checks["delete"]
            and checks["public_url"]
        ):
            if message == "WebDAV 能力探测与读写校验通过":
                failed = [k for k, v in checks.items() if not v and k != "conditional_create"]
                message = f"WebDAV 能力校验未通过：{', '.join(failed)}"
        elif not checks["conditional_create"]:
            message = "WebDAV 校验通过（服务端不支持 If-None-Match，已自动启用内容哈希防覆盖模式）"

        return WebDavProbeResult(
            write=checks["write"],
            read=checks["read"],
            range_read=checks["range_read"],
            conditional_create=checks["conditional_create"],
            overwrite_protection=checks["overwrite_protection"],
            delete=checks["delete"],
            public_url=checks["public_url"],
            message=message,
        )

    async def _put_with_cas_fallback(
        self,
        *,
        key: str,
        namespace: str,
        temporary: Path,
        size_bytes: int,
        sha256: str,
        content_type: str,
        metadata: Mapping[str, str] | None,
    ) -> StoredObject:
        parent_key, _, _ = key.rpartition("/")
        parent_rel = _join_segments(self.root_path, namespace, parent_key)
        base_rel = _join_segments(self.root_path, namespace)

        # Check sibling objects in the parent directory so same logical name with different bytes conflicts.
        siblings = await self._propfind_depth_one(parent_rel)
        for rel_path, is_collection, _, _ in siblings:
            if is_collection:
                continue
            sibling_key = _strip_prefix_segments(rel_path, base_rel)
            if not sibling_key:
                continue
            match_kind = _matches_logical_key(sibling_key, key)
            if match_kind is None:
                continue
            existing_bytes = await self.read_object(sibling_key, namespace=namespace)
            existing_sha = hashlib.sha256(existing_bytes).hexdigest()
            if existing_sha == sha256:
                self._cas_physical_keys[(namespace, key)] = sibling_key
                return StoredObject(
                    profile_id=self.profile_id,
                    backend=self.backend.value,
                    namespace=namespace,
                    key=key,
                    size_bytes=len(existing_bytes),
                    sha256=existing_sha,
                    content_type=content_type,
                    public_url=self._optional_public_url(sibling_key, namespace),
                    metadata={str(k): str(v) for k, v in (metadata or {}).items()},
                )
            raise ObjectStorageError(
                "object_key_conflict",
                "Object already exists with different content",
            )

        actual_key = content_addressed_key(key, sha256)
        await self._ensure_parent_dirs(actual_key, namespace)
        url = self._url_for_object(actual_key, namespace)
        response = await self._request(
            "PUT",
            url,
            headers=self._auth_headers(
                extra={
                    "Content-Type": content_type,
                    "Content-Length": str(size_bytes),
                }
            ),
            content=_file_chunk_stream(temporary),
        )
        if response.status_code not in {200, 201, 204}:
            raise _status_error("webdav_put_failed", response)
        await self._verify_remote_digest(
            key=actual_key,
            namespace=namespace,
            expected_size=size_bytes,
            expected_sha256=sha256,
        )
        self._cas_physical_keys[(namespace, key)] = actual_key
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=namespace,
            key=key,
            size_bytes=size_bytes,
            sha256=sha256,
            content_type=content_type,
            public_url=self._optional_public_url(actual_key, namespace),
            metadata={str(k): str(v) for k, v in (metadata or {}).items()},
        )

    async def _check_existing_key(
        self,
        *,
        key: str,
        namespace: str,
        expected_sha256: str,
    ) -> StoredObject | None:
        stat = await self.stat_object(key, namespace=namespace)
        if stat is None:
            return None
        existing_bytes = await self.read_object(key, namespace=namespace)
        existing_sha = hashlib.sha256(existing_bytes).hexdigest()
        if existing_sha == expected_sha256:
            return StoredObject(
                profile_id=self.profile_id,
                backend=self.backend.value,
                namespace=namespace,
                key=key,
                size_bytes=len(existing_bytes),
                sha256=existing_sha,
                content_type=stat.content_type or guess_content_type(key),
                public_url=self._optional_public_url(key, namespace),
            )
        raise ObjectStorageError(
            "object_key_conflict",
            "Object already exists with different content",
        )

    async def _verify_remote_digest(
        self,
        *,
        key: str,
        namespace: str,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        read_back = await self.read_object(key, namespace=namespace)
        validate_expected_size(expected_size, len(read_back))
        validate_expected_sha256(expected_sha256, hashlib.sha256(read_back).hexdigest())

    async def _ensure_parent_dirs(self, key: str, namespace: str) -> None:
        parent_key, _, _ = key.rpartition("/")
        full_dir = _join_segments(self.root_path, namespace, parent_key)
        if not full_dir:
            return
        segments = full_dir.split("/")
        accumulated = ""
        for segment in segments:
            accumulated = f"{accumulated}/{segment}" if accumulated else segment
            if accumulated in self._created_dirs:
                continue
            url = self._url_for_rel_path(accumulated, trailing_slash=True)
            response = await self._request("MKCOL", url, headers=self._auth_headers())
            if response.status_code in {200, 201, 204, 301, 405}:
                self._created_dirs.add(accumulated)
                continue
            raise _status_error("webdav_mkcol_failed", response)

    async def _propfind_recursive(self, start_rel: str) -> list[tuple[str, int, str]]:
        """List files under start_rel using Depth: 1 BFS so servers rejecting Depth: infinity work."""
        results: list[tuple[str, int, str]] = []
        queue: list[str] = [start_rel]
        visited: set[str] = set()
        while queue:
            current = queue.pop(0)
            norm = current.strip("/")
            if norm in visited:
                continue
            visited.add(norm)
            entries = await self._propfind_depth_one(norm)
            for rel_path, is_collection, size_bytes, content_type in entries:
                if is_collection:
                    if rel_path != norm and rel_path not in visited:
                        queue.append(rel_path)
                else:
                    results.append((rel_path, size_bytes, content_type))
        return results

    async def _propfind_depth_one(
        self,
        rel_dir: str,
    ) -> list[tuple[str, bool, int, str]]:
        url = self._url_for_rel_path(rel_dir, trailing_slash=True)
        response = await self._request(
            "PROPFIND",
            url,
            headers=self._auth_headers(
                extra={
                    "Depth": "1",
                    "Content-Type": "application/xml; charset=utf-8",
                }
            ),
            content=PROPFIND_BODY.encode("utf-8"),
        )
        if response.status_code == 404:
            return []
        if response.status_code not in {200, 207}:
            raise _status_error("webdav_propfind_failed", response)
        return _parse_propfind_xml(
            response.text,
            base_url_path=self._parsed_base.path,
            target_dir=rel_dir.strip("/"),
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                verify=self.verify_tls,
                follow_redirects=False,
            )
        return self._client

    async def _guard_runtime_dns(self) -> None:
        if self.allow_private_network or self._external_client:
            return
        hostname = self._parsed_base.hostname or ""
        _reject_private_host(hostname)
        port = self._parsed_base.port or (443 if self._parsed_base.scheme == "https" else 80)
        await _verify_dns_global(hostname, port)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes | AsyncIterable[bytes] | None = None,
    ) -> httpx.Response:
        await self._guard_runtime_dns()
        client = await self._ensure_client()
        try:
            return await client.request(
                method,
                url,
                headers=headers,
                content=content,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ObjectStorageError(
                "webdav_network_error",
                f"WebDAV {method} request failed: {exc.__class__.__name__}",
            ) from exc

    def _auth_headers(self, *, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.auth_type == "basic" and (self._username or self._password):
            raw = f"{self._username}:{self._password}".encode("utf-8")
            headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"
        elif self.auth_type == "bearer" and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if extra:
            headers.update(extra)
        return headers

    def _url_for_object(self, key: str, namespace: str) -> str:
        rel = _join_segments(self.root_path, namespace, key)
        return self._url_for_rel_path(rel, trailing_slash=False)

    def _url_for_rel_path(self, rel_path: str, *, trailing_slash: bool) -> str:
        cleaned = rel_path.strip("/")
        if not cleaned:
            return f"{self.base_url}/" if trailing_slash else self.base_url
        encoded = "/".join(quote(part, safe="") for part in cleaned.split("/"))
        suffix = "/" if trailing_slash else ""
        return f"{self.base_url}/{encoded}{suffix}"

    def _optional_public_url(self, key: str, namespace: str) -> str:
        if not self.public_base_url:
            return ""
        full_key = f"{namespace}/{key}" if namespace else key
        return build_public_url(self.public_base_url, full_key)


def _join_segments(*parts: str) -> str:
    cleaned = [part.strip("/") for part in parts if part and part.strip("/")]
    return "/".join(cleaned)


def _strip_prefix_segments(rel_path: str, prefix: str) -> str:
    norm_path = rel_path.strip("/")
    norm_prefix = prefix.strip("/")
    if not norm_prefix:
        return norm_path
    if norm_path == norm_prefix:
        return ""
    if norm_path.startswith(f"{norm_prefix}/"):
        return norm_path[len(norm_prefix) + 1 :]
    return ""


def _extract_sha256_header(headers: Mapping[str, str]) -> str:
    for name in ("x-checksum-sha256", "x-sha256", "x-amz-meta-sha256"):
        value = str(headers.get(name) or "").strip().lower()
        if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value):
            return value
    return ""


def _parse_content_range_total(header_value: str, *, fallback: int) -> int:
    match = CONTENT_RANGE_PATTERN.fullmatch(header_value.strip())
    if not match or match.group(3) == "*":
        return fallback
    return int(match.group(3))


def _status_error(code: str, response: httpx.Response) -> ObjectStorageError:
    status = response.status_code
    if status in {401, 403}:
        return ObjectStorageError("webdav_auth_failed", f"WebDAV rejected credentials ({status})")
    if status == 404:
        return ObjectStorageError("object_missing", "WebDAV resource not found")
    return ObjectStorageError(code, f"WebDAV request returned HTTP {status}")


def _parse_propfind_xml(
    xml_text: str,
    *,
    base_url_path: str,
    target_dir: str,
) -> list[tuple[str, bool, int, str]]:
    if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
        raise ObjectStorageError("webdav_xml_unsafe", "Unsafe XML DOCTYPE/ENTITY in PROPFIND")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ObjectStorageError("webdav_xml_invalid", f"Invalid PROPFIND XML: {exc}") from exc

    base_prefix = base_url_path.strip("/")
    entries: list[tuple[str, bool, int, str]] = []
    for resp_el in root.iter():
        if _local_name(resp_el.tag) != "response":
            continue
        href = ""
        is_collection = False
        size_bytes = 0
        content_type = ""
        for child in resp_el.iter():
            tag = _local_name(child.tag)
            if tag == "href" and not href and child.text:
                href = child.text.strip()
            elif tag == "collection":
                is_collection = True
            elif tag == "getcontentlength" and child.text and child.text.strip().isdigit():
                size_bytes = int(child.text.strip())
            elif tag == "getcontenttype" and child.text:
                content_type = child.text.strip().split(";")[0].strip()
        if not href:
            continue
        href_path = unquote(urlsplit(href).path).strip("/")
        if base_prefix:
            if href_path == base_prefix:
                rel_path = ""
            elif href_path.startswith(f"{base_prefix}/"):
                rel_path = href_path[len(base_prefix) + 1 :]
            else:
                continue
        else:
            rel_path = href_path
        if rel_path == target_dir:
            continue
        if href.endswith("/"):
            is_collection = True
        entries.append((rel_path, is_collection, size_bytes, content_type))
    return entries


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


async def _stage_input_to_temp(
    data: bytes | AsyncIterable[bytes],
    max_bytes: int | None,
    expected_sha256: str,
) -> tuple[Path, int, str]:
    descriptor, name = tempfile.mkstemp(prefix="webdav-put-", suffix=".tmp")
    os.close(descriptor)
    temporary = Path(name)
    digest = hashlib.sha256()
    size = 0
    try:
        if isinstance(data, (bytes, bytearray, memoryview)):
            raw = bytes(data)
            size = len(raw)
            if max_bytes is not None and size > max_bytes:
                raise ObjectStorageError("object_too_large", "Object exceeds size limit")
            digest.update(raw)
            await asyncio.to_thread(temporary.write_bytes, raw)
        else:
            with temporary.open("wb") as handle:
                async for chunk in data:
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


async def _file_chunk_stream(path: Path) -> AsyncIterable[bytes]:
    with path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, STREAM_CHUNK_SIZE):
            yield chunk
