"""Stage 3 tests for WebDAV driver, capability probing, CAS fallback, migrations, and faults."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape as xml_escape

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import load_settings
from app.main import create_app
from app.object_storage import (
    ObjectStorageError,
    StorageManager,
    StoragePurpose,
    WebDavObjectStorageDriver,
    content_addressed_key,
    validate_webdav_base_url,
    validate_webdav_root_path,
)
from app.object_storage.models import StorageMigrationItemStatus, StorageMigrationStatus
from app.object_storage.webdav_driver import _verify_dns_global
from app.store import InMemoryMarketStore


class FakeWebDavServer(httpx.AsyncBaseTransport):
    """Stateful RFC 4918 WebDAV server transport for deterministic contract and fault testing."""

    def __init__(
        self,
        *,
        base_prefix: str = "/dav",
        expected_auth: str = "",
        support_conditional_put: bool = True,
        support_range: bool = True,
    ) -> None:
        self.base_prefix = base_prefix.rstrip("/")
        self.expected_auth = expected_auth
        self.support_conditional_put = support_conditional_put
        self.support_range = support_range
        self.dirs: set[str] = {""}
        self.files: dict[str, tuple[bytes, str]] = {}
        self.request_log: list[tuple[str, str, dict[str, str]]] = []
        self.disconnected = False
        self.fail_put_once_substring: str = ""
        self._failed_put_once = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.request_log.append((request.method, str(request.url), headers))

        if self.disconnected:
            raise httpx.ConnectError("Simulated WebDAV network outage", request=request)

        if self.expected_auth and headers.get("authorization", "") != self.expected_auth:
            return httpx.Response(401, text="Unauthorized", request=request)

        if self.base_prefix:
            if path == self.base_prefix or path == f"{self.base_prefix}/":
                rel = ""
            elif path.startswith(f"{self.base_prefix}/"):
                rel = path[len(self.base_prefix) + 1 :].strip("/")
            else:
                return httpx.Response(404, text="Not Found", request=request)
        else:
            rel = path.strip("/")

        method = request.method.upper()
        if method == "MKCOL":
            if rel in self.dirs or rel in self.files:
                return httpx.Response(405, request=request)
            parent = rel.rpartition("/")[0]
            if parent and parent not in self.dirs:
                return httpx.Response(409, request=request)
            self.dirs.add(rel)
            return httpx.Response(201, request=request)

        if method == "PUT":
            body = await request.aread()
            if (
                self.fail_put_once_substring
                and not self._failed_put_once
                and self.fail_put_once_substring in rel
            ):
                self._failed_put_once = True
                raise httpx.ReadTimeout("Simulated transient upload timeout", request=request)
            parent = rel.rpartition("/")[0]
            if parent and parent not in self.dirs:
                return httpx.Response(409, request=request)
            if self.support_conditional_put and headers.get("if-none-match") == "*":
                if rel in self.files:
                    return httpx.Response(412, request=request)
            existed = rel in self.files
            content_type = headers.get("content-type", "application/octet-stream")
            self.files[rel] = (body, content_type)
            return httpx.Response(204 if existed else 201, request=request)

        if method == "HEAD":
            entry = self.files.get(rel)
            if entry is None:
                return httpx.Response(404, request=request)
            data, content_type = entry
            etag = f'"{hashlib.md5(data).hexdigest()}"'
            return httpx.Response(
                200,
                headers={
                    "Content-Length": str(len(data)),
                    "Content-Type": content_type,
                    "ETag": etag,
                },
                request=request,
            )

        if method == "GET":
            entry = self.files.get(rel)
            if entry is None:
                return httpx.Response(404, request=request)
            data, content_type = entry
            range_hdr = headers.get("range", "")
            if range_hdr and self.support_range and range_hdr.startswith("bytes="):
                rng = range_hdr.removeprefix("bytes=")
                start_s, end_s = rng.split("-", 1)
                start = int(start_s)
                end = int(end_s) if end_s else len(data) - 1
                if start >= len(data) or end < start:
                    return httpx.Response(
                        416,
                        headers={"Content-Range": f"bytes */{len(data)}"},
                        request=request,
                    )
                end = min(end, len(data) - 1)
                sliced = data[start : end + 1]
                return httpx.Response(
                    206,
                    content=sliced,
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": str(len(sliced)),
                        "Content-Range": f"bytes {start}-{end}/{len(data)}",
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                content=data,
                headers={"Content-Type": content_type, "Content-Length": str(len(data))},
                request=request,
            )

        if method == "DELETE":
            if rel in self.files:
                self.files.pop(rel, None)
                return httpx.Response(204, request=request)
            if rel in self.dirs and rel != "":
                self.dirs.discard(rel)
                return httpx.Response(204, request=request)
            return httpx.Response(404, request=request)

        if method == "PROPFIND":
            if rel and rel not in self.dirs and rel not in self.files:
                return httpx.Response(404, request=request)
            xml_items: list[str] = []
            if rel in self.dirs:
                xml_items.append(self._prop_entry(rel, is_dir=True, size=0, content_type=""))
                prefix = f"{rel}/" if rel else ""
                seen_children: set[str] = set()
                for d in sorted(self.dirs):
                    if not d or not d.startswith(prefix):
                        continue
                    rem = d[len(prefix) :]
                    if "/" not in rem and d not in seen_children:
                        seen_children.add(d)
                        xml_items.append(self._prop_entry(d, is_dir=True, size=0, content_type=""))
                for fpath, (fdata, fct) in sorted(self.files.items()):
                    if not fpath.startswith(prefix):
                        continue
                    rem = fpath[len(prefix) :]
                    if "/" not in rem and fpath not in seen_children:
                        seen_children.add(fpath)
                        xml_items.append(
                            self._prop_entry(fpath, is_dir=False, size=len(fdata), content_type=fct)
                        )
            elif rel in self.files:
                fdata, fct = self.files[rel]
                xml_items.append(
                    self._prop_entry(rel, is_dir=False, size=len(fdata), content_type=fct)
                )
            xml_body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:multistatus xmlns:d="DAV:">' + "".join(xml_items) + "</d:multistatus>"
            )
            return httpx.Response(
                207,
                content=xml_body.encode("utf-8"),
                headers={"Content-Type": "application/xml; charset=utf-8"},
                request=request,
            )

        return httpx.Response(405, request=request)

    def _prop_entry(self, rel: str, *, is_dir: bool, size: int, content_type: str) -> str:
        encoded = "/".join(quote(seg, safe="") for seg in rel.split("/") if seg)
        href = f"{self.base_prefix}/{encoded}" if encoded else f"{self.base_prefix}/"
        if is_dir and not href.endswith("/"):
            href += "/"
        res_type = "<d:collection/>" if is_dir else ""
        ct_tag = (
            f"<d:getcontenttype>{xml_escape(content_type)}</d:getcontenttype>"
            if content_type
            else ""
        )
        size_tag = "" if is_dir else f"<d:getcontentlength>{size}</d:getcontentlength>"
        return (
            "<d:response>"
            f"<d:href>{xml_escape(href)}</d:href>"
            "<d:propstat><d:prop>"
            f"<d:resourcetype>{res_type}</d:resourcetype>"
            f"{size_tag}{ct_tag}"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            "</d:response>"
        )


async def _chunk_stream(payload: bytes, size: int = 257) -> AsyncIterator[bytes]:
    for offset in range(0, len(payload), size):
        yield payload[offset : offset + size]


def test_webdav_url_root_path_and_ssrf_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    # HTTPS required by default; HTTP rejected unless allow_private_network=True.
    with pytest.raises(ObjectStorageError) as http_exc:
        validate_webdav_base_url("http://dav.example.com/dav")
    assert http_exc.value.code == "webdav_https_required"

    # Credentials in URL are strictly forbidden.
    with pytest.raises(ObjectStorageError) as cred_exc:
        validate_webdav_base_url("https://alice:secret@dav.example.com/dav")
    assert cred_exc.value.code == "webdav_credentials_in_url_forbidden"

    # Private/loopback hosts rejected by default.
    for blocked in (
        "https://localhost/dav",
        "https://nas.local/dav",
        "https://storage.internal/dav",
        "https://127.0.0.1:8443/dav",
        "https://10.0.0.8/dav",
        "https://192.168.1.50/dav",
        "https://[::1]/dav",
    ):
        with pytest.raises(ObjectStorageError) as priv_exc:
            validate_webdav_base_url(blocked, allow_private_network=False)
        assert priv_exc.value.code == "webdav_private_host_forbidden", blocked

    # Explicitly allowing private network unlocks intranet HTTP/HTTPS hosts.
    assert (
        validate_webdav_base_url("http://192.168.1.50:8080/dav/", allow_private_network=True)
        == "http://192.168.1.50:8080/dav"
    )
    assert validate_webdav_base_url("https://dav.example.com/dav/") == "https://dav.example.com/dav"

    # Root path validation blocks traversal.
    assert validate_webdav_root_path("/astrhub/objects/") == "astrhub/objects"
    with pytest.raises(ObjectStorageError) as root_exc:
        validate_webdav_root_path("astrhub/../secret")
    assert root_exc.value.code == "webdav_root_path_invalid"

    # DNS resolution guard blocks public domain resolving to private IP.
    monkeypatch.setattr(
        "app.object_storage.webdav_driver.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("10.20.30.40", 443))],
    )
    with pytest.raises(ObjectStorageError) as dns_exc:
        asyncio.run(_verify_dns_global("rebind.example.com", 443))
    assert dns_exc.value.code == "webdav_private_host_forbidden"


def test_webdav_driver_contract_headers_range_copy_list_and_delete(tmp_path: Path) -> None:
    expected_basic = "Basic " + base64.b64encode(b"davuser:davpass-secret").decode("ascii")
    server = FakeWebDavServer(expected_auth=expected_basic, support_conditional_put=True)
    client = httpx.AsyncClient(transport=server)
    driver = WebDavObjectStorageDriver(
        profile_id="dav-std",
        base_url="https://dav.example.com/dav",
        root_path="astrhub/stage3",
        auth_type="basic",
        username="davuser",
        password="davpass-secret",
        public_base_url="https://cdn.example.com/dav-files",
        client=client,
    )

    payload = (b"WebDavChunkPayload!" * 80) + b"END"
    payload_sha = hashlib.sha256(payload).hexdigest()

    async def run() -> None:
        try:
            probe = await driver.probe_capabilities()
            assert probe.write is True
            assert probe.read is True
            assert probe.range_read is True
            assert probe.conditional_create is True
            assert probe.overwrite_protection is True
            assert probe.delete is True
            assert probe.public_url is True
            # Probe left zero files behind.
            assert server.files == {}

            stored = await driver.put_object(
                "alice/demo/1.0.0/demo.zip",
                _chunk_stream(payload),
                namespace="published",
                content_type="application/zip",
                expected_sha256=payload_sha,
                if_absent=True,
            )
            assert stored.key == "alice/demo/1.0.0/demo.zip"
            assert stored.size_bytes == len(payload)
            assert stored.sha256 == payload_sha
            assert stored.public_url == (
                "https://cdn.example.com/dav-files/published/alice/demo/1.0.0/demo.zip"
            )
            assert "astrhub/stage3/published/alice/demo/1.0.0/demo.zip" in server.files

            # Auth was strictly passed in Authorization header, never in URLs.
            for _method, req_url, req_headers in server.request_log:
                assert "davpass-secret" not in req_url
                assert req_headers.get("authorization") == expected_basic

            # Idempotent re-put with same content succeeds.
            same = await driver.put_object(
                "alice/demo/1.0.0/demo.zip",
                payload,
                namespace="published",
                if_absent=True,
            )
            assert same.sha256 == payload_sha

            # Different content under if_absent=True raises object_key_conflict.
            with pytest.raises(ObjectStorageError) as conflict_exc:
                await driver.put_object(
                    "alice/demo/1.0.0/demo.zip",
                    b"tampered-zip",
                    namespace="published",
                    if_absent=True,
                )
            assert conflict_exc.value.code == "object_key_conflict"

            # True HTTP 206 Range read slices accurately.
            ranged = await driver.read_range(
                "alice/demo/1.0.0/demo.zip",
                namespace="published",
                start_byte=50,
                max_bytes=120,
                max_object_bytes=len(payload),
                expected_size_bytes=len(payload),
                expected_sha256=payload_sha,
            )
            assert ranged.content == payload[50:170]
            assert ranged.truncated is True

            # Download to disk & copy_object & overwrite.
            dl = tmp_path / "downloaded.zip"
            dl_obj = await driver.download_to_path(
                "alice/demo/1.0.0/demo.zip",
                dl,
                namespace="published",
            )
            assert dl_obj.sha256 == payload_sha
            assert dl.read_bytes() == payload

            copied = await driver.copy_object(
                "alice/demo/1.0.0/demo.zip",
                "alice/demo/1.0.0/demo-v2.zip",
                source_namespace="published",
                target_namespace="published",
                expected_sha256=payload_sha,
                if_absent=True,
            )
            assert copied.sha256 == payload_sha

            replaced = await driver.put_object(
                "alice/demo/1.0.0/demo-v2.zip",
                b"overwritten-bytes",
                namespace="published",
                if_absent=False,
            )
            assert replaced.size_bytes == len(b"overwritten-bytes")
            assert (
                await driver.read_object("alice/demo/1.0.0/demo-v2.zip", namespace="published")
                == b"overwritten-bytes"
            )

            listed = await driver.list_objects(namespace="published", prefix="alice/demo/")
            assert [item.key for item in listed] == [
                "alice/demo/1.0.0/demo-v2.zip",
                "alice/demo/1.0.0/demo.zip",
            ]

            await driver.delete_object("alice/demo/1.0.0/demo-v2.zip", namespace="published")
            assert (
                await driver.stat_object("alice/demo/1.0.0/demo-v2.zip", namespace="published")
                is None
            )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_webdav_non_conditional_server_uses_cas_key_and_rejects_same_name_conflict() -> None:
    server = FakeWebDavServer(
        expected_auth="Bearer secret-bearer-token",
        support_conditional_put=False,
        support_range=True,
    )
    client = httpx.AsyncClient(transport=server)
    driver = WebDavObjectStorageDriver(
        profile_id="dav-nocond",
        base_url="https://dav.example.com/dav",
        root_path="cas-dir",
        auth_type="bearer",
        token="secret-bearer-token",
        client=client,
    )

    payload_a = b"PK\x03\x04content-version-A"
    payload_b = b"PK\x03\x04content-version-B-different"
    sha_a = hashlib.sha256(payload_a).hexdigest()
    expected_cas_key = content_addressed_key("alice/mod/1.0.0/mod.zip", sha_a)

    async def run() -> None:
        try:
            probe = await driver.probe_capabilities()
            assert probe.conditional_create is False
            assert probe.overwrite_protection is True
            assert probe.delete is True
            assert server.files == {}
            assert driver.conditional_create_supported is False

            stored = await driver.put_object(
                "alice/mod/1.0.0/mod.zip",
                payload_a,
                if_absent=True,
            )
            assert stored.key == expected_cas_key
            assert stored.sha256 == sha_a

            # Same logical key + same bytes -> idempotent return of CAS key.
            same = await driver.put_object(
                "alice/mod/1.0.0/mod.zip",
                payload_a,
                if_absent=True,
            )
            assert same.key == expected_cas_key

            # Same logical key + different bytes -> conflict, original bytes untouched!
            with pytest.raises(ObjectStorageError) as exc:
                await driver.put_object(
                    "alice/mod/1.0.0/mod.zip",
                    payload_b,
                    if_absent=True,
                )
            assert exc.value.code == "object_key_conflict"
            assert await driver.read_object(expected_cas_key) == payload_a
        finally:
            await client.aclose()

    asyncio.run(run())


def _make_webdav_app(
    servers: dict[str, FakeWebDavServer],
) -> tuple[TestClient, Any]:
    store = InMemoryMarketStore()
    store.create_internal_admin("coreadmin", hash_password("corepass123"))
    settings = load_settings().with_updates(
        oauth_credential_key="webdav-stage3-master-key",
    )
    app = create_app(settings=settings, store=store)

    def _client_factory(profile: Any, _cfg: dict[str, Any]) -> httpx.AsyncClient:
        transport = servers[profile.id]
        return httpx.AsyncClient(transport=transport)

    app.state.storage_manager._webdav_client_factory = _client_factory
    client = TestClient(app, follow_redirects=False)
    login = client.post(
        "/v1/auth/internal/login",
        json={"username": "coreadmin", "password": "corepass123"},
    )
    assert login.status_code == 200
    return client, app


def test_webdav_capability_gate_migration_fault_recovery_and_cas_alias_delivery(
    tmp_path: Path,
) -> None:
    expected_basic = "Basic " + base64.b64encode(b"ops:pass123").decode("ascii")
    dav_broken = FakeWebDavServer(expected_auth=expected_basic, support_range=False)
    dav_main = FakeWebDavServer(expected_auth=expected_basic, support_conditional_put=True)
    dav_cas = FakeWebDavServer(expected_auth="Bearer cas-tok", support_conditional_put=False)

    client, app = _make_webdav_app(
        {
            "dav-broken": dav_broken,
            "dav-main": dav_main,
            "dav-cas": dav_cas,
        }
    )
    manager: StorageManager = app.state.storage_manager

    # 1. Create source local-fs, broken WebDAV (no Range), healthy WebDAV, and non-conditional WebDAV.
    assert (
        client.post(
            "/v1/admin/storage/profiles",
            json={
                "id": "local-src",
                "name": "Local Source",
                "backend": "local",
                "config": {
                    "mode": "filesystem",
                    "root": str(tmp_path / "src"),
                    "public_base_url": "https://old.example.com/packages",
                },
            },
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/admin/storage/profiles",
            json={
                "id": "dav-broken",
                "name": "Broken WebDAV",
                "backend": "webdav",
                "config": {
                    "base_url": "https://broken.example.com/dav",
                    "root_path": "pkg",
                    "auth_type": "basic",
                },
                "secret": {"username": "ops", "password": "pass123"},
            },
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/admin/storage/profiles",
            json={
                "id": "dav-main",
                "name": "Healthy WebDAV",
                "backend": "webdav",
                "config": {
                    "base_url": "https://dav.example.com/dav",
                    "root_path": "astrhub/main",
                    "auth_type": "basic",
                },
                "secret": {"username": "ops", "password": "pass123"},
            },
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/admin/storage/profiles",
            json={
                "id": "dav-cas",
                "name": "Non-Conditional WebDAV",
                "backend": "webdav",
                "config": {
                    "base_url": "https://cas.example.com/dav",
                    "root_path": "astrhub/cas",
                    "auth_type": "bearer",
                },
                "secret": {"token": "cas-tok"},
            },
        ).status_code
        == 201
    )

    # 2. Broken WebDAV (missing 206 Range support) fails test AND is rejected by bind_purpose (409)!
    broken_test = client.post("/v1/admin/storage/profiles/dav-broken/test")
    assert broken_test.status_code == 200
    assert broken_test.json()["ok"] is False
    assert broken_test.json()["checks"]["range"] is False

    broken_bind = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "dav-broken"},
    )
    assert broken_bind.status_code == 409
    assert broken_bind.json()["code"] == "webdav_capability_check_failed"

    # 3. Bind local-src to published, dav-main to quarantine (auto-probes dav-main cleanly!).
    assert (
        client.put(
            "/v1/admin/storage/bindings/artifact-published",
            json={"profile_id": "local-src"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/v1/admin/storage/bindings/artifact-quarantine",
            json={"profile_id": "dav-main"},
        ).status_code
        == 200
    )

    pkg1_key = "alice/p1/1.0.0/p1-1.0.0-11112222.zip"
    pkg2_key = "alice/p2/1.0.0/p2-1.0.0-33334444.zip"
    pkg1_bytes = b"PK\x03\x04WEBDAV-PKG-1-DATA-0123456789" * 8
    pkg2_bytes = b"PK\x03\x04WEBDAV-PKG-2-DATA-9876543210" * 8
    private_bytes = b"PRIVATE-WEBDAV-QUARANTINE"

    async def seed() -> tuple[str, str, str]:
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=pkg1_key,
            data=pkg1_bytes,
            content_type="application/zip",
        )
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=pkg2_key,
            data=pkg2_bytes,
            content_type="application/zip",
        )
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_QUARANTINE,
            key="artifacts/q1/source.zip",
            data=private_bytes,
            content_type="application/zip",
        )
        objs = await manager.repository.list_objects()
        by_key = {o.key: o.id for o in objs}
        return by_key[pkg1_key], by_key[pkg2_key], by_key["artifacts/q1/source.zip"]

    pkg1_id, pkg2_id, quarantine_id = asyncio.run(seed())

    # Private quarantine object on WebDAV must never be exposed via public endpoints.
    assert client.get(f"/v1/storage/public/{quarantine_id}").status_code == 404
    assert client.get("/packages/artifacts/q1/source.zip").status_code == 404

    # 4. Switch artifact-published from local-src -> dav-main with simulated network disconnect on pkg2!
    rebind_res = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "dav-main", "run_immediately": False},
    )
    assert rebind_res.status_code == 200, rebind_res.text
    mig_id = rebind_res.json()["migration"]["id"]

    # Inject a transient timeout for pkg2 on dav_main.
    dav_main.fail_put_once_substring = "p2-1.0.0-33334444.zip"
    first_run = asyncio.run(manager.run_migration(mig_id))
    assert first_run.status == StorageMigrationStatus.COMPLETED_WITH_ERRORS
    assert first_run.completed_count == 1
    assert first_run.failed_count == 1

    # Even while WebDAV is completely disconnected, unmigrated pkg2 stays readable from local-src!
    dav_main.disconnected = True
    assert client.get(f"/v1/storage/public/{pkg2_id}").content == pkg2_bytes

    # Reconnect WebDAV -> retry failed -> ONLY pkg2 retries and finishes cleanly.
    dav_main.disconnected = False
    assert client.post(f"/v1/admin/storage/migrations/{mig_id}/retry-failed").status_code == 200
    second_run = asyncio.run(manager.run_migration(mig_id))
    assert second_run.status == StorageMigrationStatus.COMPLETED
    assert second_run.completed_count == 2
    assert second_run.failed_count == 0

    detail = client.get(f"/v1/admin/storage/migrations/{mig_id}").json()
    by_key = {item["source_key"]: item for item in detail["items"]}
    assert by_key[pkg1_key]["attempts"] == 1
    assert by_key[pkg2_key]["attempts"] == 2
    assert by_key[pkg2_key]["status"] == StorageMigrationItemStatus.COMPLETED.value

    # Delete local-src files to prove both same-origin & /packages Range delivery read from WebDAV!
    (tmp_path / "src" / pkg1_key).unlink()
    (tmp_path / "src" / pkg2_key).unlink()

    res_pub = client.get(f"/v1/storage/public/{pkg1_id}")
    assert res_pub.status_code == 200
    assert res_pub.content == pkg1_bytes
    assert res_pub.headers["cache-control"] == "public, max-age=3600"

    res_range = client.get(f"/packages/{pkg2_key}", headers={"Range": "bytes=4-23"})
    assert res_range.status_code == 206
    assert res_range.content == pkg2_bytes[4:24]
    assert res_range.headers["cache-control"] == "public, max-age=3600"

    # 5. Migrate from dav-main -> dav-cas (non-conditional WebDAV storing content-hashed keys!).
    cas_test = client.post("/v1/admin/storage/profiles/dav-cas/test")
    assert cas_test.status_code == 200
    assert cas_test.json()["ok"] is True
    assert cas_test.json()["checks"]["conditional_create"] is False
    assert cas_test.json()["checks"]["overwrite_protection"] is True

    cas_rebind = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "dav-cas", "run_immediately": True},
    )
    assert cas_rebind.status_code == 200, cas_rebind.text
    assert cas_rebind.json()["migration"]["status"] == "completed"

    # Verify stored key on dav-cas is content-hashed AND historical /packages/{pkg1_key} still works!
    expected_pkg1_cas = content_addressed_key(pkg1_key, hashlib.sha256(pkg1_bytes).hexdigest())
    assert f"astrhub/cas/{expected_pkg1_cas}" in dav_cas.files
    assert client.get(f"/packages/{pkg1_key}").content == pkg1_bytes
    assert client.get(f"/v1/storage/public/{pkg1_id}").content == pkg1_bytes
