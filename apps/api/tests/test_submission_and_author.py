from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import load_settings
from app.store import InMemoryMarketStore
from tests.github_access_helpers import install_github_access_double


def make_client() -> TestClient:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "CORS_ORIGIN": "http://127.0.0.1:5173",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }
    )
    app = main_module.create_app(settings=settings, store=InMemoryMarketStore())
    install_github_access_double(app)
    return TestClient(app)


def test_list_author_plugins_returns_only_listed_plugins_for_author():
    with make_client() as client:
        store = client.app.state.store
        user = store.upsert_github_user({"login": "alice", "name": "Alice"})
        listed_plugin = store.register_plugin(
            user,
            {
                "name": "astrbot_plugin_listed",
                "desc": "Listed plugin",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_listed",
            },
        )
        store.update_plugin_status(listed_plugin["id"], "listed", user["id"])
        store.register_plugin(
            user,
            {
                "name": "astrbot_plugin_pending",
                "desc": "Pending plugin",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_pending",
            },
        )
        other_user = store.upsert_github_user({"login": "bob", "name": "Bob"})
        other_plugin = store.register_plugin(
            other_user,
            {
                "name": "astrbot_plugin_other",
                "desc": "Other plugin",
                "author": "Bob",
                "repo": "https://github.com/bob/astrbot_plugin_other",
            },
        )
        store.update_plugin_status(other_plugin["id"], "listed", other_user["id"])

        response = client.get("/v1/authors/alice/plugins")
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "alice"
        assert data["github_url"] == "https://github.com/alice"
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "astrbot_plugin_listed"


def test_list_author_plugins_merges_legacy_github_login_and_avoids_extra_query_when_same():
    with make_client() as client:
        store = client.app.state.store
        calls: list[str] = []
        original_list_author_plugins = store.list_author_plugins

        def tracked_list_author_plugins(username: str):
            calls.append(username)
            return original_list_author_plugins(username)

        store.list_author_plugins = tracked_list_author_plugins

        same_user = store.upsert_github_user({"id": "801", "login": "same_name", "name": "Same"})
        same_plugin = store.register_plugin(
            same_user,
            {
                "name": "astrbot_plugin_same_name",
                "desc": "Same name",
                "author": "Same",
                "repo": "https://github.com/same_name/astrbot_plugin_same_name",
            },
        )
        store.update_plugin_status(same_plugin["id"], "listed", same_user["id"])

        same_resp = client.get("/v1/authors/same_name/plugins")
        assert same_resp.status_code == 200
        assert calls == ["same_name"]

        calls.clear()
        renamed = store.upsert_github_user({"id": "802", "login": "LegacyGH", "name": "Renamed"})
        for stored_user in store.state["users"]:
            if stored_user["id"] == renamed["id"]:
                stored_user["public_username"] = "PublicName"
                renamed = dict(stored_user)
                break
        newer = store.register_plugin(
            renamed,
            {
                "name": "astrbot_plugin_newer",
                "desc": "Newer canonical",
                "author": "Renamed",
                "repo": "https://github.com/LegacyGH/astrbot_plugin_newer",
            },
        )
        older_legacy = store.register_plugin(
            renamed,
            {
                "name": "astrbot_plugin_older_legacy",
                "desc": "Older legacy",
                "author": "Renamed",
                "repo": "https://github.com/LegacyGH/astrbot_plugin_older_legacy",
            },
        )
        store.update_plugin_status(newer["id"], "listed", renamed["id"])
        store.update_plugin_status(older_legacy["id"], "listed", renamed["id"])
        for item in store.state["plugins"]:
            if item["id"] == newer["id"]:
                item["owner_username"] = "PublicName"
                item["owner_github_login"] = "LegacyGH"
                item["updated_at"] = "2026-11-24T12:00:00Z"
            elif item["id"] == older_legacy["id"]:
                item["owner_username"] = ""
                item["owner_github_login"] = "LegacyGH"
                item["updated_at"] = "2026-11-23T12:00:00Z"

        merged_resp = client.get("/v1/authors/PublicName/plugins")
        assert merged_resp.status_code == 200
        assert calls == ["PublicName", "LegacyGH"]
        assert [item["name"] for item in merged_resp.json()["items"]] == [
            "astrbot_plugin_newer",
            "astrbot_plugin_older_legacy",
        ]


def test_metadata_preview_strict_validation_rejects_missing_required_fields(monkeypatch):
    async def mock_fetch_metadata(*args, **kwargs):
        return {"name": "astrbot_plugin_test"}

    monkeypatch.setattr(main_module, "fetch_github_plugin_metadata_files", mock_fetch_metadata)

    with pytest.raises(main_module.GithubMetadataError) as exc_info:
        import asyncio

        asyncio.run(
            main_module.fetch_plugin_submission_metadata_preview(
                "https://github.com/alice/astrbot_plugin_test",
                load_settings({"GITHUB_METADATA_SYNC_ENABLED": "true"}),
                {"login": "alice"},
                strict=True,
            )
        )
    assert exc_info.value.status_code == 400
    assert "插件简介" in exc_info.value.message
    assert "作者" in exc_info.value.message


def test_metadata_preview_strict_validation_accepts_complete_metadata(monkeypatch):
    async def mock_fetch_metadata(*args, **kwargs):
        return {
            "name": "astrbot_plugin_test",
            "desc": "Test description",
            "author": "Alice",
        }

    monkeypatch.setattr(main_module, "fetch_github_plugin_metadata_files", mock_fetch_metadata)

    import asyncio

    result = asyncio.run(
        main_module.fetch_plugin_submission_metadata_preview(
            "https://github.com/alice/astrbot_plugin_test",
            load_settings({"GITHUB_METADATA_SYNC_ENABLED": "true"}),
            {"login": "alice"},
            strict=True,
        )
    )
    assert result["name"] == "astrbot_plugin_test"
    assert result["desc"] == "Test description"
    assert result["author"] == "Alice"
    assert result["repo"] == "https://github.com/alice/astrbot_plugin_test"
