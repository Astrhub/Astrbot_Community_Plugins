from __future__ import annotations

import asyncio
import hashlib

from dataclasses import replace

from app.config import load_settings
from app.downloads import build_package_download_url, normalize_publication_download_url
from app.main import create_app
from app.store import InMemoryMarketStore
from app.object_storage import StoragePurpose
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


def test_published_package_downloads_are_same_origin_and_counted(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = InMemoryMarketStore()
    store.artifact_runtime = None
    app = create_app(settings, store)
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
    }
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
                "id": "artifact_download1",
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
        await app.state.storage_manager.service.put_object(
            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
            key=published_key,
            data=payload,
            content_type="application/zip",
            owner_type="artifact",
            owner_id=artifact["id"],
        )
        return artifact["id"]

    artifact_id = asyncio.run(seed())
    try:
        with TestClient(app) as client:
            plugins = client.get("/v1/plugins").json()["items"]
            assert plugins[0]["download_url"] == (
                f"https://plugins.example.test/packages/{plugin['id']}/{artifact_id}"
            )
            assert plugins[0]["download_count"] == 0

            url = f"/packages/{plugin['id']}/{artifact_id}"
            first = client.get(url)
            assert first.status_code == 200, first.text
            assert first.content == payload
            assert first.headers["x-download-count"] == "1"
            assert first.headers["cache-control"] == "public, max-age=3600"

            partial = client.get(url, headers={"Range": "bytes=0-15"})
            assert partial.status_code == 206
            assert partial.content == payload[:16]
            assert partial.headers["x-download-count"] == "1"

            head = client.head(url)
            assert head.status_code == 200
            assert "x-download-count" not in head.headers
            assert client.get(url + "/wrong").status_code == 404
            assert client.get("/packages/other-plugin/" + artifact_id).status_code == 404

            updated_plugins = client.get("/v1/plugins").json()["items"]
            assert updated_plugins[0]["download_count"] == 2
            assert store.state["plugins"][0]["download_count"] == 2
    finally:
        asyncio.run(app.state.artifact_runtime.close())
        asyncio.run(app.state.storage_manager.close())


def test_revoked_or_unpublished_packages_are_not_downloadable(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = InMemoryMarketStore()
    store.artifact_runtime = None
    app = create_app(settings, store)

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
            assert "download_count" not in (store.state.get("plugins") or [{}])[0]
    finally:
        asyncio.run(app.state.artifact_runtime.close())
        asyncio.run(app.state.storage_manager.close())
