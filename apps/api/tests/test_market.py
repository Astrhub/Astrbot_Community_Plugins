from __future__ import annotations

import asyncio
import os

from fastapi.testclient import TestClient
import pytest

import app.main as main_module
from app.auth import Role, can_edit_plugin, can_manage_admins, can_moderate_plugins
from app.config import load_settings
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
    return TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))


def make_setup_client(tmp_path) -> TestClient:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "CORS_ORIGIN": "http://127.0.0.1:5173",
            "RUNTIME_CONFIG_FILE": str(tmp_path / "runtime.env"),
        }
    )
    app = main_module.create_app(settings=settings, store=InMemoryMarketStore())
    app.state.setup_initializer_calls = []

    class FakeSetupStore(InMemoryMarketStore):
        pass

    async def fake_setup_initializer(
        payload,
        database_url: str,
        redis_url: str,
        core_admin_password_hash: str,
    ) -> FakeSetupStore:
        app.state.setup_initializer_calls.append(
            {
                "payload": payload,
                "database_url": database_url,
                "redis_url": redis_url,
                "core_admin_password_hash": core_admin_password_hash,
            }
        )
        store = FakeSetupStore()
        store.create_internal_admin(payload.admin.username, core_admin_password_hash)
        return store

    app.state.setup_initializer = fake_setup_initializer
    return TestClient(app)


def setup_payload(
    postgres_database: str = "market",
    postgres_port: int = 5432,
    redis_port: int = 6379,
    site_name: str = "AstrBot Community Plugins",
) -> dict[str, object]:
    return {
        "site": {"name": site_name, "icon_url": "/custom-logo.webp"},
        "admin": {"username": "admin", "password": "password123"},
        "postgres": {
            "host": "127.0.0.1",
            "port": postgres_port,
            "database": postgres_database,
            "username": "market",
            "password": "market",
            "ssl": False,
        },
        "redis": {
            "host": "127.0.0.1",
            "port": redis_port,
            "database": 0,
            "password": "",
            "ssl": False,
        },
    }


def plugin_payload(
    name: str = "astrbot_plugin_demo",
    repo: str = "https://github.com/alice/astrbot_plugin_demo",
    tags: list[str] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "display_name": "Demo",
        "desc": "Demo plugin",
        "author": "Alice",
        "repo": repo,
        "tags": tags or ["demo"],
    }


def system_settings_payload() -> dict[str, object]:
    return {
        "site": {
            "name": "AstrHub",
            "icon_url": "/hub.webp",
            "subtitle": "社区插件中心",
            "description": "发现和管理插件。",
            "contact_email": "ops@example.com",
            "docs_url": "https://docs.example.com/plugins",
        },
        "auth": {
            "github_login_enabled": True,
            "public_login_enabled": True,
            "login_agreement_enabled": True,
            "login_agreement_text": "登录条款",
            "service_terms_enabled": True,
            "service_terms_text": "服务条款",
        },
        "github": {
            "client_id": "client-id",
            "client_secret": "github-secret",
            "callback_url": "https://market.example.com/v1/auth/github/callback",
            "scope": "read:user user:email read:org",
            "admin_org": "Astrhub",
        },
        "market": {
            "submissions_enabled": True,
            "comments_enabled": True,
            "likes_enabled": True,
            "plugin_auto_approve_enabled": False,
            "max_plugin_tags": 4,
        },
        "email": {
            "provider": "cloudflare",
            "smtp": {
                "host": "",
                "port": 587,
                "username": "",
                "password": "",
                "from_address": "",
                "ssl": False,
            },
            "cloudflare": {
                "account_id": "cf-account",
                "api_token": "cf-token",
                "from_address": "noreply@example.com",
            },
            "daily_limit": 10,
            "verification_daily_limit_per_user": 3,
        },
    }


def test_github_users_do_not_become_core_admin_automatically() -> None:
    store = InMemoryMarketStore()
    first = store.upsert_github_user({"login": "alice", "name": "Alice"})
    second = store.upsert_github_user({"login": "bob", "name": "Bob"})

    assert first["role"] == Role.USER
    assert second["role"] == Role.USER


def test_internal_admin_is_core_admin() -> None:
    store = InMemoryMarketStore()
    admin = store.create_internal_admin("admin", "hash")

    assert admin["role"] == Role.CORE_ADMIN
    assert admin["internal_username"] == "admin"


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
    store = client.app.state.store
    admin = store.create_internal_admin("admin", "hash")
    login = client.get("/v1/auth/debug-login?login=alice")
    assert login.status_code == 200
    store.update_user_role(login.json()["user"]["id"], Role.ADMIN.value)

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
        f"/v1/admin/users/{admin['id']}/mute",
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


def test_market_feature_flags_close_submission_likes_and_comments() -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "MARKET_SUBMISSIONS_ENABLED": "false",
            "MARKET_COMMENTS_ENABLED": "false",
            "MARKET_LIKES_ENABLED": "false",
        }
    )
    store = InMemoryMarketStore()
    client = TestClient(main_module.create_app(settings=settings, store=store))
    login = client.get("/v1/auth/debug-login?login=alice")
    user = store.get_user_by_id(login.json()["user"]["id"])
    plugin = store.submit_plugin(user, plugin_payload())
    store.update_plugin_status(plugin["id"], "listed", user["id"])

    submission = client.post("/v1/plugins/submissions", json=plugin_payload())
    assert submission.status_code == 403
    assert submission.json()["error"] == "Plugin submissions are closed"
    assert client.post(f"/v1/plugins/{plugin['id']}/like").status_code == 403
    assert (
        client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Nice"}).status_code
        == 403
    )


def test_plugin_auto_approve_and_max_tags_are_enforced() -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "PLUGIN_AUTO_APPROVE_ENABLED": "true",
            "MAX_PLUGIN_TAGS": "1",
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))
    client.get("/v1/auth/debug-login?login=alice")

    too_many_tags = client.post(
        "/v1/plugins/submissions",
        json=plugin_payload(tags=["demo", "tool"]),
    )
    assert too_many_tags.status_code == 400
    assert too_many_tags.json()["error"] == "Plugin can have at most 1 tags"

    submission = client.post("/v1/plugins/submissions", json=plugin_payload(tags=["demo"]))
    assert submission.status_code == 201
    assert submission.json()["status"] == "listed"
    patch = client.patch(
        f"/v1/plugins/{submission.json()['id']}",
        json={"tags": ["demo", "tool"]},
    )
    assert patch.status_code == 400


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


def test_first_run_setup_can_save_structured_runtime_config(tmp_path) -> None:
    client = make_setup_client(tmp_path)

    status = client.get("/v1/setup/status")
    assert status.status_code == 200
    assert status.json()["required"] is True
    assert status.json()["missing"] == ["database_url", "redis_url"]
    assert status.json()["restart_required"] is False
    assert status.json()["saved_setup"]["postgres"]["host"] == "127.0.0.1"
    assert status.json()["saved_setup"]["postgres"]["password"] == ""
    assert status.json()["site"]["name"] == "AstrBot Community Plugins"

    response = client.post("/v1/setup", json=setup_payload(site_name="AstrHub Plugins"))

    assert response.status_code == 200
    assert response.json()["restart_required"] is False
    assert response.json()["activated"] is True
    assert len(client.app.state.setup_initializer_calls) == 1
    setup_call = client.app.state.setup_initializer_calls[0]
    assert (
        setup_call["database_url"]
        == "postgresql://market:market@127.0.0.1:5432/market?sslmode=disable"
    )
    assert setup_call["redis_url"] == "redis://127.0.0.1:6379/0"
    assert setup_call["core_admin_password_hash"].startswith("pbkdf2_sha256")
    runtime_file = (tmp_path / "runtime.env").read_text()
    assert (
        "DATABASE_URL=postgresql://market:market@127.0.0.1:5432/market?sslmode=disable"
        in runtime_file
    )
    assert "REDIS_URL=redis://127.0.0.1:6379/0" in runtime_file
    assert "POSTGRES_SSL=false" in runtime_file
    assert 'SITE_NAME="AstrHub Plugins"' in runtime_file
    assert "CORE_ADMIN_USERNAME=admin" in runtime_file
    assert "CORE_ADMIN_PASSWORD_HASH=pbkdf2_sha256" in runtime_file
    assert "SITE_SUBTITLE" not in runtime_file
    assert "GITHUB_LOGIN_ENABLED" not in runtime_file
    assert "MARKET_SUBMISSIONS_ENABLED" not in runtime_file
    assert "EMAIL_PROVIDER" not in runtime_file
    login = client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == Role.CORE_ADMIN
    assert client.get("/health").json()["setup"] == "complete"
    assert client.get("/v1/setup/status").json()["required"] is False


def test_setup_initialization_failure_does_not_write_runtime_config(tmp_path) -> None:
    client = make_setup_client(tmp_path)

    async def failing_setup_initializer(*_args) -> None:
        raise main_module.error(400, "PostgreSQL connection failed: refused")

    client.app.state.setup_initializer = failing_setup_initializer
    response = client.post("/v1/setup", json=setup_payload())

    assert response.status_code == 400
    assert response.json()["error"] == "PostgreSQL connection failed: refused"
    assert not (tmp_path / "runtime.env").exists()


def test_setup_initializer_creates_database_schema_and_internal_admin(monkeypatch) -> None:
    calls: list[object] = []

    async def fake_ensure_postgres_database(config: dict[str, object]) -> None:
        calls.append(("ensure_database", config["database"]))

    class FakePgRedisMarketStore:
        def __init__(self, database_url: str, redis_url: str, session_ttl_seconds: int) -> None:
            calls.append(("store", database_url, redis_url, session_ttl_seconds))

        async def connect(self) -> None:
            calls.append("connect")

        async def create_internal_admin(self, username: str, password_hash: str) -> None:
            calls.append(("admin", username, password_hash))

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(main_module, "ensure_postgres_database", fake_ensure_postgres_database)
    monkeypatch.setattr(main_module, "PgRedisMarketStore", FakePgRedisMarketStore)

    payload = main_module.SetupConfig.model_validate(setup_payload())
    store = asyncio.run(
        main_module.initialize_setup_infrastructure(
            payload,
            "postgresql://market:market@127.0.0.1:5432/market?sslmode=disable",
            "redis://127.0.0.1:6379/0",
            "hash",
        )
    )

    assert isinstance(store, FakePgRedisMarketStore)
    assert calls == [
        ("ensure_database", "market"),
        (
            "store",
            "postgresql://market:market@127.0.0.1:5432/market?sslmode=disable",
            "redis://127.0.0.1:6379/0",
            60,
        ),
        "connect",
        ("admin", "admin", "hash"),
    ]


def test_setup_activation_switches_store_without_process_restart(tmp_path) -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "RUNTIME_CONFIG_FILE": str(tmp_path / "runtime.env"),
        }
    )

    class ClosableMemoryStore(InMemoryMarketStore):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    old_store = ClosableMemoryStore()
    new_store = InMemoryMarketStore()
    app = main_module.create_app(settings=settings, store=old_store)

    async def fake_setup_initializer(payload, _database_url, _redis_url, password_hash):
        new_store.create_internal_admin(payload.admin.username, password_hash)
        return new_store

    app.state.setup_initializer = fake_setup_initializer
    client = TestClient(app)

    response = client.post("/v1/setup", json=setup_payload())

    assert response.status_code == 200
    assert response.json()["activated"] is True
    assert app.state.store is new_store
    assert old_store.closed is True
    assert client.get("/health").json()["setup"] == "complete"


def test_setup_after_first_run_requires_core_admin(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())
    client.cookies.clear()

    forbidden = client.post(
        "/v1/setup",
        headers={"x-dev-github-login": "bob"},
        json=setup_payload(postgres_database="other", redis_port=6380),
    )
    assert forbidden.status_code == 403

    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    allowed = client.post(
        "/v1/setup",
        json=setup_payload(postgres_database="other", redis_port=6380),
    )
    assert allowed.status_code == 200


def test_public_site_config_uses_settings() -> None:
    settings = load_settings(
        {
            "SITE_NAME": "AstrHub",
            "SITE_ICON_URL": "https://example.com/icon.webp",
            "SITE_SUBTITLE": "社区插件中心",
            "SITE_DESCRIPTION": "浏览 AstrBot 插件。",
            "SITE_CONTACT_EMAIL": "ops@example.com",
            "SITE_DOCS_URL": "https://docs.example.com",
            "MARKET_SUBMISSIONS_ENABLED": "false",
            "MARKET_COMMENTS_ENABLED": "false",
            "MARKET_LIKES_ENABLED": "false",
            "MAX_PLUGIN_TAGS": "3",
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))

    assert client.get("/v1/site").json() == {
        "name": "AstrHub",
        "icon_url": "https://example.com/icon.webp",
        "subtitle": "社区插件中心",
        "description": "浏览 AstrBot 插件。",
        "contact_email": "ops@example.com",
        "docs_url": "https://docs.example.com",
        "auth": {
            "github_login_enabled": False,
            "public_login_enabled": True,
            "login_agreement_enabled": False,
            "login_agreement_text": "",
            "service_terms_enabled": False,
            "service_terms_text": "",
            "terms_revision": main_module.digest_terms(settings),
        },
        "market": {
            "submissions_enabled": False,
            "comments_enabled": False,
            "likes_enabled": False,
            "max_plugin_tags": 3,
        },
    }


def test_setup_status_redacts_infrastructure_after_initial_setup(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())
    client.cookies.clear()

    public_status = client.get("/v1/setup/status").json()
    assert public_status["saved_setup"]["postgres"]["database"] == ""
    assert public_status["saved_setup"]["postgres"]["password"] == ""

    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    core_status = client.get("/v1/setup/status").json()
    assert core_status["saved_setup"]["postgres"]["password"] == "market"
    assert core_status["saved_setup"]["github"]["client_secret"] == ""
    assert core_status["saved_setup"]["email"]["cloudflare"]["api_token"] == ""


def test_core_admin_can_update_system_settings_and_preserve_masked_secrets(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())

    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    payload = system_settings_payload()
    saved = client.put("/v1/admin/settings", json=payload)
    assert saved.status_code == 200
    settings = saved.json()["settings"]
    assert settings["site"]["name"] == "AstrHub"
    assert settings["github"]["client_secret"] == main_module.MASKED_SECRET
    assert settings["github"]["client_secret_configured"] is True
    assert settings["email"]["cloudflare"]["api_token"] == main_module.MASKED_SECRET
    assert settings["email"]["cloudflare"]["api_token_configured"] is True
    assert client.get("/v1/site").json()["market"]["max_plugin_tags"] == 4

    masked_payload = system_settings_payload()
    masked_payload["github"]["client_secret"] = main_module.MASKED_SECRET
    masked_payload["email"]["cloudflare"]["api_token"] = main_module.MASKED_SECRET
    masked_payload["site"]["name"] = "AstrHub Updated"
    preserved = client.put("/v1/admin/settings", json=masked_payload)
    assert preserved.status_code == 200
    runtime_file = (tmp_path / "runtime.env").read_text()
    assert "GITHUB_CLIENT_SECRET=github-secret" in runtime_file
    assert "CLOUDFLARE_EMAIL_API_TOKEN=cf-token" in runtime_file
    assert 'SITE_NAME="AstrHub Updated"' in runtime_file


def test_system_settings_require_core_admin(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())
    client.cookies.clear()

    forbidden = client.get("/v1/admin/settings", headers={"x-dev-github-login": "bob"})
    assert forbidden.status_code == 403


def test_astrbot_plugin_source_matches_core_custom_registry_format() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    client.app.state.store.update_user_role(login.json()["user"]["id"], Role.ADMIN.value)
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


def test_cloudflare_email_test_uses_official_sending_endpoint(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    class FakeCloudflareResponse:
        status_code = 200
        content = b"{}"

        def json(self) -> dict[str, object]:
            return {"success": True, "result": {"permanent_bounces": []}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, url: str, **kwargs) -> FakeCloudflareResponse:
            requests.append({"url": url, **kwargs})
            return FakeCloudflareResponse()

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "EMAIL_PROVIDER": "cloudflare",
            "CLOUDFLARE_EMAIL_ACCOUNT_ID": "account",
            "CLOUDFLARE_EMAIL_API_TOKEN": "token",
            "CLOUDFLARE_EMAIL_FROM": "noreply@example.com",
        }
    )
    store = InMemoryMarketStore()
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    client = TestClient(main_module.create_app(settings=settings, store=store))
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )

    response = client.post(
        "/v1/admin/settings/email/test",
        json={"to": "user@example.com", "subject": "Test", "body": "Hello"},
    )
    assert response.status_code == 200
    assert response.json() == {"sent": True}
    assert requests[0]["url"] == (
        "https://api.cloudflare.com/client/v4/accounts/account/email/sending/send"
    )
    assert requests[0]["headers"]["authorization"] == "Bearer token"
    assert requests[0]["json"] == {
        "to": "user@example.com",
        "from": "noreply@example.com",
        "subject": "Test",
        "text": "Hello",
        "html": "Hello",
    }


def test_cloudflare_email_errors_are_reported(monkeypatch) -> None:
    class FakeCloudflareResponse:
        status_code = 400
        content = b"{}"

        def json(self) -> dict[str, object]:
            return {"success": False, "errors": [{"code": 1000, "message": "bad sender"}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, url: str, **kwargs) -> FakeCloudflareResponse:
            return FakeCloudflareResponse()

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "EMAIL_PROVIDER": "cloudflare",
            "CLOUDFLARE_EMAIL_ACCOUNT_ID": "account",
            "CLOUDFLARE_EMAIL_API_TOKEN": "token",
            "CLOUDFLARE_EMAIL_FROM": "noreply@example.com",
        }
    )
    store = InMemoryMarketStore()
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    client = TestClient(main_module.create_app(settings=settings, store=store))
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )

    response = client.post(
        "/v1/admin/settings/email/test",
        json={"to": "user@example.com", "subject": "Test", "body": "Hello"},
    )
    assert response.status_code == 502
    assert response.json()["error"] == "Cloudflare email API error: [1000] bad sender"


def test_market_web_fallback_does_not_mask_api_routes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "MARKET_WEB_DIST", tmp_path / "missing-dist")
    client = make_client()

    missing_api = client.get("/v1/does-not-exist")
    assert missing_api.status_code == 404
    assert missing_api.json()["error"] == "Not found"
    assert client.get("/v1").status_code == 404

    missing_web_build = client.get("/some-spa-route")
    assert missing_web_build.status_code == 404
    assert (
        missing_web_build.json()["error"]
        == "Market web build is missing. Run npm run build:web first."
    )


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
    assert isinstance(main_module.create_store(memory_settings), InMemoryMarketStore)

    production_settings = load_settings(
        {
            "DATABASE_URL": "postgresql://market:market@127.0.0.1:5432/market",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }
    )
    assert isinstance(main_module.create_store(production_settings), PgRedisMarketStore)


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
        pytest.skip(
            "Set ASTRBOT_TEST_DATABASE_URL and ASTRBOT_TEST_REDIS_URL to run integration storage test"
        )

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

        admin = await store.create_internal_admin("admin", "hash")
        alice = await store.upsert_github_user({"login": "alice", "name": "Alice"})
        assert admin["role"] == Role.CORE_ADMIN
        assert alice["role"] == Role.USER
        await store.update_user_role(alice["id"], Role.ADMIN.value)

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
