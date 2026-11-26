"""CloudFlare ImgBed object storage driver with scope guards and plugin ZIP acceptance probing."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import tempfile
import zipfile
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from .models import (
    ObjectListingEntry,
    ObjectRange,
    ObjectStorageError,
    StorageBackend,
    StorageCapabilities,
    StoredObject,
    guess_content_type,
    slice_object_range,
    validate_expected_sha256,
    validate_expected_size,
    validate_namespace,
    validate_object_key,
    validate_profile_id,
    validate_range_params,
)
from .webdav_driver import (
    _parse_content_range_total,
    _reject_private_host,
    _stage_input_to_temp,
    _verify_dns_global,
)

DEFAULT_IMGBED_TIMEOUT_SECONDS = 30.0
STREAM_CHUNK_SIZE = 256 * 1024
ALLOWED_IMGBED_SCOPES = frozenset({"upload", "list", "delete"})
AVATAR_VARIANT_SIZES = frozenset({64, 128, 256})

# Valid 1x1 transparent PNG used for ImgBed health probe.
_TINY_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(frozen=True, slots=True)
class ImgBedProbeResult:
    """Result of probing ImgBed avatar capabilities and optional plugin ZIP acceptance gates."""

    reachable: bool
    upload: bool
    read: bool
    image_variants: bool
    delete: bool
    write_probe_skipped_no_delete_scope: bool
    plugin_zip_ready: bool
    plugin_zip_checks: dict[str, bool]
    range_read: bool
    message: str


def validate_imgbed_base_url(
    value: Any,
    *,
    allow_private_network: bool = False,
) -> str:
    """Validate ImgBed instance origin URL without credentials, query, or unsafe private hosts."""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ObjectStorageError("imgbed_base_url_missing", "ImgBed base URL is required")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ObjectStorageError("imgbed_base_url_invalid", "Invalid ImgBed base URL") from exc
    allowed_schemes = {"https", "http"} if allow_private_network else {"https"}
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        if parsed.scheme == "http" and not allow_private_network:
            raise ObjectStorageError(
                "imgbed_https_required",
                "ImgBed base URL must use HTTPS unless allow_private_network is explicitly enabled",
            )
        raise ObjectStorageError("imgbed_base_url_invalid", "ImgBed base URL must be absolute")
    if parsed.username or parsed.password:
        raise ObjectStorageError(
            "imgbed_credentials_in_url_forbidden",
            "ImgBed credentials must not be embedded in the URL",
        )
    if parsed.query or parsed.fragment:
        raise ObjectStorageError(
            "imgbed_base_url_invalid",
            "ImgBed base URL cannot contain query parameters or fragments",
        )
    if not allow_private_network:
        _reject_private_host(parsed.hostname)
    return raw


def validate_imgbed_folder(value: Any) -> str:
    """Normalize a dedicated ImgBed uploadFolder prefix for service-layer directory isolation."""
    raw = str(value or "").strip().strip("/")
    if not raw:
        return ""
    if "\\" in raw or any(ord(ch) < 32 for ch in raw):
        raise ObjectStorageError("imgbed_folder_invalid", "Invalid ImgBed upload folder")
    parts = raw.split("/")
    if len(parts) > 12:
        raise ObjectStorageError("imgbed_folder_invalid", "ImgBed upload folder is too deep")
    for part in parts:
        if not part or part in {".", ".."} or len(part) > 128:
            raise ObjectStorageError(
                "imgbed_folder_invalid", "Invalid ImgBed upload folder segment"
            )
    return "/".join(parts)


def validate_imgbed_expire(value: Any) -> str:
    """Validate an ImgBed expire option using the upstream h/d and 3650-day contract."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw == "forever":
        return raw
    match = re.fullmatch(r"([1-9]\d{0,5})([hd])", raw)
    if match:
        amount = int(match.group(1))
        limit = 3650 * 24 if match.group(2) == "h" else 3650
        if amount <= limit:
            return raw
    raise ObjectStorageError(
        "imgbed_expire_invalid",
        "ImgBed expire must be forever or a positive integer with h/d, up to 3650d",
    )


def validate_imgbed_scopes(value: Any) -> tuple[str, ...]:
    """Validate explicit Token permission scopes declared on an ImgBed profile."""
    if value is None:
        return ("upload",)
    if isinstance(value, str):
        raw_items = [part.strip().lower() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_items = [str(part).strip().lower() for part in value if str(part).strip()]
    else:
        raise ObjectStorageError("imgbed_scopes_invalid", "ImgBed scopes must be a list")
    cleaned = tuple(dict.fromkeys(raw_items))
    if not cleaned:
        raise ObjectStorageError("imgbed_scopes_invalid", "ImgBed requires at least 'upload' scope")
    invalid = [item for item in cleaned if item not in ALLOWED_IMGBED_SCOPES]
    if invalid:
        raise ObjectStorageError(
            "imgbed_scopes_invalid",
            f"Unsupported ImgBed scopes: {', '.join(invalid)}",
        )
    if "upload" not in cleaned:
        raise ObjectStorageError("imgbed_scopes_invalid", "ImgBed profile must include 'upload'")
    return cleaned


def build_imgbed_variant_url(
    url: str,
    *,
    width: int,
    height: int,
    fit: str = "cover",
    fallback: str = "original",
) -> str:
    """Append ImgBed image transformation params only to genuine /file/... URLs."""
    raw = str(url or "").strip()
    if not raw or width <= 0 or height <= 0:
        return raw
    parsed = urlsplit(raw)
    if not parsed.path.startswith("/file/"):
        return raw
    query_items = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k not in {"width", "height", "fit", "fallback"}
    ]
    query_items.extend(
        [
            ("width", str(int(width))),
            ("height", str(int(height))),
            ("fit", str(fit or "cover")),
            ("fallback", str(fallback or "original")),
        ]
    )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_items),
            parsed.fragment,
        )
    )


def _build_probe_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.yaml", "name: astrbot_plugin_probe\nversion: 0.0.1\n")
        archive.writestr("main.py", "def probe() -> str:\n    return 'astrhub-imgbed-probe'\n")
    return buf.getvalue()


class ImgBedObjectStorageDriver:
    """ObjectStorageDriver backed by CloudFlare ImgBed (/upload, /file/*, /api/manage/*)."""

    backend = StorageBackend.IMGBED

    def __init__(
        self,
        *,
        profile_id: str,
        base_url: str,
        token: str,
        upload_folder: str = "",
        expire: str = "",
        scopes: tuple[str, ...] | list[str] = ("upload",),
        image_variants_enabled: bool = True,
        range_read_supported: bool = False,
        plugin_zip_verified: bool = False,
        allow_private_network: bool = False,
        verify_tls: bool = True,
        timeout_seconds: float = DEFAULT_IMGBED_TIMEOUT_SECONDS,
        list_page_size: int = 1000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.allow_private_network = bool(allow_private_network)
        self.base_url = validate_imgbed_base_url(
            base_url,
            allow_private_network=self.allow_private_network,
        )
        self._token = str(token or "").strip()
        if not self._token:
            raise ObjectStorageError("imgbed_token_missing", "ImgBed Bearer token is required")
        self.upload_folder = validate_imgbed_folder(upload_folder)
        self.expire = validate_imgbed_expire(expire)
        self.scopes = validate_imgbed_scopes(scopes)
        self.image_variants_enabled = bool(image_variants_enabled)
        self.range_read_supported = bool(range_read_supported)
        self.plugin_zip_verified = bool(plugin_zip_verified)
        self.verify_tls = bool(verify_tls)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.list_page_size = max(1, min(1000, int(list_page_size)))
        self._external_client = client is not None
        self._client = client
        self._parsed_base = urlsplit(self.base_url)

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            conditional_create=False,
            range_read=self.range_read_supported,
            public_read=True,
            image_variants=self.image_variants_enabled,
            list_objects="list" in self.scopes,
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
            full_path = self._full_path(key, normalized_ns)
            if if_absent:
                existing = await self._check_existing_object(
                    key=key,
                    namespace=normalized_ns,
                    full_path=full_path,
                    expected_sha256=sha256,
                )
                if existing is not None:
                    return existing

            folder, _, filename = full_path.rpartition("/")
            params: dict[str, str] = {
                "uploadNameType": "origin",
                "returnFormat": "default",
            }
            if folder:
                params["uploadFolder"] = folder
            if self.expire:
                params["expire"] = self.expire

            raw_bytes = await asyncio.to_thread(temporary.read_bytes)
            response = await self._request(
                "POST",
                f"{self.base_url}/upload",
                params=params,
                headers=self._auth_headers(),
                files={"file": (filename, raw_bytes, resolved_type)},
            )
            if response.status_code not in {200, 201}:
                raise _imgbed_status_error("imgbed_upload_failed", response)
            src = _extract_imgbed_src(response)
            actual_full_path = src.removeprefix("/file/").strip("/")
            self._assert_within_upload_folder(actual_full_path)
            actual_key = _strip_prefix(
                actual_full_path, _join_parts(self.upload_folder, normalized_ns)
            )
            if not actual_key:
                raise ObjectStorageError(
                    "imgbed_invalid_src",
                    "ImgBed upload returned a src outside the expected namespace",
                )

            # Verify uploaded bytes from /file/{src} so corrupted uploads fail loudly.
            verified_bytes = await self._read_by_src(src)
            validate_expected_size(size_bytes, len(verified_bytes))
            validate_expected_sha256(sha256, hashlib.sha256(verified_bytes).hexdigest())

            merged_meta = {str(k): str(v) for k, v in (metadata or {}).items()}
            merged_meta["imgbed_src"] = src
            return StoredObject(
                profile_id=self.profile_id,
                backend=self.backend.value,
                namespace=normalized_ns,
                key=actual_key,
                size_bytes=size_bytes,
                sha256=sha256,
                content_type=resolved_type,
                public_url=f"{self.base_url}{src}",
                metadata=merged_meta,
            )
        finally:
            temporary.unlink(missing_ok=True)

    async def stat_object(self, key: str, *, namespace: str = "") -> StoredObject | None:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        full_path = self._full_path(key, normalized_ns)
        url = self._file_url(full_path)
        response = await self._request("HEAD", url, headers={})
        if response.status_code == 404:
            return None
        if response.status_code not in {200, 204}:
            raise _imgbed_status_error("imgbed_stat_failed", response)
        size_raw = response.headers.get("content-length", "0").strip()
        size_bytes = int(size_raw) if size_raw.isdigit() else 0
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=normalized_ns,
            key=key,
            size_bytes=size_bytes,
            sha256="",
            content_type=content_type or guess_content_type(key),
            public_url=url,
            metadata={"imgbed_src": f"/file/{full_path}"},
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
        full_path = self._full_path(key, normalized_ns)
        return await self._read_by_src(
            f"/file/{full_path}",
            max_bytes=max_bytes,
            expected_sha256=expected_sha256,
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
        stat = await self.stat_object(key, namespace=normalized_ns)
        if stat is None:
            raise ObjectStorageError("object_missing", "Object not found")
        if max_object_bytes is not None and stat.size_bytes > max_object_bytes:
            raise ObjectStorageError("object_too_large", "Object exceeds limit")
        validate_expected_size(expected_size_bytes, stat.size_bytes)
        if start_byte > stat.size_bytes:
            raise ObjectStorageError("object_range_invalid", "Object range is invalid")
        end_byte = min(stat.size_bytes, start_byte + max_bytes)
        if start_byte == end_byte:
            return ObjectRange(
                content=b"",
                size_bytes=stat.size_bytes,
                sha256=expected_sha256,
                content_type=stat.content_type or guess_content_type(key),
                start_byte=start_byte,
                end_byte=end_byte,
                truncated=end_byte < stat.size_bytes,
            )

        url = self._file_url(self._full_path(key, normalized_ns))
        response = await self._request(
            "GET",
            url,
            headers={"Range": f"bytes={start_byte}-{end_byte - 1}"},
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
                    "imgbed_range_length_mismatch",
                    "ImgBed range response length does not match requested slice",
                )
            resolved_end = start_byte + len(body)
            return ObjectRange(
                content=body,
                size_bytes=total_size,
                sha256=expected_sha256,
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
        raise _imgbed_status_error("imgbed_range_failed", response)

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
        full_path = self._full_path(key, normalized_ns)
        url = self._file_url(full_path)
        await self._guard_runtime_dns()
        client = await self._ensure_client()
        descriptor, temp_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=".imgbed-dl-",
            suffix=".tmp",
        )
        os.close(descriptor)
        temporary = Path(temp_name)
        digest = hashlib.sha256()
        written = 0
        content_type = guess_content_type(key)
        try:
            async with client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code == 404:
                    raise ObjectStorageError("object_missing", "Object not found")
                if response.status_code != 200:
                    raise _imgbed_status_error("imgbed_get_failed", response)
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
            raise ObjectStorageError("imgbed_network_error", f"ImgBed GET failed: {exc}") from exc
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
            public_url=url,
            metadata={"imgbed_src": f"/file/{full_path}"},
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
        descriptor, temp_name = tempfile.mkstemp(prefix="imgbed-copy-", suffix=".tmp")
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
        if "delete" not in self.scopes:
            raise ObjectStorageError(
                "imgbed_scope_missing",
                "ImgBed profile does not grant 'delete' scope",
            )
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        full_path = self._full_path(key, normalized_ns)
        self._assert_within_upload_folder(full_path)
        encoded = "/".join(quote(part, safe="") for part in full_path.split("/"))
        response = await self._request(
            "DELETE",
            f"{self.base_url}/api/manage/delete/{encoded}",
            headers=self._auth_headers(),
        )
        if response.status_code in {200, 202, 204, 404}:
            return
        raise _imgbed_status_error("imgbed_delete_failed", response)

    def public_url(self, key: str, *, namespace: str = "") -> str:
        validate_object_key(key)
        normalized_ns = validate_namespace(namespace)
        return self._file_url(self._full_path(key, normalized_ns))

    async def list_objects(
        self,
        *,
        namespace: str = "",
        prefix: str = "",
    ) -> list[ObjectListingEntry]:
        if "list" not in self.scopes:
            raise ObjectStorageError(
                "imgbed_scope_missing",
                "ImgBed profile does not grant 'list' scope",
            )
        normalized_ns = validate_namespace(namespace)
        base_dir = _join_parts(self.upload_folder, normalized_ns)
        entries_by_key: dict[str, ObjectListingEntry] = {}
        start = 0
        page_size = self.list_page_size
        max_pages = 10_000

        for _ in range(max_pages):
            params: dict[str, Any] = {
                "start": start,
                "count": page_size,
                "recursive": "true",
            }
            if base_dir:
                params["dir"] = base_dir
            response = await self._request(
                "GET",
                f"{self.base_url}/api/manage/list",
                params=params,
                headers=self._auth_headers(),
            )
            if response.status_code == 404:
                break
            if response.status_code != 200:
                raise _imgbed_status_error("imgbed_list_failed", response)
            try:
                payload = response.json()
            except ValueError as exc:
                raise ObjectStorageError("imgbed_list_invalid", "Invalid ImgBed list JSON") from exc

            raw_files = payload.get("files") if isinstance(payload, Mapping) else payload
            if not isinstance(raw_files, list) or not raw_files:
                break

            page_new_count = 0
            for item in raw_files:
                if not isinstance(item, Mapping):
                    continue
                raw_name = str(item.get("name") or item.get("id") or "").strip().lstrip("/")
                if raw_name.startswith("file/"):
                    raw_name = raw_name.removeprefix("file/")
                if not raw_name:
                    continue
                # Service-layer isolation: never expose files outside this profile's upload_folder.
                if self.upload_folder and not (
                    raw_name == self.upload_folder or raw_name.startswith(f"{self.upload_folder}/")
                ):
                    continue
                key = _strip_prefix(raw_name, base_dir)
                if not key:
                    continue
                if prefix and not key.startswith(prefix):
                    continue
                if key not in entries_by_key:
                    page_new_count += 1
                meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
                size_bytes = _parse_imgbed_size_bytes(meta, item)
                entries_by_key[key] = ObjectListingEntry(
                    namespace=normalized_ns,
                    key=key,
                    size_bytes=size_bytes,
                    content_type=guess_content_type(key),
                )

            returned_raw = payload.get("returnedCount") if isinstance(payload, Mapping) else None
            returned_count = (
                int(returned_raw)
                if isinstance(returned_raw, int) and returned_raw >= 0
                else len(raw_files)
            )
            total_raw = payload.get("totalCount") if isinstance(payload, Mapping) else None
            total_count = int(total_raw) if isinstance(total_raw, int) and total_raw >= 0 else None

            if returned_count <= 0:
                break
            start += returned_count
            if total_count is not None:
                if start >= total_count:
                    break
            elif returned_count < page_size:
                break
            elif page_new_count == 0 and len(raw_files) == 0:
                break

        entries = list(entries_by_key.values())
        entries.sort(key=lambda entry: entry.key)
        return entries

    async def probe_capabilities(
        self,
        *,
        probe_prefix: str = "__healthcheck__/",
        test_plugin_zip: bool = False,
    ) -> ImgBedProbeResult:
        """Probe ImgBed capabilities without leaking persistent files when delete scope is absent."""
        token = os.urandom(8).hex()
        has_delete = "delete" in self.scopes
        reachable = False
        upload_ok = False
        read_ok = False
        variants_ok = False
        delete_ok = False
        write_probe_skipped = not has_delete
        message = "ImgBed 头像上传与公开读取校验通过"

        if not has_delete:
            # Never upload a persistent probe object when delete permission is absent.
            try:
                if "list" in self.scopes:
                    list_res = await self._request(
                        "GET",
                        f"{self.base_url}/api/manage/list",
                        params={
                            "start": 0,
                            "count": 1,
                            **({"dir": self.upload_folder} if self.upload_folder else {}),
                        },
                        headers=self._auth_headers(),
                    )
                    if list_res.status_code != 200:
                        raise _imgbed_status_error("imgbed_list_failed", list_res)
                    reachable = True
                else:
                    ping_res = await self._request("HEAD", f"{self.base_url}/", headers={})
                    reachable = ping_res.status_code < 500
                    if not reachable:
                        raise _imgbed_status_error("imgbed_unreachable", ping_res)

                sample_path = _join_parts(self.upload_folder, f"{probe_prefix}probe.webp")
                sample_url = self._file_url(sample_path)
                if self.image_variants_enabled:
                    variant_url = build_imgbed_variant_url(
                        sample_url,
                        width=256,
                        height=256,
                        fit="cover",
                        fallback="original",
                    )
                    variants_ok = (
                        "/file/" in variant_url
                        and "width=256" in variant_url
                        and "height=256" in variant_url
                        and "fit=cover" in variant_url
                        and "fallback=original" in variant_url
                    )
                else:
                    variants_ok = True
                message = "ImgBed 实例可达性与配置校验通过（未授予 delete 权限，已跳过会留下持久文件的上传探针）"
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
        else:
            img_key = f"{probe_prefix}probe-{token}.png"
            img_sha = hashlib.sha256(_TINY_PNG_BYTES).hexdigest()
            uploaded_img_key = ""
            try:
                stored = await self.put_object(
                    img_key,
                    _TINY_PNG_BYTES,
                    content_type="image/png",
                    expected_sha256=img_sha,
                    # Unique probe keys let the upload create a missing uploadFolder.
                    if_absent=False,
                )
                reachable = True
                uploaded_img_key = stored.key
                src = stored.metadata.get("imgbed_src", "")
                upload_ok = bool(src.startswith("/file/") and stored.sha256 == img_sha)
                read_bytes = await self.read_object(stored.key, expected_sha256=img_sha)
                read_ok = read_bytes == _TINY_PNG_BYTES
                if self.image_variants_enabled:
                    variant_url = build_imgbed_variant_url(
                        stored.public_url,
                        width=256,
                        height=256,
                        fit="cover",
                        fallback="original",
                    )
                    variants_ok = (
                        "/file/" in variant_url
                        and "width=256" in variant_url
                        and "height=256" in variant_url
                        and "fit=cover" in variant_url
                        and "fallback=original" in variant_url
                    )
                else:
                    variants_ok = True
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
            finally:
                if uploaded_img_key:
                    try:
                        await self.delete_object(uploaded_img_key)
                        delete_ok = await self.stat_object(uploaded_img_key) is None
                    except Exception as cleanup_exc:
                        delete_ok = False
                        if message == "ImgBed 头像上传与公开读取校验通过":
                            message = f"ImgBed 测试图片清理失败：{cleanup_exc}"

        plugin_zip_checks: dict[str, bool] = {
            "scopes": {"upload", "list", "delete"}.issubset(set(self.scopes)),
            "upload_zip": False,
            "head": False,
            "get": False,
            "range": False,
            "cache_control": False,
            "list": False,
            "revoke": False,
        }
        plugin_zip_ready = False
        range_supported = self.range_read_supported

        should_probe_zip = test_plugin_zip or plugin_zip_checks["scopes"]
        if should_probe_zip and plugin_zip_checks["scopes"]:
            zip_key = f"{probe_prefix}probe-plugin-{token}.zip"
            zip_bytes = _build_probe_zip_bytes()
            zip_sha = hashlib.sha256(zip_bytes).hexdigest()
            uploaded_zip_key = ""
            try:
                stored_zip = await self.put_object(
                    zip_key,
                    zip_bytes,
                    content_type="application/zip",
                    expected_sha256=zip_sha,
                    if_absent=False,
                )
                uploaded_zip_key = stored_zip.key
                plugin_zip_checks["upload_zip"] = (
                    stored_zip.metadata.get("imgbed_src", "").startswith("/file/")
                    and stored_zip.sha256 == zip_sha
                )
                full_zip_url = self._file_url(self._full_path(uploaded_zip_key, ""))

                head_res = await self._request("HEAD", full_zip_url, headers={})
                plugin_zip_checks["head"] = head_res.status_code == 200 and head_res.headers.get(
                    "content-length", ""
                ).strip() == str(len(zip_bytes))
                cc = head_res.headers.get("cache-control", "").lower()

                get_res = await self._request("GET", full_zip_url, headers={})
                plugin_zip_checks["get"] = (
                    get_res.status_code == 200
                    and hashlib.sha256(get_res.content).hexdigest() == zip_sha
                )
                if not cc:
                    cc = get_res.headers.get("cache-control", "").lower()
                plugin_zip_checks["cache_control"] = bool(cc) and "no-store" not in cc

                range_res = await self._request(
                    "GET",
                    full_zip_url,
                    headers={"Range": "bytes=0-15"},
                )
                plugin_zip_checks["range"] = (
                    range_res.status_code == 206 and range_res.content == zip_bytes[:16]
                )
                range_supported = plugin_zip_checks["range"]

                listed = await self.list_objects(prefix=probe_prefix)
                plugin_zip_checks["list"] = any(item.key == uploaded_zip_key for item in listed)
            except Exception as zip_exc:
                if test_plugin_zip:
                    message = f"ImgBed 插件 ZIP 验收未通过：{zip_exc}"
            finally:
                if uploaded_zip_key:
                    try:
                        await self.delete_object(uploaded_zip_key)
                        plugin_zip_checks["revoke"] = (
                            await self.stat_object(uploaded_zip_key) is None
                        )
                    except Exception:
                        plugin_zip_checks["revoke"] = False

            plugin_zip_ready = all(plugin_zip_checks.values())
            self.range_read_supported = range_supported
            self.plugin_zip_verified = plugin_zip_ready
            if test_plugin_zip and not plugin_zip_ready and message.startswith("ImgBed 头像"):
                failed = [k for k, ok in plugin_zip_checks.items() if not ok]
                message = f"ImgBed 插件 ZIP 验收未通过：{', '.join(failed)}"
            elif plugin_zip_ready and upload_ok and read_ok and variants_ok and delete_ok:
                message = "ImgBed 头像与插件 ZIP 能力验收全部通过"
        else:
            self.plugin_zip_verified = False
            if test_plugin_zip:
                message = "ImgBed 绑定插件公开用途需要 upload、list 与 delete 权限并通过 ZIP 验收"

        base_passed = (
            reachable and variants_ok
            if write_probe_skipped
            else (upload_ok and read_ok and variants_ok and delete_ok)
        )
        if not base_passed and message.startswith("ImgBed 头像"):
            message = "ImgBed 基础读写能力校验未通过"

        return ImgBedProbeResult(
            reachable=reachable,
            upload=upload_ok,
            read=read_ok,
            image_variants=variants_ok,
            delete=delete_ok,
            write_probe_skipped_no_delete_scope=write_probe_skipped,
            plugin_zip_ready=plugin_zip_ready,
            plugin_zip_checks=plugin_zip_checks,
            range_read=range_supported,
            message=message,
        )

    async def _check_existing_object(
        self,
        *,
        key: str,
        namespace: str,
        full_path: str,
        expected_sha256: str,
    ) -> StoredObject | None:
        stat = await self.stat_object(key, namespace=namespace)
        if stat is None and "list" in self.scopes:
            listed = await self.list_objects(namespace=namespace, prefix=key)
            if any(entry.key == key for entry in listed):
                stat = StoredObject(
                    profile_id=self.profile_id,
                    backend=self.backend.value,
                    namespace=namespace,
                    key=key,
                    size_bytes=0,
                    sha256="",
                    content_type=guess_content_type(key),
                    public_url=self._file_url(full_path),
                    metadata={"imgbed_src": f"/file/{full_path}"},
                )
        if stat is None:
            return None
        existing_bytes = await self._read_by_src(f"/file/{full_path}")
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
                public_url=self._file_url(full_path),
                metadata={"imgbed_src": f"/file/{full_path}"},
            )
        raise ObjectStorageError(
            "object_key_conflict",
            "Object already exists with different content",
        )

    async def _read_by_src(
        self,
        src: str,
        *,
        max_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> bytes:
        normalized_src = "/" + src.lstrip("/")
        if not normalized_src.startswith("/file/"):
            raise ObjectStorageError("imgbed_invalid_src", "ImgBed src must start with /file/")
        rel = normalized_src.removeprefix("/file/").strip("/")
        self._assert_within_upload_folder(rel)
        url = self._file_url(rel)
        await self._guard_runtime_dns()
        client = await self._ensure_client()
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        try:
            async with client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code == 404:
                    raise ObjectStorageError("object_missing", "Object not found")
                if response.status_code != 200:
                    raise _imgbed_status_error("imgbed_get_failed", response)
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
            raise ObjectStorageError("imgbed_network_error", f"ImgBed GET failed: {exc}") from exc
        content = b"".join(chunks)
        validate_expected_sha256(expected_sha256, digest.hexdigest())
        return content

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
        params: Mapping[str, Any] | None = None,
        files: Any | None = None,
    ) -> httpx.Response:
        await self._guard_runtime_dns()
        client = await self._ensure_client()
        try:
            return await client.request(
                method,
                url,
                headers=headers,
                params=params,
                files=files,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ObjectStorageError(
                "imgbed_network_error",
                f"ImgBed {method} request failed: {exc.__class__.__name__}",
            ) from exc

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _full_path(self, key: str, namespace: str) -> str:
        return _join_parts(self.upload_folder, namespace, key)

    def _file_url(self, full_path: str) -> str:
        encoded = "/".join(quote(part, safe="") for part in full_path.strip("/").split("/") if part)
        return f"{self.base_url}/file/{encoded}"

    def _assert_within_upload_folder(self, full_path: str) -> None:
        cleaned = full_path.strip("/")
        if not self.upload_folder:
            return
        if cleaned != self.upload_folder and not cleaned.startswith(f"{self.upload_folder}/"):
            raise ObjectStorageError(
                "imgbed_folder_scope_violation",
                "ImgBed object path is outside this profile's upload_folder",
            )


def _parse_imgbed_size_bytes(meta: Mapping[str, Any], item: Mapping[str, Any]) -> int:
    """Parse ImgBed object size preferring exact FileSizeBytes before FileSize (MB or int bytes)."""
    for candidate in (meta.get("FileSizeBytes"), item.get("size")):
        if candidate is None or isinstance(candidate, bool):
            continue
        try:
            parsed = int(candidate)
            if parsed >= 0:
                return parsed
        except (TypeError, ValueError):
            continue

    raw_file_size = meta.get("FileSize")
    if raw_file_size is None or isinstance(raw_file_size, bool):
        return 0
    if isinstance(raw_file_size, int):
        return max(0, raw_file_size)
    if isinstance(raw_file_size, float):
        return max(0, round(raw_file_size * 1024 * 1024))
    text = str(raw_file_size).strip()
    if not text:
        return 0
    lowered = text.lower()
    if lowered.endswith("mb"):
        text = text[:-2].strip()
        try:
            return max(0, round(float(text) * 1024 * 1024))
        except ValueError:
            return 0
    if "." in text:
        try:
            return max(0, round(float(text) * 1024 * 1024))
        except ValueError:
            return 0
    try:
        return max(0, int(text))
    except ValueError:
        return 0


def _join_parts(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part and part.strip("/"))


def _strip_prefix(full_path: str, prefix: str) -> str:
    norm = full_path.strip("/")
    norm_prefix = prefix.strip("/")
    if not norm_prefix:
        return norm
    if norm == norm_prefix:
        return ""
    if norm.startswith(f"{norm_prefix}/"):
        return norm[len(norm_prefix) + 1 :]
    return ""


def _extract_imgbed_src(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ObjectStorageError(
            "imgbed_invalid_response", "ImgBed upload returned non-JSON"
        ) from exc
    item: Any = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(item, Mapping):
        raise ObjectStorageError("imgbed_invalid_response", "ImgBed upload payload is invalid")
    src = str(item.get("src") or "").strip()
    if not src:
        raise ObjectStorageError(
            "imgbed_missing_src",
            "ImgBed upload response did not include /file/... src",
        )
    normalized = "/" + src.lstrip("/")
    if not normalized.startswith("/file/"):
        raise ObjectStorageError(
            "imgbed_invalid_src",
            "ImgBed upload response src must use the /file/... delivery path",
        )
    return normalized


def _imgbed_status_error(code: str, response: httpx.Response) -> ObjectStorageError:
    status = response.status_code
    if status in {401, 403}:
        return ObjectStorageError("imgbed_auth_failed", f"ImgBed rejected token ({status})")
    if status == 404:
        return ObjectStorageError("object_missing", "ImgBed object not found")
    if status == 415:
        return ObjectStorageError(
            "imgbed_media_type_rejected", "ImgBed rejected uploaded file type"
        )
    return ObjectStorageError(code, f"ImgBed request returned HTTP {status}")


async def _file_chunk_stream(path: Path) -> AsyncIterable[bytes]:
    with path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, STREAM_CHUNK_SIZE):
            yield chunk
