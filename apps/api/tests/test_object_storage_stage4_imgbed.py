"""Stage 4 tests for CloudFlare ImgBed driver, plugin ZIP gates, migration, and decommission drill."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import load_settings
from app.main import create_app
from app.object_storage import (
    AVATAR_VARIANT_SIZES,
    ImgBedObjectStorageDriver,
    ObjectStorageError,
    StorageManager,
    StoragePurpose,
    build_imgbed_variant_url,
    validate_imgbed_base_url,
    validate_imgbed_expire,
    validate_imgbed_folder,
    validate_imgbed_scopes,
)
from app.store import InMemoryMarketStore


def _parse_multipart_file(content_type: str, body: bytes) -> tuple[str, bytes, str]:
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    assert match is not None, f"Missing multipart boundary in {content_type}"
    boundary = match.group(1).encode("ascii")
    parts = body.split(b"--" + boundary)
    for part in parts:
        if b'name="file"' not in part:
            continue
        header_raw, _, payload_raw = part.partition(b"\r\n\r\n")
        payload = payload_raw.removesuffix(b"\r\n")
        header_text = header_raw.decode("utf-8", errors="replace")
        fn_match = re.search(r'filename="([^"]+)"', header_text)
        ct_match = re.search(r"Content-Type:\s*([^\r\n]+)", header_text, re.IGNORECASE)
        filename = fn_match.group(1) if fn_match else "upload.bin"
        mime = ct_match.group(1).strip() if ct_match else "application/octet-stream"
        return filename, payload, mime
    raise AssertionError("Multipart payload did not contain a file field")


class FakeImgBedServer(httpx.AsyncBaseTransport):
    """Stateful CloudFlare ImgBed HTTP mock supporting /upload, /file/*, and /api/manage/*."""

    def __init__(
        self,
        *,
        expected_token: str,
        granted_scopes: set[str],
        allow_zip: bool = True,
        support_range: bool = True,
        cache_control: str = "public, max-age=86400",
        foreign_files: dict[str, tuple[bytes, str]] | None = None,
    ) -> None:
        self.expected_token = expected_token
        self.granted_scopes = set(granted_scopes)
        self.allow_zip = allow_zip
        self.support_range = support_range
        self.cache_control = cache_control
        self.files: dict[str, tuple[bytes, str]] = dict(foreign_files or {})
        self.request_log: list[tuple[str, str, dict[str, str]]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.request_log.append((request.method, str(request.url), headers))
        method = request.method.upper()

        if path == "/upload" and method == "POST":
            if headers.get("authorization") != f"Bearer {self.expected_token}":
                return httpx.Response(401, json={"error": "Unauthorized"}, request=request)
            if "upload" not in self.granted_scopes:
                return httpx.Response(403, json={"error": "Missing upload scope"}, request=request)
            body = await request.aread()
            filename, file_bytes, mime = _parse_multipart_file(
                headers.get("content-type", ""),
                body,
            )
            if filename.lower().endswith(".zip") and not self.allow_zip:
                return httpx.Response(415, json={"error": "ZIP not allowlisted"}, request=request)
            folder = str(request.url.params.get("uploadFolder") or "").strip("/")
            full_path = f"{folder}/{filename}" if folder else filename
            self.files[full_path] = (file_bytes, mime)
            # Intentionally return a raw publicUrl alongside src to prove driver uses /file/... src!
            return httpx.Response(
                200,
                json=[
                    {
                        "src": f"/file/{full_path}",
                        "publicUrl": f"https://raw-underlying-r2.internal/direct/{full_path}",
                    }
                ],
                request=request,
            )

        if path.startswith("/file/") and method in {"GET", "HEAD"}:
            rel = path.removeprefix("/file/").strip("/")
            entry = self.files.get(rel)
            if entry is None:
                return httpx.Response(404, text="Not Found", request=request)
            data, mime = entry
            resp_headers = {
                "Content-Type": mime,
                "Content-Length": str(len(data)),
            }
            if self.cache_control:
                resp_headers["Cache-Control"] = self.cache_control
            if method == "HEAD":
                return httpx.Response(200, headers=resp_headers, request=request)
            range_hdr = headers.get("range", "")
            if range_hdr and self.support_range and range_hdr.startswith("bytes="):
                start_s, end_s = range_hdr.removeprefix("bytes=").split("-", 1)
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
                        **resp_headers,
                        "Content-Length": str(len(sliced)),
                        "Content-Range": f"bytes {start}-{end}/{len(data)}",
                    },
                    request=request,
                )
            return httpx.Response(200, content=data, headers=resp_headers, request=request)

        if path == "/api/manage/list" and method == "GET":
            if headers.get("authorization") != f"Bearer {self.expected_token}":
                return httpx.Response(401, json={"error": "Unauthorized"}, request=request)
            if "list" not in self.granted_scopes:
                return httpx.Response(403, json={"error": "Missing list scope"}, request=request)
            # Deliberately return ALL files (simulating token without folder-level isolation!)
            files_list = [
                {"name": name, "metadata": {"FileSize": len(raw)}}
                for name, (raw, _ct) in sorted(self.files.items())
            ]
            return httpx.Response(200, json={"files": files_list}, request=request)

        if path.startswith("/api/manage/delete/") and method == "DELETE":
            if headers.get("authorization") != f"Bearer {self.expected_token}":
                return httpx.Response(401, json={"error": "Unauthorized"}, request=request)
            if "delete" not in self.granted_scopes:
                return httpx.Response(403, json={"error": "Missing delete scope"}, request=request)
            rel = path.removeprefix("/api/manage/delete/").strip("/")
            if rel not in self.files:
                return httpx.Response(404, json={"error": "Not Found"}, request=request)
            self.files.pop(rel, None)
            return httpx.Response(200, json={"deleted": True}, request=request)

        return httpx.Response(404, json={"error": "Unhandled route"}, request=request)


class MissingDirectoryImgBedServer(FakeImgBedServer):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        directory = str(request.url.params.get("dir") or "").strip("/")
        if (
            path == "/api/manage/list"
            and request.method.upper() == "GET"
            and directory
            and not any(name.startswith(f"{directory}/") for name in self.files)
        ):
            headers = {key.lower(): value for key, value in request.headers.items()}
            self.request_log.append((request.method, str(request.url), headers))
            return httpx.Response(400, json={"error": "Directory not found"}, request=request)
        return await super().handle_async_request(request)


def test_imgbed_probe_upload_creates_missing_folder_before_list_acceptance() -> None:
    server = MissingDirectoryImgBedServer(
        expected_token="tok-missing-folder",
        granted_scopes={"upload", "list", "delete"},
    )
    client = httpx.AsyncClient(transport=server)
    driver = ImgBedObjectStorageDriver(
        profile_id="imgbed-new-folder",
        base_url="https://imgbed.example.com",
        token="tok-missing-folder",
        upload_folder="astrhub/new-plugins",
        expire="12h",
        scopes=("upload", "list", "delete"),
        client=client,
    )

    async def run() -> None:
        try:
            probe = await driver.probe_capabilities(test_plugin_zip=True)

            assert probe.reachable is True
            assert probe.upload is True
            assert probe.read is True
            assert probe.delete is True
            assert probe.plugin_zip_ready is True
            assert all(probe.plugin_zip_checks.values())
            assert probe.message == "ImgBed 头像与插件 ZIP 能力验收全部通过"
            upload_urls = [
                httpx.URL(url)
                for method, url, _headers in server.request_log
                if method == "POST" and httpx.URL(url).path == "/upload"
            ]
            assert len(upload_urls) == 2
            assert all(url.params.get("expire") == "12h" for url in upload_urls)
            assert server.files == {}
        finally:
            await client.aclose()

    asyncio.run(run())


def test_imgbed_validators_and_variant_url_helper() -> None:
    assert AVATAR_VARIANT_SIZES == frozenset({64, 128, 256})
    assert validate_imgbed_base_url("https://imgbed.example.com/") == "https://imgbed.example.com"
    assert validate_imgbed_folder("/astrhub/avatars/") == "astrhub/avatars"
    assert validate_imgbed_scopes(["upload"]) == ("upload",)
    assert validate_imgbed_scopes(["upload", "list", "delete"]) == ("upload", "list", "delete")
    assert validate_imgbed_expire(" 7d ") == "7d"
    assert validate_imgbed_expire("87600h") == "87600h"
    assert validate_imgbed_expire("forever") == "forever"
    assert validate_imgbed_expire("") == ""
    with pytest.raises(ObjectStorageError) as expire_exc:
        validate_imgbed_expire("3651d")
    assert expire_exc.value.code == "imgbed_expire_invalid"

    with pytest.raises(ObjectStorageError) as http_exc:
        validate_imgbed_base_url("http://imgbed.example.com")
    assert http_exc.value.code == "imgbed_https_required"

    with pytest.raises(ObjectStorageError) as priv_exc:
        validate_imgbed_base_url("https://127.0.0.1")
    assert priv_exc.value.code == "webdav_private_host_forbidden"

    with pytest.raises(ObjectStorageError) as cred_exc:
        validate_imgbed_base_url("https://u:p@imgbed.example.com")
    assert cred_exc.value.code == "imgbed_credentials_in_url_forbidden"

    with pytest.raises(ObjectStorageError) as folder_exc:
        validate_imgbed_folder("astrhub/../escape")
    assert folder_exc.value.code == "imgbed_folder_invalid"

    with pytest.raises(ObjectStorageError) as scope_exc:
        validate_imgbed_scopes(["list"])
    assert scope_exc.value.code == "imgbed_scopes_invalid"

    # Variant params ONLY attach to genuine /file/... URLs.
    imgbed_url = "https://imgbed.example.com/file/astrhub/avatars/u1.webp"
    v256 = build_imgbed_variant_url(imgbed_url, width=256, height=256)
    assert v256 == (
        "https://imgbed.example.com/file/astrhub/avatars/u1.webp"
        "?width=256&height=256&fit=cover&fallback=original"
    )
    external_url = "https://avatars.githubusercontent.com/u/12345"
    assert build_imgbed_variant_url(external_url, width=128, height=128) == external_url


def test_imgbed_driver_src_preservation_folder_isolation_and_conflict_detection() -> None:
    server = FakeImgBedServer(
        expected_token="tok-imgbed-full",
        granted_scopes={"upload", "list", "delete"},
        allow_zip=True,
        support_range=True,
        foreign_files={"other-app/private.zip": (b"foreign-secret", "application/zip")},
    )
    client = httpx.AsyncClient(transport=server)
    driver = ImgBedObjectStorageDriver(
        profile_id="imgbed-pkg",
        base_url="https://imgbed.example.com",
        token="tok-imgbed-full",
        upload_folder="astrhub/plugins",
        expire="7d",
        scopes=("upload", "list", "delete"),
        client=client,
    )

    payload_1 = b"PK\x03\x04IMGBED-ARCHIVE-V1-0123456789"
    payload_2 = b"PK\x03\x04IMGBED-ARCHIVE-V2-CONFLICT!!"
    sha_1 = hashlib.sha256(payload_1).hexdigest()

    async def run() -> None:
        try:
            stored = await driver.put_object(
                "alice/demo/1.0.0/demo-1.0.0-aabbccdd.zip",
                payload_1,
                content_type="application/zip",
                expected_sha256=sha_1,
                if_absent=True,
            )
            # Must save /file/... src and NEVER substitute raw underlying publicUrl.
            assert stored.metadata["imgbed_src"] == (
                "/file/astrhub/plugins/alice/demo/1.0.0/demo-1.0.0-aabbccdd.zip"
            )
            assert stored.public_url == (
                "https://imgbed.example.com/file/astrhub/plugins/"
                "alice/demo/1.0.0/demo-1.0.0-aabbccdd.zip"
            )
            assert "raw-underlying-r2" not in stored.public_url

            # Token must only appear in Authorization header, never in request URLs.
            for _method, req_url, _hdrs in server.request_log:
                assert "tok-imgbed-full" not in req_url

            upload_urls = [
                httpx.URL(url)
                for method, url, _headers in server.request_log
                if method == "POST" and httpx.URL(url).path == "/upload"
            ]
            assert upload_urls
            assert all(url.params.get("expire") == "7d" for url in upload_urls)
            # Idempotent re-put with same bytes succeeds.
            same = await driver.put_object(
                "alice/demo/1.0.0/demo-1.0.0-aabbccdd.zip",
                payload_1,
                if_absent=True,
            )
            assert same.sha256 == sha_1

            # Same deterministic key with different bytes raises object_key_conflict!
            with pytest.raises(ObjectStorageError) as conflict_exc:
                await driver.put_object(
                    "alice/demo/1.0.0/demo-1.0.0-aabbccdd.zip",
                    payload_2,
                    if_absent=True,
                )
            assert conflict_exc.value.code == "object_key_conflict"

            # Service-layer isolation: list_objects hides other-app/private.zip outside upload_folder.
            listed = await driver.list_objects()
            assert [entry.key for entry in listed] == ["alice/demo/1.0.0/demo-1.0.0-aabbccdd.zip"]

            # Delete removes only our object; foreign object untouched.
            await driver.delete_object("alice/demo/1.0.0/demo-1.0.0-aabbccdd.zip")
            assert await driver.stat_object("alice/demo/1.0.0/demo-1.0.0-aabbccdd.zip") is None
            assert "other-app/private.zip" in server.files
        finally:
            await client.aclose()

    asyncio.run(run())


def _make_imgbed_app(
    servers: dict[str, FakeImgBedServer],
) -> tuple[TestClient, Any]:
    store = InMemoryMarketStore()
    store.create_internal_admin("coreadmin", hash_password("corepass123"))
    settings = load_settings().with_updates(
        oauth_credential_key="imgbed-stage4-master-key",
    )
    app = create_app(settings=settings, store=store)

    def _client_factory(profile: Any, _cfg: dict[str, Any]) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=servers[profile.id])

    app.state.storage_manager._imgbed_client_factory = _client_factory
    client = TestClient(app, follow_redirects=False)
    login = client.post(
        "/v1/auth/internal/login",
        json={"username": "coreadmin", "password": "corepass123"},
    )
    assert login.status_code == 200
    return client, app


def test_imgbed_purpose_gates_migration_history_url_and_controlled_decommission_drill(
    tmp_path: Path,
) -> None:
    avatar_server = FakeImgBedServer(
        expected_token="tok-avatar-upload-only",
        granted_scopes={"upload"},
        allow_zip=False,
    )
    zip_blocked_server = FakeImgBedServer(
        expected_token="tok-no-zip",
        granted_scopes={"upload", "list", "delete"},
        allow_zip=False,
    )
    plugins_server = FakeImgBedServer(
        expected_token="tok-plugins-full",
        granted_scopes={"upload", "list", "delete"},
        allow_zip=True,
        support_range=True,
        cache_control="public, max-age=86400",
    )

    client, app = _make_imgbed_app(
        {
            "imgbed-avatar": avatar_server,
            "imgbed-no-zip": zip_blocked_server,
            "imgbed-plugins": plugins_server,
        }
    )
    manager: StorageManager = app.state.storage_manager
    legacy_root = tmp_path / "legacy-old-pkg"

    # 1. Create legacy local published profile, local quarantine profile, and 3 ImgBed profiles.
    for payload in (
        {
            "id": "legacy-old-pkg",
            "name": "Legacy Local Packages",
            "backend": "local",
            "config": {
                "mode": "filesystem",
                "root": str(legacy_root),
                "public_base_url": "https://plugins.eloina.cn/packages",
            },
        },
        {
            "id": "local-quarantine",
            "name": "Local Quarantine",
            "backend": "local",
            "config": {"mode": "filesystem", "root": str(tmp_path / "local-q")},
        },
        {
            "id": "imgbed-avatar",
            "name": "ImgBed Avatar (upload-only)",
            "backend": "imgbed",
            "config": {
                "base_url": "https://img.example.com",
                "upload_folder": "astrhub/avatars",
                "scopes": ["upload"],
                "image_variants_enabled": True,
            },
            "secret": {"token": "tok-avatar-upload-only"},
        },
        {
            "id": "imgbed-no-zip",
            "name": "ImgBed Without ZIP Allowlist",
            "backend": "imgbed",
            "config": {
                "base_url": "https://nozip.example.com",
                "upload_folder": "astrhub/nozip",
                "scopes": ["upload", "list", "delete"],
            },
            "secret": {"token": "tok-no-zip"},
        },
        {
            "id": "imgbed-plugins",
            "name": "ImgBed Published Plugins",
            "backend": "imgbed",
            "config": {
                "base_url": "https://pkg-imgbed.example.com",
                "upload_folder": "astrhub/plugins",
                "expire": "30d",
                "scopes": ["upload", "list", "delete"],
            },
            "secret": {"token": "tok-plugins-full"},
        },
    ):
        res = client.post("/v1/admin/storage/profiles", json=payload)
        assert res.status_code == 201, res.text

    # 2. ImgBed MUST NEVER bind to artifact-quarantine!
    q_blocked = client.put(
        "/v1/admin/storage/bindings/artifact-quarantine",
        json={"profile_id": "imgbed-plugins"},
    )
    assert q_blocked.status_code == 400
    assert q_blocked.json()["code"] == "storage_backend_disallowed_for_purpose"

    # 3. Upload-only ImgBed binds cleanly to avatar, but is rejected for artifact-published!
    avatar_test = client.post("/v1/admin/storage/profiles/imgbed-avatar/test")
    assert avatar_test.status_code == 200
    assert avatar_test.json()["ok"] is True
    assert avatar_test.json()["checks"]["plugin_zip_ready"] is False

    avatar_bind = client.put(
        "/v1/admin/storage/bindings/avatar",
        json={"profile_id": "imgbed-avatar"},
    )
    assert avatar_bind.status_code == 200

    avatar_as_plugin = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "imgbed-avatar"},
    )
    assert avatar_as_plugin.status_code == 409
    assert avatar_as_plugin.json()["code"] == "imgbed_plugin_gate_failed"

    # 4. ImgBed without ZIP upload allowlist is also rejected for artifact-published!
    nozip_bind = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "imgbed-no-zip"},
    )
    assert nozip_bind.status_code == 409
    assert nozip_bind.json()["code"] == "imgbed_plugin_gate_failed"

    # 5. Bind legacy-old-pkg to artifact-published & upload both an avatar and plugin ZIPs.
    assert (
        client.put(
            "/v1/admin/storage/bindings/artifact-published",
            json={"profile_id": "legacy-old-pkg"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/v1/admin/storage/bindings/artifact-quarantine",
            json={"profile_id": "local-quarantine"},
        ).status_code
        == 200
    )

    plugin_key = "alice/astrbot_plugin_hello/1.2.3/astrbot_plugin_hello-1.2.3-abcdef12.zip"
    plugin_bytes = b"PK\x03\x04LEGACY-PLUGIN-RELEASE-PAYLOAD-0123456789" * 6
    avatar_bytes = b"\x89PNG\r\n\x1a\nFAKE-AVATAR-BYTES"

    async def seed_avatar_and_plugin() -> tuple[str, str]:
        avatar_obj = await manager.service.put_object(
            purpose=StoragePurpose.AVATAR,
            key="users/u1/avatar-v1.png",
            data=avatar_bytes,
            content_type="image/png",
        )
        assert avatar_obj.profile_id == "imgbed-avatar"
        assert avatar_obj.public_url == (
            "https://img.example.com/file/astrhub/avatars/users/u1/avatar-v1.png"
        )
        assert build_imgbed_variant_url(avatar_obj.public_url, width=128, height=128).endswith(
            "?width=128&height=128&fit=cover&fallback=original"
        )
        await manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=plugin_key,
            data=plugin_bytes,
            content_type="application/zip",
        )
        objs = await manager.repository.list_objects()
        by_key = {o.key: o.id for o in objs}
        return by_key["users/u1/avatar-v1.png"], by_key[plugin_key]

    avatar_obj_id, plugin_obj_id = asyncio.run(seed_avatar_and_plugin())

    # Before migration, retiring or removing legacy-old-pkg is forbidden (409).
    retire_blocked = client.post("/v1/admin/storage/profiles/legacy-old-pkg/retire")
    assert retire_blocked.status_code == 409
    delete_blocked = client.delete("/v1/admin/storage/profiles/legacy-old-pkg")
    assert delete_blocked.status_code == 409

    # 6. Test and bind imgbed-plugins -> migrates historical ZIP from legacy-old-pkg to ImgBed!
    plugins_test = client.post("/v1/admin/storage/profiles/imgbed-plugins/test")
    assert plugins_test.status_code == 200
    assert plugins_test.json()["ok"] is True
    assert plugins_test.json()["checks"]["plugin_zip_ready"] is True
    plugin_upload_urls = [
        httpx.URL(url)
        for method, url, _headers in plugins_server.request_log
        if method == "POST" and httpx.URL(url).path == "/upload"
    ]
    assert plugin_upload_urls
    assert all(url.params.get("expire") == "30d" for url in plugin_upload_urls)

    migrate_res = client.put(
        "/v1/admin/storage/bindings/artifact-published",
        json={"profile_id": "imgbed-plugins", "run_immediately": True},
    )
    assert migrate_res.status_code == 200, migrate_res.text
    mig = migrate_res.json()["migration"]
    assert mig["status"] == "completed"
    assert mig["completed_count"] == 1
    assert mig["failed_count"] == 0
    assert mig["unknown_objects"] == []

    # Verify avatar binding stayed independent on imgbed-avatar!
    bindings = {
        b["purpose"]: b["profile_id"]
        for b in client.get("/v1/admin/storage/bindings").json()["items"]
    }
    assert bindings["avatar"] == "imgbed-avatar"
    assert bindings["artifact-published"] == "imgbed-plugins"
    assert bindings["artifact-quarantine"] == "local-quarantine"

    # Remove old local ZIP file to prove historical URL reads from ImgBed!
    (legacy_root / plugin_key).unlink()

    hist_get = client.get(f"/packages/{plugin_key}")
    assert hist_get.status_code == 200
    assert hist_get.content == plugin_bytes
    assert hist_get.headers["cache-control"] == "public, max-age=3600"

    hist_range = client.get(f"/packages/{plugin_key}", headers={"Range": "bytes=0-19"})
    assert hist_range.status_code == 206
    assert hist_range.content == plugin_bytes[:20]

    pub_by_id = client.get(f"/v1/storage/public/{plugin_obj_id}")
    assert pub_by_id.status_code == 200
    assert pub_by_id.content == plugin_bytes

    avatar_pub = client.get(f"/v1/storage/public/{avatar_obj_id}")
    assert avatar_pub.status_code == 200
    assert avatar_pub.content == avatar_bytes

    # 7. Controlled decommission drill on legacy-old-pkg: all gates satisfied -> retire & remove succeed!
    retire_res = client.post("/v1/admin/storage/profiles/legacy-old-pkg/retire")
    assert retire_res.status_code == 200, retire_res.text
    assert retire_res.json()["status"] == "retired"
    assert (
        legacy_root.exists()
    )  # Decommissioning never deletes remote/disk directory automatically.

    remove_res = client.delete("/v1/admin/storage/profiles/legacy-old-pkg")
    assert remove_res.status_code == 200, remove_res.text
    assert remove_res.json()["status"] == "removed"

    # Historical URL STILL works even after legacy-old-pkg is retired and removed!
    assert client.get(f"/packages/{plugin_key}").content == plugin_bytes
