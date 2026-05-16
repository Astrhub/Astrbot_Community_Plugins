from __future__ import annotations

import asyncio
import os

from fastapi.testclient import TestClient
import pytest

import app.main as main_module
from app.auth import Role, can_edit_plugin, can_manage_admins, can_moderate_plugins
from app.config import load_settings
from app.main import create_app, create_store
from app.store import InMemoryMarketStore, PgRedisMarketStore, SCHEMA_SQL


def make_client(enable_dev_auth: bool = True) -> TestClient:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true" if enable_dev_auth else "false",
            "CORS_ORIGIN": "http://127.0.0.1:5173",
            "MARKET_API_KEYS": "local:test-key:market:read|market:write",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }
    )
    return TestClient(create_app(settings=settings, store=InMemoryMarketStore()))


def make_setup_client(tmp_path) -> TestClient:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "CORS_ORIGIN": "http://127.0.0.1:5173",
            "RUNTIME_CONFIG_FILE": str(tmp_path / "runtime.env"),
        }
    )
    return TestClient(create_app(settings=settings, store=InMemoryMarketStore()))


def test_first_user_becomes_core_admin() -> None:
    store = InMemoryMarketStore()
    first = store.upsert_github_user({"login": "alice", "name": "Alice"})
    second = store.upsert_github_user({"login": "bob", "name": "Bob"})

    assert first["role"] == Role.CORE_ADMIN
    assert second["role"] == Role.USER


def test_core_admin_can_manage_admins_while_normal_admin_moderates_plugins() -> None:
    core = {"role": Role.CORE_ADMIN}
    admin = {"role": Role.ADMIN}
    user = {"role": Role.USER}

    assert can_manage_admins(core) is True
    assert can_manage_admins(admin) is False
    assert can_moderate_plugins(admin) is True
    assert can_moderate_plugins(user) is False


def test_plugin_owners_can_edit_their_own_metadata() -> None:
    plugin = {"owner_user_id": "user_1", "owner_github_login": "alice"}

    assert can_edit_plugin({"id": "user_1", "github_login": "alice"}, plugin) is True
    assert can_edit_plugin({"id": "user_2", "github_login": "bob"}, plugin) is False


def test_submission_listing_comments_and_moderation_flow() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    assert login.status_code == 200
    assert login.json()["user"]["role"] == Role.CORE_ADMIN

    submission = client.post(
        "/v1/plugins/submissions",
        json={
            "name": "astrbot_plugin_demo",
            "display_name": "Demo",
            "desc": "Demo plugin",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_demo",
            "tags": ["demo"],
        },
    )
    assert submission.status_code == 201
    plugin = submission.json()
    assert plugin["status"] == "pending"
    assert client.get("/v1/plugins").json()["items"] == []

    listed = client.post(f"/v1/admin/plugins/{plugin['id']}/list")
    assert listed.status_code == 200
    assert client.get("/v1/plugins").json()["items"][0]["id"] == plugin["id"]

    comment = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Nice"})
    assert comment.status_code == 201
    assert comment.json()["body"] == "Nice"

    muted = client.post(
        f"/v1/admin/users/{login.json()['user']['id']}/mute",
        json={"muted_until": "2099-01-01T00:00:00Z"},
    )
    assert muted.status_code == 200
    assert muted.json()["muted_until"] == "2099-01-01T00:00:00Z"


def test_submission_requires_github_repo_owner() -> None:
    client = make_client()
    client.get("/v1/auth/debug-login?login=alice")

    response = client.post(
        "/v1/plugins/submissions",
        json={
            "name": "astrbot_plugin_demo",
            "desc": "Demo plugin",
            "author": "Alice",
            "repo": "https://github.com/bob/astrbot_plugin_demo",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"] == "GitHub account must own the repository"


def test_cors_allows_browser_session_cookies_and_dev_auth_header() -> None:
    client = make_client()
    response = client.options(
        "/v1/plugins",
        headers={
            "origin": "http://127.0.0.1:5173",
            "access-control-request-method": "GET",
            "access-control-request-headers": "x-dev-github-login",
        },
    )

    assert response.headers["access-control-allow-credentials"] == "true"
    assert "x-dev-github-login" in response.headers["access-control-allow-headers"].lower()


def test_first_run_setup_can_save_database_and_redis_urls(tmp_path) -> None:
    client = make_setup_client(tmp_path)

    status = client.get("/v1/setup/status")
    assert status.status_code == 200
    assert status.json()["required"] is True
    assert status.json()["missing"] == ["database_url", "redis_url"]
    assert status.json()["restart_required"] is False

    response = client.post(
        "/v1/setup",
        json={
            "database_url": "postgresql://market:market@127.0.0.1:5432/market",
            "redis_url": "redis://127.0.0.1:6379/0",
        },
    )

    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert "DATABASE_URL=postgresql://market:market@127.0.0.1:5432/market" in (
        tmp_path / "runtime.env"
    ).read_text()
    assert client.get("/health").json()["setup"] == "required"


def test_setup_after_first_run_requires_core_admin(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.get("/v1/auth/debug-login?login=alice")
    client.cookies.clear()

    client.post(
        "/v1/setup",
        json={
            "database_url": "postgresql://market:market@127.0.0.1:5432/market",
            "redis_url": "redis://127.0.0.1:6379/0",
        },
    )

    forbidden = client.post(
        "/v1/setup",
        headers={"x-dev-github-login": "bob"},
        json={
            "database_url": "postgresql://other:other@127.0.0.1:5432/other",
            "redis_url": "redis://127.0.0.1:6380/0",
        },
    )
    assert forbidden.status_code == 403

    allowed = client.post(
        "/v1/setup",
        headers={"x-dev-github-login": "alice"},
        json={
            "database_url": "postgresql://other:other@127.0.0.1:5432/other",
            "redis_url": "redis://127.0.0.1:6380/0",
        },
    )
    assert allowed.status_code == 200


def test_astrbot_plugin_source_matches_core_custom_registry_format() -> None:
    client = make_client()
    client.get("/v1/auth/debug-login?login=alice")
    submitted = client.post(
        "/v1/plugins/submissions",
        json={
            "name": "astrbot_plugin_demo",
            "display_name": "Demo",
            "desc": "Demo plugin",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_demo",
            "social_link": "https://github.com/alice",
            "tags": ["demo"],
        },
    ).json()
    client.post(f"/v1/admin/plugins/{submitted['id']}/list")

    response = client.get("/plugins.json")
    assert response.status_code == 200
    feed = response.json()
    assert list(feed) == ["astrbot_plugin_demo"]
    plugin = feed["astrbot_plugin_demo"]
    assert plugin["updated_at"]
    plugin_without_timestamp = {key: value for key, value in plugin.items() if key != "updated_at"}
    assert plugin_without_timestamp == {
        "name": "astrbot_plugin_demo",
        "display_name": "Demo",
        "desc": "Demo plugin",
        "short_desc": "Demo plugin",
        "author": "Alice",
        "repo": "https://github.com/alice/astrbot_plugin_demo",
        "social_link": "https://github.com/alice",
        "tags": ["demo"],
        "stars": 0,
        "version": "1.0.0",
        "logo": "",
        "pinned": False,
        "download_url": "",
        "i18n": {},
        "astrbot_version": "",
        "category": "",
        "support_platforms": [],
    }

    assert client.get("/plugins-md5.json").json()["md5"]
    assert client.get("/v1/astrbot/plugins.json").json() == feed


def test_market_web_fallback_does_not_mask_api_routes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "MARKET_WEB_DIST", tmp_path / "missing-dist")
    client = make_client()

    missing_api = client.get("/v1/does-not-exist")
    assert missing_api.status_code == 404
    assert missing_api.json()["error"] == "Not found"
    assert client.get("/v1").status_code == 404

    missing_web_build = client.get("/some-spa-route")
    assert missing_web_build.status_code == 404
    assert missing_web_build.json()["error"] == "Market web build is missing. Run npm run build:web first."


def test_market_web_serves_built_spa(tmp_path, monkeypatch) -> None:
    web_dist = tmp_path / "dist"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<html>market</html>", encoding="utf-8")
    (web_dist / "logo.webp").write_text("logo", encoding="utf-8")
    monkeypatch.setattr(main_module, "MARKET_WEB_DIST", web_dist)

    client = make_client()

    assert client.get("/").text == "<html>market</html>"
    assert client.get("/submit").text == "<html>market</html>"
    assert client.get("/logo.webp").text == "logo"


def test_store_selection_uses_pg_redis_only_when_both_urls_are_configured() -> None:
    memory_settings = load_settings({})
    assert isinstance(create_store(memory_settings), InMemoryMarketStore)

    production_settings = load_settings(
        {
            "DATABASE_URL": "postgresql://market:market@127.0.0.1:5432/market",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }
    )
    assert isinstance(create_store(production_settings), PgRedisMarketStore)


def test_postgres_schema_uses_constraints_jsonb_and_indexes() -> None:
    assert "CREATE TABLE IF NOT EXISTS market_users" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS market_plugins" in SCHEMA_SQL
    assert "jsonb NOT NULL DEFAULT '[]'::jsonb" in SCHEMA_SQL
    assert "CHECK (status IN ('pending', 'listed', 'unlisted'))" in SCHEMA_SQL
    assert "REFERENCES market_users(id)" in SCHEMA_SQL
    assert "USING GIN (tags)" in SCHEMA_SQL


def test_pg_redis_store_round_trip_from_env() -> None:
    database_url = os.getenv("ASTRBOT_TEST_DATABASE_URL", "")
    redis_url = os.getenv("ASTRBOT_TEST_REDIS_URL", "")
    if not database_url or not redis_url:
        pytest.skip("Set ASTRBOT_TEST_DATABASE_URL and ASTRBOT_TEST_REDIS_URL to run integration storage test")

    asyncio.run(run_pg_redis_store_round_trip(database_url, redis_url))


async def run_pg_redis_store_round_trip(database_url: str, redis_url: str) -> None:
    store = PgRedisMarketStore(database_url, redis_url, session_ttl_seconds=60)
    await store.connect()
    try:
        async with store._pool().acquire() as connection:
            await connection.execute(
                """
                TRUNCATE market_api_keys, market_comments, market_submissions,
                         market_plugins, market_announcements, market_users
                RESTART IDENTITY CASCADE
                """
            )

        alice = await store.upsert_github_user({"login": "alice", "name": "Alice"})
        assert alice["role"] == Role.CORE_ADMIN

        plugin = await store.submit_plugin(
            alice,
            {
                "name": "astrbot_plugin_demo",
                "display_name": "Demo",
                "desc": "Demo plugin",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_demo",
                "tags": ["demo"],
            },
        )
        assert plugin["status"] == "pending"
        assert await store.list_public_plugins() == []

        listed = await store.update_plugin_status(plugin["id"], "listed", alice["id"])
        assert listed and listed["status"] == "listed"

        comment = await store.add_comment(plugin["id"], alice["id"], "Nice")
        assert comment["body"] == "Nice"
        assert len(await store.list_comments(plugin["id"])) == 1

        session = await store.create_session(alice["id"])
        assert (await store.get_user_by_session(session["token"]))["github_login"] == "alice"
        assert await store.revoke_session(session["token"]) is True
        assert await store.get_user_by_session(session["token"]) is None
    finally:
        await store.close()
