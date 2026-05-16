from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import Role, can_edit_plugin, can_manage_admins, can_moderate_plugins
from app.config import load_settings
from app.main import create_app
from app.store import InMemoryMarketStore


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
