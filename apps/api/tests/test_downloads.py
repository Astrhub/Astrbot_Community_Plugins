from __future__ import annotations

import asyncio
import hashlib

from dataclasses import replace

from app import downloads as download_helpers
from app.config import load_settings
from app.downloads import (
    build_package_download_url,
    normalize_publication_download_url,
    requester_hash,
)
from app.main import create_app
from app.store import InMemoryMarketStore
from app.object_storage import StoragePurpose
from app.object_storage import routes as object_storage_routes
from fastapi import Request
from fastapi.testclient import TestClient

from test_artifact_pipeline import plugin_zip


def _settings(tmp_path):
    return load_settings().with_updates(
        oauth_credential_key="download-test-master-key",
        artifacts=replace(
            load_settings().artifacts,
            enabled=True,
            storage_backend="local",
            cdn_base_url="https://cdn.example.test",
            local_root=str(tmp_path / "artifacts"),
        ),
        database_url="postgresql://unused",
        web_url="https://plugins.example.test",
    )


def _plugin(**overrides):
    plugin = {
        "id": "astrbot_plugin_demo",
        "name": "astrbot_plugin_demo",
        "display_name": "Demo",
        "desc": "Demo plugin",
        "author": "Alice",
        "repo": "https://github.com/alice/astrbot_plugin_demo",
        "version": "v1.0.0",
        "repo_version": "v1.0.0",
        "status": "listed",
        "cdn_enabled": True,
        "owner_user_id": "owner-1",
        "owner_github_login": "Alice",
    }
    plugin.update(overrides)
    return plugin


def test_download_url_ignores_legacy_backend_location() -> None:
    publication = {
        "id": "artifact-1",
        "plugin_id": "astrbot_plugin_demo",
        "download_url": "https://imgbed.example.test/file/astrbot/old/plugin.zip",
    }

    assert normalize_publication_download_url(publication, "https://plugins.example.test") == (
        "https://plugins.example.test/packages/astrbot_plugin_demo/artifact-1"
    )
    assert build_package_download_url("astrbot_plugin_demo", "artifact-1") == (
        "/packages/astrbot_plugin_demo/artifact-1"
    )


def test_requester_hash_trusts_proxy_headers_only_from_loopback() -> None:
    forwarded_headers = [(b"x-forwarded-for", b"198.51.100.1, 203.0.113.11")]
    proxied = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 50000),
            "headers": forwarded_headers,
        }
    )
    direct = Request({"type": "http", "client": ("203.0.113.11", 50000), "headers": []})
    untrusted = Request(
        {
            "type": "http",
            "client": ("203.0.113.12", 50000),
            "headers": forwarded_headers,
        }
    )
    assert requester_hash(proxied, "plugin-1") == requester_hash(direct, "plugin-1")
    assert requester_hash(untrusted, "plugin-1") != requester_hash(direct, "plugin-1")


def test_in_memory_download_window_expires_after_one_minute(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(download_helpers.time, "monotonic", lambda: clock[0])
    download_helpers._requester_windows.clear()

    async def scenario() -> None:
        assert await download_helpers.should_count_in_memory("plugin-1", "requester-1")
        clock[0] += 59
        assert not await download_helpers.should_count_in_memory("plugin-1", "requester-1")
        clock[0] += 2
        assert await download_helpers.should_count_in_memory("plugin-1", "requester-1")

    try:
        asyncio.run(scenario())
    finally:
        download_helpers._requester_windows.clear()


def test_published_package_downloads_are_same_origin_and_counted(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    store = InMemoryMarketStore()
    store.artifact_runtime = None
    app = create_app(settings, store)
    plugin = _plugin(download_url="https://imgbed.example.test/file/legacy/plugin.zip")
    store.state["plugins"].append(store._normalize_plugin(plugin))
    payload = plugin_zip()
    published_key = "10001/astrbot_plugin_demo/v1.0.0/astrbot_plugin_demo-abcdef1234.zip"

    async def seed() -> str:
        await app.state.storage_manager.initialize()
        runtime = app.state.artifact_runtime
        store.artifact_runtime = runtime
        await runtime.start(store)
        artifact = await runtime.repository.create_artifact(
            {
                "id": "artifact-download1",
                "plugin_id": plugin["id"],
                "submitted_by": "owner-1",
                "source_type": "github",
                "quarantine_key": "quarantine/artifact.zip",
                "source_repo": plugin["repo"],
                "source_commit": "a" * 40,
                "version": "v1.0.0",
                "normalized_version": "1.0.0",
                "archive_sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "cdn_settings": {"cdn_enabled": True, "cdn_generation": 0},
            }
        )
        await runtime.repository.transition_publication_status(artifact["id"], "publishing")
        await runtime.repository.publish_artifact(
            artifact["id"],
            expected_repo_version="v1.0.0",
            published_key=published_key,
            download_url="https://imgbed.example.test/file/legacy/plugin.zip",
        )
        assert runtime.repository.artifacts[artifact["id"]]["download_url"] is None
        await app.state.storage_manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=published_key,
            data=payload,
            content_type="application/zip",
            owner_type="artifact",
            owner_id=artifact["id"],
        )
        manager = app.state.storage_manager
        stored = await manager.repository.find_active_object_by_purpose_and_key(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=published_key,
        )
        assert stored is not None
        legacy_record = replace(stored, owner_type="legacy_backfill", owner_id="")
        await manager.repository.upsert_object(legacy_record)
        manager.service.cache_object_record(legacy_record)
        return artifact["id"]

    artifact_id = asyncio.run(seed())
    try:
        with TestClient(app, client=("203.0.113.10", 50000)) as client:
            plugins = client.get("/v1/plugins").json()["items"]
            assert plugins[0]["download_url"] == (
                f"https://plugins.example.test/packages/{plugin['id']}/{artifact_id}"
            )
            assert plugins[0]["download_count"] == 0

            expected_url = f"https://plugins.example.test/packages/{plugin['id']}/{artifact_id}"
            for path in (
                f"/v1/plugins/{plugin['id']}",
                f"/v1/plugins/by-author/Alice/{plugin['name']}",
            ):
                detail = client.get(path)
                assert detail.status_code == 200, detail.text
                assert detail.json()["download_url"] == expected_url

            author_listing = client.get("/v1/authors/Alice/plugins")
            assert author_listing.status_code == 200, author_listing.text
            assert author_listing.json()["items"][0]["download_url"] == expected_url

            url = f"/packages/{plugin['id']}/{artifact_id}"
            first = client.get(url)
            assert first.status_code == 200, first.text
            assert first.content == payload
            assert first.headers["x-download-count"] == "1"
            assert first.headers["cache-control"] == "public, max-age=0, must-revalidate"

            partial = client.get(url, headers={"Range": "bytes=0-15"})
            assert partial.status_code == 206
            assert partial.content == payload[:16]
            assert "x-download-count" not in partial.headers

            head = client.head(url)
            assert head.status_code == 200
            assert "x-download-count" not in head.headers
            assert client.get(url + "/wrong").status_code == 404
            assert client.get("/packages/other-plugin/" + artifact_id).status_code == 404

            with TestClient(app, client=("203.0.113.11", 50000)) as other_client:
                legacy = other_client.get(f"/packages/{published_key}")
                assert legacy.status_code == 200
                assert legacy.content == payload
                assert legacy.headers["x-download-count"] == "1"
                assert legacy.headers["cache-control"] == "public, max-age=0, must-revalidate"

            updated_plugins = client.get("/v1/plugins").json()["items"]
            assert updated_plugins[0]["download_count"] == 2
            assert store.state["plugins"][0]["download_count"] == 2

            original_serve = object_storage_routes._serve_public_object

            async def revoke_during_read(*args, **kwargs):
                response = await original_serve(*args, **kwargs)
                await app.state.artifact_runtime.repository.transition_publication_status(
                    artifact_id, "revoking"
                )
                return response

            monkeypatch.setattr(object_storage_routes, "_serve_public_object", revoke_during_read)
            assert client.get(url).status_code == 404
            assert store.state["plugins"][0]["download_count"] == 2
    finally:
        asyncio.run(app.state.artifact_runtime.close())
        asyncio.run(app.state.storage_manager.close())


def test_revoked_or_unpublished_packages_are_not_downloadable(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = InMemoryMarketStore()
    store.artifact_runtime = None
    app = create_app(settings, store)
    store.state["plugins"].append(store._normalize_plugin(_plugin()))

    async def seed() -> None:
        await app.state.storage_manager.initialize()
        runtime = app.state.artifact_runtime
        store.artifact_runtime = runtime
        await runtime.start(store)
        await runtime.repository.create_artifact(
            {
                "id": "artifact_unpublished",
                "plugin_id": "astrbot_plugin_demo",
                "submitted_by": "owner-1",
                "source_type": "github",
                "quarantine_key": "quarantine/artifact.zip",
                "source_repo": "https://github.com/alice/astrbot_plugin_demo",
                "source_commit": "a" * 40,
                "version": "v1.0.0",
                "normalized_version": "1.0.0",
                "archive_sha256": "a" * 64,
                "size_bytes": 1,
                "cdn_settings": {"cdn_enabled": True},
            }
        )

    asyncio.run(seed())
    try:
        with TestClient(app) as client:
            response = client.get("/packages/astrbot_plugin_demo/artifact_unpublished")
            assert response.status_code == 404
            assert store.state["plugins"][0]["download_count"] == 0
    finally:
        asyncio.run(app.state.artifact_runtime.close())
        asyncio.run(app.state.storage_manager.close())


def test_public_endpoints_hide_legacy_urls_when_artifacts_are_disabled(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings = settings.with_updates(artifacts=replace(settings.artifacts, enabled=False))
    store = InMemoryMarketStore()
    store.artifact_runtime = None
    app = create_app(settings, store)
    store.state["plugins"].append(
        store._normalize_plugin(
            _plugin(download_url="https://imgbed.example.test/file/legacy/plugin.zip")
        )
    )

    with TestClient(app) as client:
        assert client.get("/v1/plugins").json()["items"][0]["download_url"] == ""
        for path in (
            "/v1/plugins/astrbot_plugin_demo",
            "/v1/plugins/by-author/Alice/astrbot_plugin_demo",
        ):
            detail = client.get(path)
            assert detail.status_code == 200, detail.text
            assert detail.json()["download_url"] == ""
        author_listing = client.get("/v1/authors/Alice/plugins")
        assert author_listing.status_code == 200, author_listing.text
        assert author_listing.json()["items"][0]["download_url"] == ""
        feed = client.get("/plugins.json").json()
        assert "download_url" not in feed["Alice/astrbot_plugin_demo"]
