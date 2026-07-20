from __future__ import annotations

import asyncio
import base64
import os
from types import SimpleNamespace

from fastapi import HTTPException
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


def enable_test_email(client: TestClient) -> None:
    client.app.state.settings = client.app.state.settings.with_updates(
        email_provider="smtp",
        smtp_host="smtp.example.com",
        smtp_from="noreply@example.com",
    )


def make_store_request(store: InMemoryMarketStore) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store)))


def make_setup_client(tmp_path) -> TestClient:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "CORS_ORIGIN": "http://127.0.0.1:5173",
            "APP_ENV_FILE": str(tmp_path / ".env"),
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
    site_name: str = "Astrhub 插件市场",
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
            "web_url": "https://market.example.com",
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
            "api_token": "system-github-token",
            "api_token_remove_indexes": [],
            "metadata_sync_enabled": True,
            "metadata_sync_interval_seconds": 1800,
        },
        "email": {
            "provider": "cloudflare",
            "smtp": {
                "host": "",
                "port": 587,
                "username": "",
                "password": "",
                "from_address": "",
                "from_name": "Astrhub Plugins Market",
                "ssl": False,
                "encryption": "auto",
                "auth_method": "auto",
                "validate_certs": True,
            },
            "cloudflare": {
                "account_id": "cf-account",
                "api_token": "cf-token",
                "from_address": "noreply@example.com",
                "from_name": "AstrHub Notice",
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


def test_user_can_update_own_profile() -> None:
    client = make_client()
    client.get("/v1/auth/debug-login?login=alice")

    response = client.patch(
        "/v1/me/profile",
        json={"github_name": "Alice Dev", "avatar_url": "https://example.com/avatar.webp"},
    )

    assert response.status_code == 200
    assert response.json()["github_name"] == "Alice Dev"
    assert response.json()["avatar_url"] == "https://example.com/avatar.webp"


def test_admin_can_store_github_token_without_public_echo() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    client.app.state.store.update_user_role(login.json()["user"]["id"], Role.ADMIN.value)

    response = client.patch("/v1/me/profile", json={"github_token": "github_pat_readonly"})
    me = client.get("/v1/me").json()

    assert response.status_code == 200
    assert response.json()["has_github_token"] is True
    assert "github_token" not in response.json()
    assert me["has_github_token"] is True
    assert "github_token" not in me
    assert (
        client.app.state.store.get_user_by_id(login.json()["user"]["id"])["github_token"]
        == "github_pat_readonly"
    )


def test_normal_user_can_store_github_token_and_refresh_interval() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")

    response = client.patch(
        "/v1/me/profile",
        json={"github_token": "github_pat_readonly", "github_refresh_interval_seconds": 300},
    )

    assert response.status_code == 200
    assert response.json()["has_github_token"] is True
    assert response.json()["github_refresh_interval_seconds"] == 300
    assert "github_token" not in response.json()
    stored = client.app.state.store.get_user_by_id(login.json()["user"]["id"])
    assert stored["github_token"] == "github_pat_readonly"
    assert stored["github_refresh_interval_seconds"] == 300


def test_user_notification_preferences_default_on_and_update() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")

    assert login.json()["user"]["notification_email"] == ""
    assert login.json()["user"]["github_email"] == ""
    assert login.json()["user"]["notify_plugin_review"] is True
    assert login.json()["user"]["notify_comments"] is True
    assert login.json()["user"]["notify_replies"] is True
    assert login.json()["user"]["notify_likes"] is True
    assert login.json()["user"]["notify_unlist"] is True
    assert login.json()["user"]["email_notify_plugin_review"] is True
    assert login.json()["user"]["email_notify_pending_review"] is True
    assert login.json()["user"]["email_notify_comments"] is False
    assert login.json()["user"]["email_notify_replies"] is False
    assert login.json()["user"]["email_notify_likes"] is False
    assert login.json()["user"]["email_notify_unlist"] is True

    response = client.patch(
        "/v1/me/profile",
        json={
            "notification_email": "alice@example.com",
            "notify_plugin_review": False,
            "notify_comments": False,
            "notify_replies": False,
            "notify_likes": False,
            "notify_unlist": False,
            "email_notify_plugin_review": True,
            "email_notify_pending_review": False,
            "email_notify_comments": True,
            "email_notify_replies": True,
            "email_notify_likes": True,
            "email_notify_unlist": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["notification_email"] == "alice@example.com"
    assert response.json()["notify_plugin_review"] is False
    assert response.json()["notify_comments"] is False
    assert response.json()["notify_replies"] is False
    assert response.json()["notify_likes"] is False
    assert response.json()["notify_unlist"] is False
    assert response.json()["email_notify_plugin_review"] is True
    assert response.json()["email_notify_pending_review"] is False
    assert response.json()["email_notify_comments"] is True
    assert response.json()["email_notify_replies"] is True
    assert response.json()["email_notify_likes"] is True
    assert response.json()["email_notify_unlist"] is True
    stored = client.app.state.store.get_user_by_id(login.json()["user"]["id"])
    assert stored["notification_email"] == "alice@example.com"
    assert stored["notify_plugin_review"] is False
    assert stored["notify_comments"] is False
    assert stored["notify_replies"] is False
    assert stored["notify_likes"] is False
    assert stored["notify_unlist"] is False
    assert stored["email_notify_plugin_review"] is True
    assert stored["email_notify_pending_review"] is False
    assert stored["email_notify_comments"] is True
    assert stored["email_notify_replies"] is True
    assert stored["email_notify_likes"] is True
    assert stored["email_notify_unlist"] is True


def test_user_notification_email_must_be_valid() -> None:
    client = make_client()
    client.get("/v1/auth/debug-login?login=alice")

    response = client.patch("/v1/me/profile", json={"notification_email": "invalid"})

    assert response.status_code == 400
    assert response.json()["error"] == "Notification email is invalid"


def test_public_user_does_not_expose_notification_emails() -> None:
    user = InMemoryMarketStore().upsert_github_user(
        {
            "login": "alice",
            "name": "Alice",
            "github_email": "alice-oauth@example.com",
            "notification_email": "notify@example.com",
        }
    )

    public = main_module.public_user(user)

    assert "github_email" not in public
    assert "notification_email" not in public


def test_user_can_manage_personal_api_keys() -> None:
    client = make_client()
    client.get("/v1/auth/debug-login?login=alice")

    created = client.post(
        "/v1/me/api-keys",
        json={"name": "Plugin Sync", "scopes": ["market:read"]},
    )

    assert created.status_code == 201
    api_key = created.json()
    assert api_key["id"]
    assert api_key["name"] == "Plugin Sync"
    assert api_key["scopes"] == ["market:read"]
    assert api_key["key"].startswith("sk-ah-")

    listed = client.get("/v1/me/api-keys")
    assert listed.status_code == 200
    listed_key = listed.json()["items"][0]
    assert listed_key["id"] == api_key["id"]
    assert listed_key["name"] == "Plugin Sync"
    assert listed_key["created_at"]
    assert "key" not in listed_key

    authenticated = client.get(
        "/v1/api-keys",
        headers={"authorization": f"Bearer {api_key['key']}"},
    )
    assert authenticated.status_code == 200

    deleted = client.delete(f"/v1/me/api-keys/{api_key['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 1
    assert client.get("/v1/me/api-keys").json()["items"] == []

    expired = client.get(
        "/v1/api-keys",
        headers={"authorization": f"Bearer {api_key['key']}"},
    )
    assert expired.status_code == 401


def test_admin_api_key_issue_uses_access_key_prefix() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    client.app.state.store.update_user_role(login.json()["user"]["id"], Role.ADMIN.value)

    response = client.post(
        "/v1/api-keys",
        json={"name": "Admin Client", "scopes": ["market:read", "market:write"]},
    )

    assert response.status_code == 201
    api_key = response.json()
    assert api_key["key"].startswith("sk-ah-")
    assert api_key["scopes"] == ["market:read", "market:write"]


def test_admin_user_listing_redacts_github_tokens() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    store = client.app.state.store
    store.update_user_role(login.json()["user"]["id"], Role.ADMIN.value)
    client.patch("/v1/me/profile", json={"github_token": "github_pat_readonly"})

    response = client.get("/v1/admin/users")

    assert response.status_code == 200
    users = response.json()["items"]
    alice = next(user for user in users if user["github_login"] == "alice")
    assert alice["has_github_token"] is True
    assert "github_token" not in alice


def test_internal_admin_can_link_existing_github_identity() -> None:
    client = make_client()
    store = client.app.state.store
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )

    linked = asyncio.run(
        main_module.link_github_profile_to_user(
            make_store_request(store),
            store.get_user_by_internal_username("admin"),
            {
                "id": "123",
                "login": "admin-gh",
                "name": "Admin GH",
                "avatar_url": "https://example.com/admin.webp",
            },
        )
    )

    assert linked["role"] == Role.CORE_ADMIN
    assert linked["internal_username"] == "admin"
    assert linked["github_login"] == "admin-gh"


def test_linking_github_merges_plain_github_user_into_admin() -> None:
    client = make_client()
    store = client.app.state.store
    admin = store.create_internal_admin("admin", main_module.hash_password("password123"))
    github_user = store.upsert_github_user(
        {
            "id": "123",
            "login": "alice",
            "name": "Alice",
            "avatar_url": "https://example.com/alice.webp",
        }
    )
    plugin = store.submit_plugin(github_user, plugin_payload())

    linked = asyncio.run(
        main_module.link_github_profile_to_user(
            make_store_request(store),
            admin,
            {
                "id": "123",
                "login": "alice",
                "name": "Alice",
                "avatar_url": "https://example.com/alice.webp",
            },
        )
    )

    assert linked["id"] == admin["id"]
    assert linked["role"] == Role.CORE_ADMIN
    assert linked["github_login"] == "alice"
    assert store.get_user_by_id(github_user["id"]) is None
    assert store.get_plugin(plugin["id"])["owner_user_id"] == admin["id"]


def test_github_callback_binds_logged_in_internal_admin(monkeypatch) -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "CORS_ORIGIN": "http://127.0.0.1:5173",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "GITHUB_LOGIN_ENABLED": "true",
            "GITHUB_CLIENT_ID": "client-id",
            "GITHUB_CLIENT_SECRET": "client-secret",
            "GITHUB_CALLBACK_URL": "http://127.0.0.1:8787/v1/auth/github/callback",
        }
    )
    store = InMemoryMarketStore()
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    client = TestClient(main_module.create_app(settings=settings, store=store))
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )

    login_response = client.get("/v1/auth/github/login", follow_redirects=False)
    state = client.cookies.get(settings.oauth_state_cookie_name)

    async def fake_exchange_github_code(settings, code):
        return "github-token"

    async def fake_fetch_github_profile(access_token):
        return {
            "id": 123,
            "login": "admin-gh",
            "name": "Admin GH",
            "avatar_url": "https://example.com/admin.webp",
        }

    monkeypatch.setattr(main_module, "exchange_github_code", fake_exchange_github_code)
    monkeypatch.setattr(main_module, "fetch_github_profile", fake_fetch_github_profile)

    response = client.get(
        f"/v1/auth/github/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    me = client.get("/v1/me")

    assert login_response.status_code == 307
    assert response.status_code == 307
    assert me.json()["internal_username"] == "admin"
    assert me.json()["role"] == Role.CORE_ADMIN
    assert me.json()["github_login"] == "admin-gh"


def test_core_admin_internal_login_works_when_public_login_closed() -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "PUBLIC_LOGIN_ENABLED": "false",
        }
    )
    store = InMemoryMarketStore()
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    client = TestClient(main_module.create_app(settings=settings, store=store))

    response = client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["role"] == Role.CORE_ADMIN


def test_github_login_works_when_public_login_closed() -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "PUBLIC_LOGIN_ENABLED": "false",
            "GITHUB_LOGIN_ENABLED": "true",
            "GITHUB_CLIENT_ID": "client-id",
            "GITHUB_CLIENT_SECRET": "client-secret",
            "GITHUB_CALLBACK_URL": "http://127.0.0.1:8787/v1/auth/github/callback",
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))

    response = client.get("/v1/auth/github/login", follow_redirects=False)
    location = response.headers["location"]

    assert response.status_code == 307
    assert "https://github.com/login/oauth/authorize" in location
    assert "client_id=client-id" in location


def test_github_login_is_disabled_when_oauth_closed() -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "PUBLIC_LOGIN_ENABLED": "true",
            "GITHUB_LOGIN_ENABLED": "false",
            "GITHUB_CLIENT_ID": "client-id",
            "GITHUB_CLIENT_SECRET": "client-secret",
            "GITHUB_CALLBACK_URL": "https://market.example.com/v1/auth/github/callback",
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))

    response = client.get("/v1/auth/github/login", follow_redirects=False)

    assert response.status_code == 403


def test_github_login_uses_env_file_callback_url_over_initial_settings(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GITHUB_LOGIN_ENABLED=true",
                "GITHUB_CLIENT_ID=file-client",
                "GITHUB_CLIENT_SECRET=file-secret",
                "GITHUB_CALLBACK_URL=https://market.example.com/v1/auth/github/callback",
                "",
            ]
        )
    )
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "APP_ENV_FILE": str(env_file),
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))

    response = client.get("/v1/auth/github/login", follow_redirects=False)
    location = response.headers["location"]

    assert response.status_code == 307
    assert "client_id=file-client" in location
    assert (
        "redirect_uri=https%3A%2F%2Fmarket.example.com%2Fv1%2Fauth%2Fgithub%2Fcallback" in location
    )
    assert "127.0.0.1" not in location

    overridden = load_settings(
        {
            "APP_ENV_FILE": str(env_file),
            "GITHUB_LOGIN_ENABLED": "false",
        }
    )
    assert overridden.github_login_enabled is False


def test_github_login_uses_database_options_over_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GITHUB_LOGIN_ENABLED=false",
                "GITHUB_CLIENT_ID=file-client",
                "GITHUB_CLIENT_SECRET=file-secret",
                "GITHUB_CALLBACK_URL=http://127.0.0.1:8787/v1/auth/github/callback",
                "",
            ]
        )
    )
    store = InMemoryMarketStore(
        {
            "options": {
                "GITHUB_LOGIN_ENABLED": "true",
                "GITHUB_CLIENT_ID": "database-client",
                "GITHUB_CLIENT_SECRET": "database-secret",
                "GITHUB_CALLBACK_URL": "https://market.example.com/v1/auth/github/callback",
            }
        }
    )
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "APP_ENV_FILE": str(env_file),
            "GITHUB_LOGIN_ENABLED": "false",
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=store))

    response = client.get("/v1/auth/github/login", follow_redirects=False)
    location = response.headers["location"]

    assert response.status_code == 307
    assert "client_id=database-client" in location
    assert (
        "redirect_uri=https%3A%2F%2Fmarket.example.com%2Fv1%2Fauth%2Fgithub%2Fcallback" in location
    )
    assert "file-client" not in location
    assert "127.0.0.1" not in location


def test_github_login_ignores_forwarded_host_for_callback_url(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GITHUB_LOGIN_ENABLED=true",
                "GITHUB_CLIENT_ID=file-client",
                "GITHUB_CLIENT_SECRET=file-secret",
                "GITHUB_CALLBACK_URL=http://127.0.0.1:8787/v1/auth/github/callback",
                "",
            ]
        )
    )
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "APP_ENV_FILE": str(env_file),
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))

    response = client.get(
        "/v1/auth/github/login",
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "market.example.com",
        },
        follow_redirects=False,
    )
    location = response.headers["location"]

    assert response.status_code == 307
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8787%2Fv1%2Fauth%2Fgithub%2Fcallback" in location
    assert "market.example.com" not in location


def test_github_callback_ignores_forwarded_origin_for_loopback_settings(
    monkeypatch,
    tmp_path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GITHUB_LOGIN_ENABLED=true",
                "GITHUB_CLIENT_ID=file-client",
                "GITHUB_CLIENT_SECRET=file-secret",
                "GITHUB_CALLBACK_URL=http://127.0.0.1:8787/v1/auth/github/callback",
                "WEB_URL=http://127.0.0.1:8787",
                "",
            ]
        )
    )
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "APP_ENV_FILE": str(env_file),
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))
    headers = {
        "x-forwarded-proto": "https",
        "x-forwarded-host": "market.example.com",
    }
    client.get("/v1/auth/github/login", headers=headers, follow_redirects=False)
    state = client.cookies.get(settings.oauth_state_cookie_name)
    captured = {}

    async def fake_exchange_github_code(settings, code):
        captured["callback_url"] = settings.github_callback_url
        return "github-token"

    async def fake_fetch_github_profile(access_token):
        return {
            "id": 123,
            "login": "alice",
            "name": "Alice",
            "avatar_url": "https://example.com/alice.webp",
        }

    monkeypatch.setattr(main_module, "exchange_github_code", fake_exchange_github_code)
    monkeypatch.setattr(main_module, "fetch_github_profile", fake_fetch_github_profile)

    response = client.get(
        f"/v1/auth/github/callback?code=ok&state={state}",
        headers=headers,
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "http://127.0.0.1:8787"
    assert captured["callback_url"] == "http://127.0.0.1:8787/v1/auth/github/callback"


def test_public_site_config_uses_env_file_settings(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GITHUB_LOGIN_ENABLED=true",
                "LOGIN_AGREEMENT_ENABLED=true",
                "LOGIN_AGREEMENT_TEXT=File login agreement",
                "SERVICE_TERMS_ENABLED=true",
                "SERVICE_TERMS_TEXT=File service terms",
                "MARKET_SUBMISSIONS_ENABLED=false",
                "MAX_PLUGIN_TAGS=5",
                "",
            ]
        )
    )
    settings = load_settings(
        {
            "APP_ENV_FILE": str(env_file),
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))

    site_config = client.get("/v1/site").json()

    assert site_config["auth"]["github_login_enabled"] is True
    assert site_config["auth"]["login_agreement_enabled"] is True
    assert site_config["auth"]["login_agreement_text"] == "File login agreement"
    assert site_config["auth"]["service_terms_enabled"] is True
    assert site_config["auth"]["service_terms_text"] == "File service terms"
    assert site_config["auth"]["terms_revision"] == main_module.digest_terms(
        settings.with_updates(
            login_agreement_enabled=True,
            login_agreement_text="File login agreement",
            service_terms_enabled=True,
            service_terms_text="File service terms",
        )
    )
    assert site_config["market"]["submissions_enabled"] is False
    assert site_config["market"]["max_plugin_tags"] == 5

    overridden = load_settings(
        {
            "APP_ENV_FILE": str(env_file),
            "MARKET_SUBMISSIONS_ENABLED": "true",
            "MAX_PLUGIN_TAGS": "8",
        }
    )
    assert overridden.market_submissions_enabled is True
    assert overridden.max_plugin_tags == 8


def test_public_site_config_uses_database_options_over_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "SITE_NAME=File Site",
                "GITHUB_LOGIN_ENABLED=false",
                "MARKET_SUBMISSIONS_ENABLED=false",
                "",
            ]
        )
    )
    store = InMemoryMarketStore(
        {
            "options": {
                "SITE_NAME": "Database Site",
                "GITHUB_LOGIN_ENABLED": "true",
                "MARKET_SUBMISSIONS_ENABLED": "true",
            }
        }
    )
    settings = load_settings({"APP_ENV_FILE": str(env_file)})
    client = TestClient(main_module.create_app(settings=settings, store=store))

    site_config = client.get("/v1/site").json()

    assert site_config["name"] == "Database Site"
    assert site_config["auth"]["github_login_enabled"] is True
    assert site_config["market"]["submissions_enabled"] is True


def test_core_admin_can_manage_admins_while_normal_admin_moderates_plugins() -> None:
    core = {"role": Role.CORE_ADMIN}
    admin = {"role": Role.ADMIN}
    user = {"role": Role.USER}

    assert can_manage_admins(core) is True
    assert can_manage_admins(admin) is False
    assert can_moderate_plugins(admin) is True
    assert can_moderate_plugins(user) is False


def test_core_admin_can_create_internal_users_and_change_roles() -> None:
    client = make_client()
    store = client.app.state.store
    core = store.create_internal_admin("admin", main_module.hash_password("password123"))
    client.post("/v1/auth/internal/login", json={"username": "admin", "password": "password123"})

    created = client.post(
        "/v1/core/users",
        json={"username": "operator", "password": "password123", "role": "user"},
    )
    duplicate = client.post(
        "/v1/core/users",
        json={"username": "operator", "password": "password123", "role": "user"},
    )
    promoted = client.post(
        f"/v1/core/admins/{created.json()['id']}",
        json={"role": "admin"},
    )
    demote_core = client.post(f"/v1/core/admins/{core['id']}", json={"role": "user"})
    operator_login = client.post(
        "/v1/auth/internal/login",
        json={"username": "operator", "password": "password123"},
    )

    assert created.status_code == 201
    assert created.json()["internal_username"] == "operator"
    assert created.json()["role"] == Role.USER
    assert "password_hash" not in created.json()
    assert duplicate.status_code == 409
    assert promoted.status_code == 200
    assert promoted.json()["role"] == Role.ADMIN
    assert demote_core.status_code == 400
    assert operator_login.status_code == 200
    assert operator_login.json()["user"]["role"] == Role.ADMIN


def test_core_admin_can_delete_non_core_users_without_removing_plugins() -> None:
    client = make_client()
    store = client.app.state.store
    core = store.create_internal_admin("admin", main_module.hash_password("password123"))
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    plugin = store.submit_plugin(store.get_user_by_id(owner["id"]), plugin_payload())
    client.post("/v1/auth/internal/login", json={"username": "admin", "password": "password123"})

    delete_self = client.delete(f"/v1/core/users/{core['id']}")
    deleted = client.delete(f"/v1/core/users/{owner['id']}")
    deleted_again = client.delete(f"/v1/core/users/{owner['id']}")
    transferred_plugin = store.get_plugin(plugin["id"])

    assert delete_self.status_code == 400
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted_again.status_code == 404
    assert store.get_user_by_id(owner["id"]) is None
    assert transferred_plugin["owner_user_id"] == core["id"]
    assert transferred_plugin["owner_github_login"] == "alice"


def test_admin_can_mute_and_unmute_users() -> None:
    client = make_client()
    store = client.app.state.store
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    user_login = client.get("/v1/auth/debug-login?login=alice")
    user = user_login.json()["user"]
    client.post("/v1/auth/internal/login", json={"username": "admin", "password": "password123"})

    muted = client.post(
        f"/v1/admin/users/{user['id']}/mute",
        json={"muted_until": "2099-01-01T00:00:00Z", "reason": "spam"},
    )
    unmuted = client.post(f"/v1/admin/users/{user['id']}/unmute")

    assert muted.status_code == 200
    assert muted.json()["muted_until"] == "2099-01-01T00:00:00Z"
    assert muted.json()["muted_reason"] == "spam"
    assert unmuted.status_code == 200
    assert unmuted.json()["muted_until"] is None
    assert unmuted.json()["muted_reason"] == ""


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
    assert client.get("/v1/plugins/submissions").json()["items"][0]["plugin_id"] == plugin["id"]

    listed = client.post(f"/v1/admin/plugins/{plugin['id']}/list")
    assert listed.status_code == 200
    assert client.get("/v1/plugins/submissions").json()["items"] == []
    assert client.get("/v1/plugins").json()["items"][0]["id"] == plugin["id"]

    comment = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Nice"})
    assert comment.status_code == 201
    assert comment.json()["body"] == "Nice"

    muted = client.post(
        f"/v1/admin/users/{admin['id']}/mute",
        json={"muted_until": "2099-01-01T00:00:00Z", "reason": "review"},
    )
    assert muted.status_code == 200
    assert muted.json()["muted_until"] == "2099-01-01T00:00:00Z"
    assert muted.json()["muted_reason"] == "review"


def test_plugin_detail_returns_nested_comments_with_user_profile() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    user_id = login.json()["user"]["id"]
    client.patch("/v1/me/profile", json={"github_name": "Alice Dev"})
    plugin = client.app.state.store.submit_plugin(
        client.app.state.store.get_user_by_id(user_id),
        plugin_payload(),
    )
    client.app.state.store.update_plugin_status(plugin["id"], "listed", user_id)

    root = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Nice"})
    reply = client.post(
        f"/v1/plugins/{plugin['id']}/comments",
        json={"body": "Agree", "parent_id": root.json()["id"]},
    )
    detail = client.get(f"/v1/plugins/{plugin['id']}").json()

    assert root.status_code == 201
    assert reply.status_code == 201
    assert detail["comments_count"] == 2
    assert detail["comments"][0]["github_name"] == "Alice Dev"
    assert detail["comments"][0]["floor"] == 1
    assert detail["comments"][0]["is_plugin_author"] is True
    assert detail["comments"][0]["is_admin"] is False
    assert detail["comments"][1]["parent_id"] == root.json()["id"]
    assert detail["comments"][1]["floor"] == 2


def test_plugin_detail_marks_admin_author_comments() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    user_id = login.json()["user"]["id"]
    client.app.state.store.update_user_role(user_id, Role.ADMIN.value)
    plugin = client.app.state.store.submit_plugin(
        client.app.state.store.get_user_by_id(user_id),
        plugin_payload(),
    )
    client.app.state.store.update_plugin_status(plugin["id"], "listed", user_id)

    client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Maintainer note"})
    detail = client.get(f"/v1/plugins/{plugin['id']}").json()

    assert detail["comments"][0]["floor"] == 1
    assert detail["comments"][0]["is_admin"] is True
    assert detail["comments"][0]["is_plugin_author"] is True


def test_plugin_likes_are_unique_per_user() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    user_id = login.json()["user"]["id"]
    plugin = client.app.state.store.submit_plugin(
        client.app.state.store.get_user_by_id(user_id),
        plugin_payload(),
    )
    client.app.state.store.update_plugin_status(plugin["id"], "listed", user_id)

    first = client.post(f"/v1/plugins/{plugin['id']}/like")
    second = client.post(f"/v1/plugins/{plugin['id']}/like")
    detail = client.get(f"/v1/plugins/{plugin['id']}")
    unliked = client.post(f"/v1/plugins/{plugin['id']}/unlike")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["likes"] == 1
    assert second.json()["likes"] == 1
    assert second.json()["liked"] is True
    assert detail.json()["liked"] is True
    assert unliked.json()["likes"] == 0
    assert unliked.json()["liked"] is False


def test_comment_owner_and_admin_can_delete_comments() -> None:
    client = make_client()
    store = client.app.state.store
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    alice_login = client.get("/v1/auth/debug-login?login=alice")
    alice = alice_login.json()["user"]
    plugin = store.submit_plugin(store.get_user_by_id(alice["id"]), plugin_payload())
    store.update_plugin_status(plugin["id"], "listed", alice["id"])
    comment = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Nice"}).json()

    client.get("/v1/auth/debug-login?login=bob")
    forbidden = client.delete(f"/v1/comments/{comment['id']}")
    client.get("/v1/auth/debug-login?login=alice")
    deleted_by_owner = client.delete(f"/v1/comments/{comment['id']}")
    second = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Again"}).json()
    client.post("/v1/auth/internal/login", json={"username": "admin", "password": "password123"})
    deleted_by_admin = client.delete(f"/v1/comments/{second['id']}")

    assert forbidden.status_code == 403
    assert deleted_by_owner.status_code == 200
    assert deleted_by_owner.json()["deleted"] is True
    assert deleted_by_admin.status_code == 200
    assert client.get(f"/v1/plugins/{plugin['id']}").json()["comments"] == []


def test_deleting_root_comment_hides_replies() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    user_id = login.json()["user"]["id"]
    plugin = client.app.state.store.submit_plugin(
        client.app.state.store.get_user_by_id(user_id),
        plugin_payload(),
    )
    client.app.state.store.update_plugin_status(plugin["id"], "listed", user_id)
    root = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Nice"}).json()
    client.post(
        f"/v1/plugins/{plugin['id']}/comments",
        json={"body": "Agree", "parent_id": root["id"]},
    )

    deleted = client.delete(f"/v1/comments/{root['id']}")
    detail = client.get(f"/v1/plugins/{plugin['id']}")

    assert deleted.status_code == 200
    assert detail.json()["comments"] == []
    assert detail.json()["comments_count"] == 0


def test_comment_likes_are_unique_per_user() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    user_id = login.json()["user"]["id"]
    plugin = client.app.state.store.submit_plugin(
        client.app.state.store.get_user_by_id(user_id),
        plugin_payload(),
    )
    client.app.state.store.update_plugin_status(plugin["id"], "listed", user_id)
    comment = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Nice"}).json()

    first = client.post(f"/v1/comments/{comment['id']}/like")
    second = client.post(f"/v1/comments/{comment['id']}/like")
    detail = client.get(f"/v1/plugins/{plugin['id']}")
    unliked = client.post(f"/v1/comments/{comment['id']}/unlike")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["likes"] == 1
    assert second.json()["likes"] == 1
    assert detail.json()["comments"][0]["liked"] is True
    assert unliked.json()["likes"] == 0


def test_reply_and_like_actions_notify_recipients_once() -> None:
    client = make_client()
    store = client.app.state.store
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    plugin = store.submit_plugin(store.get_user_by_id(owner["id"]), plugin_payload())
    store.update_plugin_status(plugin["id"], "listed", owner["id"])
    root = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Nice"}).json()

    bob_login = client.get("/v1/auth/debug-login?login=bob")
    bob = bob_login.json()["user"]
    reply = client.post(
        f"/v1/plugins/{plugin['id']}/comments",
        json={"body": "Agree with this", "parent_id": root["id"]},
    )
    plugin_like = client.post(f"/v1/plugins/{plugin['id']}/like")
    duplicate_plugin_like = client.post(f"/v1/plugins/{plugin['id']}/like")
    comment_like = client.post(f"/v1/comments/{root['id']}/like")
    duplicate_comment_like = client.post(f"/v1/comments/{root['id']}/like")

    assert reply.status_code == 201
    assert plugin_like.status_code == 200
    assert duplicate_plugin_like.status_code == 200
    assert comment_like.status_code == 200
    assert duplicate_comment_like.status_code == 200

    client.get("/v1/auth/debug-login?login=alice")
    notifications = client.get("/v1/me/notifications").json()["items"]
    notification_types = [item["type"] for item in notifications]

    assert notification_types.count("comment_reply") == 1
    assert notification_types.count("plugin_like") == 1
    assert notification_types.count("comment_like") == 1
    assert all(item["metadata"]["actor_user_id"] == bob["id"] for item in notifications)


def test_top_level_comment_notifies_plugin_owner_by_email(monkeypatch) -> None:
    client = make_client()
    enable_test_email(client)
    sent: list[dict[str, str]] = []

    async def fake_send_email(app, settings, receiver, subject, content):
        sent.append({"receiver": receiver, "subject": subject, "content": content})

    monkeypatch.setattr(main_module, "send_email", fake_send_email)
    store = client.app.state.store
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    client.patch(
        "/v1/me/profile",
        json={
            "notification_email": "notify@example.com",
            "email_notify_comments": True,
        },
    )
    plugin = store.submit_plugin(store.get_user_by_id(owner["id"]), plugin_payload())
    store.update_plugin_status(plugin["id"], "listed", owner["id"])

    client.get("/v1/auth/debug-login?login=bob")
    comment = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Great plugin"})

    assert comment.status_code == 201
    assert sent[0]["receiver"] == "notify@example.com"
    assert sent[0]["subject"] == "Astrhub 插件市场 - 你的插件有新评论"
    assert "bob 评论了 Demo：Great plugin" in sent[0]["content"]
    assert "个人设置的通知偏好" in sent[0]["content"]
    client.get("/v1/auth/debug-login?login=alice")
    notifications = client.get("/v1/me/notifications").json()["items"]
    assert notifications[0]["type"] == "plugin_comment"


def test_notification_email_falls_back_to_github_email(monkeypatch) -> None:
    client = make_client()
    enable_test_email(client)
    sent: list[dict[str, str]] = []

    async def fake_send_email(app, settings, receiver, subject, content):
        sent.append({"receiver": receiver, "subject": subject, "content": content})

    monkeypatch.setattr(main_module, "send_email", fake_send_email)
    store = client.app.state.store
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    store.update_user_profile(
        owner["id"],
        {
            "github_email": "alice-oauth@example.com",
            "email_notify_likes": True,
        },
    )
    plugin = store.submit_plugin(store.get_user_by_id(owner["id"]), plugin_payload())
    store.update_plugin_status(plugin["id"], "listed", owner["id"])

    client.get("/v1/auth/debug-login?login=bob")
    like = client.post(f"/v1/plugins/{plugin['id']}/like")

    assert like.status_code == 200
    assert sent[0]["receiver"] == "alice-oauth@example.com"
    assert sent[0]["subject"] == "Astrhub 插件市场 - 你的插件收到了点赞"
    assert "bob 点赞了 Demo" in sent[0]["content"]


def test_notification_preferences_disable_reply_and_like_notifications() -> None:
    client = make_client()
    store = client.app.state.store
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    client.patch("/v1/me/profile", json={"notify_replies": False, "notify_likes": False})
    plugin = store.submit_plugin(store.get_user_by_id(owner["id"]), plugin_payload())
    store.update_plugin_status(plugin["id"], "listed", owner["id"])
    root = client.post(f"/v1/plugins/{plugin['id']}/comments", json={"body": "Nice"}).json()

    client.get("/v1/auth/debug-login?login=bob")
    client.post(
        f"/v1/plugins/{plugin['id']}/comments",
        json={"body": "Agree with this", "parent_id": root["id"]},
    )
    client.post(f"/v1/plugins/{plugin['id']}/like")
    client.post(f"/v1/comments/{root['id']}/like")

    client.get("/v1/auth/debug-login?login=alice")
    notifications = client.get("/v1/me/notifications")

    assert notifications.status_code == 200
    assert notifications.json()["items"] == []


def test_owner_can_manage_own_plugins_without_bypassing_review() -> None:
    client = make_client()
    store = client.app.state.store
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    plugin = store.submit_plugin(store.get_user_by_id(owner["id"]), plugin_payload())
    store.update_plugin_status(plugin["id"], "listed", owner["id"])

    mine = client.get("/v1/me/plugins")
    patched = client.patch(f"/v1/plugins/{plugin['id']}", json={"tags": ["demo", "tool"]})
    unlisted = client.post(f"/v1/plugins/{plugin['id']}/unlist", json={"reason": "维护中"})
    requested = client.post(f"/v1/plugins/{plugin['id']}/request-list")

    assert mine.status_code == 200
    assert mine.json()["items"][0]["id"] == plugin["id"]
    assert patched.status_code == 200
    assert patched.json()["tags"] == ["demo", "tool"]
    assert unlisted.status_code == 200
    assert unlisted.json()["status"] == "unlisted"
    assert unlisted.json()["unlist_reason"] == "维护中"
    assert requested.status_code == 200
    assert requested.json()["status"] == "pending"
    assert store.list_submissions()[0]["plugin_id"] == plugin["id"]


def test_core_admin_can_review_plugin_submissions() -> None:
    client = make_client()
    store = client.app.state.store
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    plugin = store.submit_plugin(owner, plugin_payload())
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )

    admin_plugins = client.get("/v1/admin/plugins")
    listed = client.post(f"/v1/admin/plugins/{plugin['id']}/list")

    assert admin_plugins.status_code == 200
    assert admin_plugins.json()["items"][0]["id"] == plugin["id"]
    assert listed.status_code == 200
    assert listed.json()["status"] == "listed"
    client.get("/v1/auth/debug-login?login=alice")
    notifications = client.get("/v1/me/notifications").json()["items"]
    assert notifications[0]["type"] == "plugin_listed"
    assert notifications[0]["metadata"]["plugin_id"] == plugin["id"]
    assert "已通过审核并上架" in notifications[0]["body"]


def test_review_and_unlist_notifications_can_send_email(monkeypatch) -> None:
    client = make_client()
    enable_test_email(client)
    sent: list[dict[str, str]] = []

    async def fake_send_email(app, settings, receiver, subject, content):
        sent.append({"receiver": receiver, "subject": subject, "content": content})

    monkeypatch.setattr(main_module, "send_email", fake_send_email)
    store = client.app.state.store
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    client.patch(
        "/v1/me/profile",
        json={
            "notification_email": "owner@example.com",
            "email_notify_plugin_review": True,
            "email_notify_unlist": True,
        },
    )
    plugin = store.submit_plugin(store.get_user_by_id(owner["id"]), plugin_payload())
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )

    listed = client.post(f"/v1/admin/plugins/{plugin['id']}/list")
    unlisted = client.post(
        f"/v1/admin/plugins/{plugin['id']}/unlist",
        json={"reason": "插件无法正常安装"},
    )

    assert listed.status_code == 200
    assert unlisted.status_code == 200
    assert [item["receiver"] for item in sent] == ["owner@example.com", "owner@example.com"]
    assert sent[0]["subject"] == "Astrhub 插件市场 - 插件审核通过"
    assert "Demo 已通过审核并上架" in sent[0]["content"]
    assert sent[1]["subject"] == "Astrhub 插件市场 - 插件已下架"
    assert "插件无法正常安装" in sent[1]["content"]


def test_pending_review_email_is_sent_to_one_opted_in_admin(monkeypatch) -> None:
    client = make_client()
    enable_test_email(client)
    sent: list[dict[str, str]] = []

    async def fake_send_email(app, settings, receiver, subject, content):
        sent.append({"receiver": receiver, "subject": subject, "content": content})

    monkeypatch.setattr(main_module, "send_email", fake_send_email)
    store = client.app.state.store
    core = store.create_internal_admin("admin", main_module.hash_password("password123"))
    admin = store.create_internal_user("reviewer", "hash", Role.ADMIN.value)
    store.create_internal_user("normal", "hash", Role.USER.value)
    client.get("/v1/auth/debug-login?login=alice")

    no_email = client.post(
        "/v1/plugins/submissions",
        json=plugin_payload(
            name="astrbot_plugin_pending_a",
            repo="https://github.com/alice/astrbot_plugin_pending_a",
        ),
    )
    assert no_email.status_code == 201
    assert sent == []

    store.update_user_profile(
        core["id"],
        {
            "notification_email": "core@example.com",
            "email_notify_pending_review": True,
        },
    )
    store.update_user_profile(
        admin["id"],
        {
            "notification_email": "admin@example.com",
            "email_notify_plugin_review": True,
            "email_notify_pending_review": False,
        },
    )

    pending = client.post(
        "/v1/plugins/submissions",
        json=plugin_payload(
            name="astrbot_plugin_pending_b",
            repo="https://github.com/alice/astrbot_plugin_pending_b",
        ),
    )

    assert pending.status_code == 201
    assert len(sent) == 1
    assert sent[0]["receiver"] == "core@example.com"
    assert sent[0]["subject"] == "Astrhub 插件市场 - 有新的插件待审查"
    assert "astrbot_plugin_pending_b" in sent[0]["content"]


def test_admin_unlist_requires_reason_and_notifies_owner() -> None:
    client = make_client()
    store = client.app.state.store
    store.create_internal_admin("admin", main_module.hash_password("password123"))
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    plugin = store.submit_plugin(owner, plugin_payload())
    store.update_plugin_status(plugin["id"], "listed", owner["id"])
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )

    missing_reason = client.post(f"/v1/admin/plugins/{plugin['id']}/unlist", json={"reason": ""})
    unlisted = client.post(
        f"/v1/admin/plugins/{plugin['id']}/unlist",
        json={"reason": "插件无法正常安装"},
    )

    assert missing_reason.status_code == 400
    assert unlisted.status_code == 200
    assert unlisted.json()["status"] == "unlisted"
    assert unlisted.json()["unlist_reason"] == "插件无法正常安装"
    client.get("/v1/auth/debug-login?login=alice")
    notifications = client.get("/v1/me/notifications").json()["items"]
    assert notifications[0]["type"] == "plugin_unlisted"
    assert notifications[0]["metadata"]["plugin_id"] == plugin["id"]
    assert "插件无法正常安装" in notifications[0]["body"]


def test_notification_unread_count_and_mark_read() -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    user_id = login.json()["user"]["id"]
    store = client.app.state.store
    store.create_notification(user_id, "第一条", "消息一")
    store.create_notification(user_id, "第二条", "消息二")

    unread = client.get("/v1/me/notifications/unread-count")
    notifications = client.get("/v1/me/notifications")
    marked = client.post("/v1/me/notifications/read")
    unread_after = client.get("/v1/me/notifications/unread-count")
    notifications_after = client.get("/v1/me/notifications")

    assert unread.status_code == 200
    assert unread.json()["count"] == 2
    assert [item["read"] for item in notifications.json()["items"]] == [False, False]
    assert marked.status_code == 200
    assert marked.json()["updated"] == 2
    assert unread_after.json()["count"] == 0
    assert [item["read"] for item in notifications_after.json()["items"]] == [True, True]


def test_notifications_support_pagination_and_delete_operations() -> None:
    client = make_client()
    alice_login = client.get("/v1/auth/debug-login?login=alice")
    alice_id = alice_login.json()["user"]["id"]
    store = client.app.state.store
    for index in range(25):
        store.create_notification(alice_id, f"消息 {index}", "内容")

    first_page = client.get("/v1/me/notifications?limit=10&offset=0")
    second_page = client.get("/v1/me/notifications?limit=10&offset=10")
    first_id = first_page.json()["items"][0]["id"]
    second_page_ids = [item["id"] for item in second_page.json()["items"][:3]]

    deleted_one = client.delete(f"/v1/me/notifications/{first_id}")
    deleted_many = client.post("/v1/me/notifications/delete", json={"ids": second_page_ids})
    after_delete = client.get("/v1/me/notifications?limit=100&offset=0")
    cleared = client.delete("/v1/me/notifications")
    after_clear = client.get("/v1/me/notifications")

    assert first_page.status_code == 200
    assert len(first_page.json()["items"]) == 10
    assert first_page.json()["total"] == 25
    assert first_page.json()["limit"] == 10
    assert first_page.json()["offset"] == 0
    assert len(second_page.json()["items"]) == 10
    assert deleted_one.status_code == 200
    assert deleted_one.json()["deleted"] == 1
    assert deleted_many.status_code == 200
    assert deleted_many.json()["deleted"] == 3
    assert after_delete.json()["total"] == 21
    assert cleared.status_code == 200
    assert cleared.json()["deleted"] == 21
    assert after_clear.json()["items"] == []
    assert after_clear.json()["total"] == 0


def test_notification_delete_is_scoped_to_current_user() -> None:
    client = make_client()
    alice_login = client.get("/v1/auth/debug-login?login=alice")
    alice_id = alice_login.json()["user"]["id"]
    store = client.app.state.store
    notification = store.create_notification(alice_id, "私有消息", "内容")

    client.get("/v1/auth/debug-login?login=bob")
    deleted = client.delete(f"/v1/me/notifications/{notification['id']}")
    bob_notifications = client.get("/v1/me/notifications")

    client.get("/v1/auth/debug-login?login=alice")
    alice_notifications = client.get("/v1/me/notifications")

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 0
    assert bob_notifications.json()["items"] == []
    assert alice_notifications.json()["items"][0]["id"] == notification["id"]


def test_listing_clears_previous_unlist_metadata() -> None:
    client = make_client()
    store = client.app.state.store
    admin = store.create_internal_admin("admin", main_module.hash_password("password123"))
    owner_login = client.get("/v1/auth/debug-login?login=alice")
    owner = owner_login.json()["user"]
    plugin = store.submit_plugin(owner, plugin_payload())
    store.unlist_plugin(plugin["id"], admin["id"], "临时下架")

    relisted = store.update_plugin_status(plugin["id"], "listed", admin["id"])

    assert relisted["status"] == "listed"
    assert "unlist_reason" not in relisted
    assert "unlisted_at" not in relisted
    assert "unlisted_by" not in relisted


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
    notifications = client.get("/v1/me/notifications").json()["items"]
    assert notifications[0]["type"] == "plugin_listed"
    assert notifications[0]["metadata"]["auto_approved"] is True
    assert "已自动审核通过并上架" in notifications[0]["body"]
    patch = client.patch(
        f"/v1/plugins/{submission.json()['id']}",
        json={"tags": ["demo", "tool"]},
    )
    assert patch.status_code == 400


def test_plugin_submission_accepts_only_official_categories() -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "GITHUB_METADATA_SYNC_ENABLED": "false",
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))
    client.get("/v1/auth/debug-login?login=alice")

    payload = plugin_payload()
    payload["category"] = "AI-Tools"
    accepted = client.post("/v1/plugins/submissions", json=payload)

    invalid_payload = plugin_payload(name="astrbot_plugin_bad")
    invalid_payload["category"] = "chatbot"
    rejected = client.post("/v1/plugins/submissions", json=invalid_payload)

    legacy_payload = plugin_payload(name="astrbot_plugin_legacy")
    legacy = client.post("/v1/plugins/submissions", json=legacy_payload)

    assert accepted.status_code == 201
    assert accepted.json()["category"] == "ai_tools"
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "Plugin category is invalid"
    assert legacy.status_code == 201
    assert legacy.json().get("category", "") == ""


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


def test_first_run_setup_can_save_structured_env_file(tmp_path) -> None:
    client = make_setup_client(tmp_path)

    status = client.get("/v1/setup/status")
    assert status.status_code == 200
    assert status.json()["required"] is True
    assert status.json()["missing"] == ["database_url", "redis_url"]
    assert status.json()["restart_required"] is False
    assert status.json()["saved_setup"]["postgres"]["host"] == "127.0.0.1"
    assert status.json()["saved_setup"]["postgres"]["password"] == ""
    assert status.json()["site"]["name"] == "Astrhub 插件市场"

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
    env_file = (tmp_path / ".env").read_text()
    assert (
        "DATABASE_URL=postgresql://market:market@127.0.0.1:5432/market?sslmode=disable" in env_file
    )
    assert "REDIS_URL=redis://127.0.0.1:6379/0" in env_file
    assert "POSTGRES_SSL=false" in env_file
    assert "CORE_ADMIN_USERNAME=admin" in env_file
    assert "CORE_ADMIN_PASSWORD_HASH=pbkdf2_sha256" in env_file
    assert "SITE_NAME" not in env_file
    assert "SITE_SUBTITLE" not in env_file
    assert "GITHUB_LOGIN_ENABLED" not in env_file
    assert "MARKET_SUBMISSIONS_ENABLED" not in env_file
    assert "EMAIL_PROVIDER" not in env_file
    assert client.app.state.store.list_options()["SITE_NAME"] == "AstrHub Plugins"
    login = client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == Role.CORE_ADMIN
    assert client.get("/health").json()["setup"] == "complete"
    assert client.get("/v1/setup/status").status_code == 404
    assert client.get("/v1/admin/setup/status").json()["required"] is False
    client.cookies.clear()
    repeat_setup = client.post("/v1/setup", json=setup_payload(site_name="Again"))
    assert repeat_setup.status_code == 404


def test_setup_initialization_failure_does_not_write_env_file(tmp_path) -> None:
    client = make_setup_client(tmp_path)

    async def failing_setup_initializer(*_args) -> None:
        raise main_module.error(400, "PostgreSQL connection failed: refused")

    client.app.state.setup_initializer = failing_setup_initializer
    response = client.post("/v1/setup", json=setup_payload())

    assert response.status_code == 400
    assert response.json()["error"] == "PostgreSQL connection failed: refused"
    assert not (tmp_path / ".env").exists()


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
            "APP_ENV_FILE": str(tmp_path / ".env"),
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


def test_setup_after_first_run_is_closed(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())
    client.cookies.clear()

    closed = client.post(
        "/v1/setup",
        headers={"x-dev-github-login": "bob"},
        json=setup_payload(postgres_database="other", redis_port=6380),
    )
    assert closed.status_code == 404

    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    still_closed = client.post(
        "/v1/setup",
        json=setup_payload(postgres_database="other", redis_port=6380),
    )
    assert still_closed.status_code == 404


def test_public_site_config_uses_settings() -> None:
    settings = load_settings(
        {
            "SITE_NAME": "AstrHub",
            "SITE_ICON_URL": "https://example.com/icon.webp",
            "WEB_URL": "https://plugins.example.com",
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
        "web_url": "https://plugins.example.com",
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


def test_setup_status_closes_after_initial_setup(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())
    client.cookies.clear()

    public_status = client.get("/v1/setup/status")
    assert public_status.status_code == 404

    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    core_status = client.get("/v1/admin/setup/status").json()
    assert core_status["saved_setup"]["postgres"]["password"] == "market"
    assert core_status["saved_setup"]["github"]["client_secret"] == ""
    assert core_status["saved_setup"]["email"]["cloudflare"]["api_token"] == ""


def test_core_admin_can_update_system_settings_and_preserve_masked_secrets(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())
    client.app.state.settings = client.app.state.settings.with_updates(
        github_client_secret="env-github-secret"
    )

    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    payload = system_settings_payload()
    payload["email"]["smtp"]["password"] = "smtp-secret"
    payload["email"]["smtp"]["encryption"] = "ssl_tls"
    payload["email"]["smtp"]["auth_method"] = "login"
    payload["email"]["smtp"]["validate_certs"] = False
    saved = client.put("/v1/admin/settings", json=payload)
    assert saved.status_code == 200
    settings = saved.json()["settings"]
    assert settings["site"]["name"] == "AstrHub"
    assert settings["github"]["client_secret"] == main_module.MASKED_SECRET
    assert settings["github"]["client_secret_configured"] is True
    assert "api_token" not in settings["github"]
    assert settings["market"]["api_token"] == main_module.MASKED_SECRET
    assert settings["market"]["api_token_configured"] is True
    assert settings["market"]["api_token_previews"] == ["s*****************n"]
    assert settings["market"]["metadata_sync_interval_seconds"] == 1800
    assert settings["email"]["smtp"]["password"] == main_module.MASKED_SECRET
    assert settings["email"]["smtp"]["password_configured"] is True
    assert settings["email"]["smtp"]["encryption"] == "ssl_tls"
    assert settings["email"]["smtp"]["auth_method"] == "login"
    assert settings["email"]["smtp"]["validate_certs"] is False
    assert settings["email"]["smtp"]["from_name"] == "Astrhub Plugins Market"
    assert settings["email"]["cloudflare"]["api_token"] == main_module.MASKED_SECRET
    assert settings["email"]["cloudflare"]["api_token_configured"] is True
    assert settings["email"]["cloudflare"]["from_name"] == "AstrHub Notice"
    assert client.get("/v1/site").json()["market"]["max_plugin_tags"] == 4
    stored_options = client.app.state.store.list_options()
    assert stored_options["SITE_NAME"] == "AstrHub"
    assert "GITHUB_CLIENT_SECRET" not in stored_options
    assert stored_options["GITHUB_API_TOKEN"] == "system-github-token"
    assert stored_options["SMTP_AUTH_METHOD"] == "login"
    assert stored_options["SMTP_ENCRYPTION"] == "ssl_tls"
    assert stored_options["SMTP_FROM_NAME"] == "Astrhub Plugins Market"
    assert stored_options["SMTP_PASSWORD"] == "smtp-secret"
    assert stored_options["SMTP_VALIDATE_CERTS"] == "false"
    assert stored_options["CLOUDFLARE_EMAIL_API_TOKEN"] == "cf-token"
    assert stored_options["CLOUDFLARE_EMAIL_FROM_NAME"] == "AstrHub Notice"
    assert client.app.state.settings.github_client_secret == "env-github-secret"
    assert client.app.state.settings.smtp_password == "smtp-secret"
    env_file_before = (tmp_path / ".env").read_text()

    masked_payload = system_settings_payload()
    masked_payload["github"]["client_secret"] = main_module.MASKED_SECRET
    masked_payload["market"]["api_token"] = main_module.MASKED_SECRET
    masked_payload["email"]["smtp"]["password"] = main_module.MASKED_SECRET
    masked_payload["email"]["cloudflare"]["api_token"] = main_module.MASKED_SECRET
    masked_payload["site"]["name"] = "AstrHub Updated"
    preserved = client.put("/v1/admin/settings", json=masked_payload)
    assert preserved.status_code == 200
    stored_options = client.app.state.store.list_options()
    assert "GITHUB_CLIENT_SECRET" not in stored_options
    assert stored_options["GITHUB_API_TOKEN"] == "system-github-token"
    assert stored_options["GITHUB_METADATA_SYNC_INTERVAL_SECONDS"] == "1800"
    assert stored_options["SMTP_AUTH_METHOD"] == "auto"
    assert stored_options["SMTP_ENCRYPTION"] == "auto"
    assert stored_options["SMTP_PASSWORD"] == "smtp-secret"
    assert stored_options["SMTP_VALIDATE_CERTS"] == "true"
    assert stored_options["CLOUDFLARE_EMAIL_API_TOKEN"] == "cf-token"
    assert stored_options["SITE_NAME"] == "AstrHub Updated"
    assert stored_options["WEB_URL"] == "https://market.example.com"
    assert (tmp_path / ".env").read_text() == env_file_before


def test_runtime_settings_ignore_database_github_client_secret() -> None:
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "GITHUB_CLIENT_SECRET": "env-github-secret",
        }
    )
    store = InMemoryMarketStore(
        {
            "options": {
                "GITHUB_CLIENT_SECRET": "database-github-secret",
            }
        }
    )
    app = main_module.create_app(settings=settings, store=store)

    effective = asyncio.run(main_module.runtime_settings_for_app(app))
    system_settings = main_module.build_system_settings(
        settings,
        store.list_options(),
        include_secrets=True,
    )

    assert effective.github_client_secret == "env-github-secret"
    assert system_settings["github"]["client_secret"] == "env-github-secret"


def test_core_admin_can_append_and_remove_system_github_api_tokens(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())
    client.app.state.settings = client.app.state.settings.with_updates(
        github_client_secret="env-github-secret"
    )
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    payload = system_settings_payload()
    payload["market"]["api_token"] = "token-a\ntoken-b"

    saved = client.put("/v1/admin/settings", json=payload)

    assert saved.status_code == 200
    assert client.app.state.store.list_options()["GITHUB_API_TOKEN"] == "token-a\ntoken-b"
    assert saved.json()["settings"]["market"]["api_token_previews"] == [
        "t*****a",
        "t*****b",
    ]

    updated_payload = system_settings_payload()
    updated_payload["github"]["client_secret"] = ""
    updated_payload["market"]["api_token"] = "token-c,token-b"
    updated_payload["market"]["api_token_remove_indexes"] = [0]

    updated = client.put("/v1/admin/settings", json=updated_payload)

    assert updated.status_code == 200
    assert client.app.state.store.list_options()["GITHUB_API_TOKEN"] == "token-b\ntoken-c"
    assert updated.json()["settings"]["market"]["api_token_previews"] == [
        "t*****b",
        "t*****c",
    ]


def test_system_github_api_tokens_are_rotated() -> None:
    settings = load_settings(
        {
            "GITHUB_API_TOKEN": "token-a\ntoken-b, token-c",
        }
    )
    app = main_module.create_app(settings=settings, store=InMemoryMarketStore())

    assert main_module.parse_github_api_tokens(settings.github_api_token) == [
        "token-a",
        "token-b",
        "token-c",
    ]
    assert main_module.github_api_headers(settings=settings)["authorization"] == "Bearer token-a"
    assert main_module.next_system_github_api_token(app, settings) == "token-a"
    assert main_module.next_system_github_api_token(app, settings) == "token-b"
    assert main_module.next_system_github_api_token(app, settings) == "token-c"
    assert main_module.next_system_github_api_token(app, settings) == "token-a"


def test_disabled_system_github_api_tokens_are_skipped() -> None:
    settings = load_settings(
        {
            "GITHUB_API_TOKEN": "token-a\ntoken-b",
        }
    )
    app = main_module.create_app(settings=settings, store=InMemoryMarketStore())
    statuses = {
        main_module.github_api_token_hash("token-a"): {
            "disabled": True,
            "status": "disabled",
            "error_code": 401,
        }
    }

    assert (
        main_module.github_api_headers(
            settings=settings,
            token_statuses=statuses,
        )["authorization"]
        == "Bearer token-b"
    )
    assert main_module.next_system_github_api_token(app, settings, statuses) == "token-b"
    assert main_module.next_system_github_api_token(app, settings, statuses) == "token-b"


def test_redact_token_previews_masks_each_token_individually() -> None:
    assert main_module.redact_token_previews("token-a\ntoken-b, token-c") == [
        "t*****a",
        "t*****b",
        "t*****c",
    ]


def test_smtp_settings_default_to_auto_encryption() -> None:
    default_settings = load_settings({"SMTP_PORT": "587"})
    ssl_flag_settings = load_settings({"SMTP_PORT": "587", "SMTP_SSL": "true"})
    ssl_port_settings = load_settings({"SMTP_PORT": "465"})
    explicit_tls_settings = load_settings({"SMTP_PORT": "465", "SMTP_ENCRYPTION": "ssl_tls"})
    explicit_plain_settings = load_settings(
        {"SMTP_PORT": "465", "SMTP_ENCRYPTION": "none", "SMTP_AUTH_METHOD": "LOGIN"}
    )

    assert default_settings.smtp_encryption == "auto"
    assert default_settings.smtp_validate_certs is True
    assert ssl_flag_settings.smtp_encryption == "auto"
    assert ssl_flag_settings.smtp_ssl is False
    assert ssl_port_settings.smtp_encryption == "auto"
    assert explicit_tls_settings.smtp_encryption == "ssl_tls"
    assert explicit_tls_settings.smtp_ssl is True
    assert explicit_plain_settings.smtp_encryption == "none"
    assert explicit_plain_settings.smtp_auth_method == "login"


def test_system_settings_reject_local_oauth_callback_when_enabled(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    payload = system_settings_payload()
    payload["github"]["callback_url"] = "http://127.0.0.1:8787/v1/auth/github/callback"

    response = client.put("/v1/admin/settings", json=payload)

    assert response.status_code == 400
    assert response.json()["error"] == "GitHub callback URL must use a public host"


def test_system_settings_reject_local_web_url_when_oauth_enabled(tmp_path) -> None:
    client = make_setup_client(tmp_path)
    client.post("/v1/setup", json=setup_payload())
    client.post(
        "/v1/auth/internal/login",
        json={"username": "admin", "password": "password123"},
    )
    payload = system_settings_payload()
    payload["site"]["web_url"] = "http://127.0.0.1:8787"

    response = client.put("/v1/admin/settings", json=payload)

    assert response.status_code == 400
    assert response.json()["error"] == "Web URL must use a public host when GitHub login is enabled"


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


def test_submission_enriches_plugin_metadata_from_github(monkeypatch) -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    client.app.state.store.update_user_role(login.json()["user"]["id"], Role.ADMIN.value)
    client.patch("/v1/me/profile", json={"github_token": "github_pat_readonly"})
    seen_authorizations = []
    metadata_text = "\n".join(
        [
            "name: astrbot_plugin_demo",
            "display_name: Repo Demo",
            "desc: Repo metadata description",
            "author: Repo Author",
            "version: v2.1.0",
            "tags: [repo, metadata]",
            "astrbot_version: '>=4.5.0'",
            "support_platforms:",
            "  - aiocqhttp",
            "  - telegram",
        ]
    )

    class FakeResponse:
        def __init__(self, status_code: int, data: dict[str, object]) -> None:
            self.status_code = status_code
            self._data = data
            self.headers = {}

        def json(self) -> dict[str, object]:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            seen_authorizations.append((kwargs.get("headers") or {}).get("authorization"))
            if url == "https://api.github.com/repos/alice/astrbot_plugin_demo":
                return FakeResponse(
                    200,
                    {
                        "stargazers_count": 42,
                        "updated_at": "2026-05-17T00:00:00Z",
                        "default_branch": "main",
                    },
                )
            if url.endswith("/contents/metadata.yml"):
                return FakeResponse(
                    200,
                    {"content": base64.b64encode(metadata_text.encode()).decode()},
                )
            if url.endswith("/contents/logo.png"):
                return FakeResponse(200, {"name": "logo.png"})
            return FakeResponse(404, {})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)

    submission = client.post("/v1/plugins/submissions", json=plugin_payload())
    client.post(f"/v1/admin/plugins/{submission.json()['id']}/list")
    public_plugin = client.get("/v1/plugins").json()["items"][0]
    source_plugin = client.get("/plugins.json").json()["astrbot_plugin_demo"]

    assert public_plugin["stars"] == 42
    assert public_plugin["display_name"] == "Repo Demo"
    assert public_plugin["desc"] == "Repo metadata description"
    assert public_plugin["author"] == "Repo Author"
    assert public_plugin["tags"] == ["repo", "metadata"]
    assert public_plugin["version"] == "v2.1.0"
    assert public_plugin["logo"] == (
        "https://raw.githubusercontent.com/alice/astrbot_plugin_demo/main/logo.png"
    )
    assert source_plugin["stars"] == 42
    assert source_plugin["display_name"] == "Repo Demo"
    assert source_plugin["desc"] == "Repo metadata description"
    assert source_plugin["author"] == "Repo Author"
    assert source_plugin["tags"] == ["repo", "metadata"]
    assert source_plugin["version"] == "v2.1.0"
    assert source_plugin["astrbot_version"] == ">=4.5.0"
    assert source_plugin["support_platforms"] == ["aiocqhttp", "telegram"]
    assert "Bearer github_pat_readonly" in seen_authorizations


def test_submission_metadata_preview_prefills_from_github(monkeypatch) -> None:
    client = make_client()
    client.get("/v1/auth/debug-login?login=alice")
    metadata_text = "\n".join(
        [
            "name: astrbot_plugin_demo",
            "desc: Preview metadata description",
            "display_name: Preview Demo",
            "version: v2.2.0",
            "author: Preview Author",
            "repo: https://github.com/alice/astrbot_plugin_demo",
            'astrbot_version: ">=4.10.4"',
            "social_link: https://example.com/preview",
            "category: productivity",
            "tags:",
            "  - preview",
            "  - metadata",
            "support_platforms:",
            "  - aiocqhttp",
        ]
    )
    metadata_filenames: list[str] = []

    class FakeResponse:
        def __init__(self, status_code: int, data: dict[str, object]) -> None:
            self.status_code = status_code
            self._data = data
            self.headers = {}

        def json(self) -> dict[str, object]:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            if url == "https://api.github.com/repos/alice/astrbot_plugin_demo":
                return FakeResponse(
                    200,
                    {
                        "name": "astrbot_plugin_demo",
                        "description": "Repository description",
                        "homepage": "https://example.com/home",
                        "topics": ["repo-topic"],
                        "owner": {"login": "alice"},
                    },
                )
            if "/contents/metadata." in url:
                metadata_filenames.append(url.rsplit("/", 1)[-1])
            if url.endswith("/contents/metadata.yaml"):
                return FakeResponse(
                    200,
                    {"content": base64.b64encode(metadata_text.encode()).decode()},
                )
            return FakeResponse(404, {})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)

    response = client.post(
        "/v1/plugins/submissions/metadata-preview",
        json={"repo": "https://github.com/alice/astrbot_plugin_demo"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "repo": "https://github.com/alice/astrbot_plugin_demo",
        "name": "astrbot_plugin_demo",
        "display_name": "Preview Demo",
        "desc": "Preview metadata description",
        "author": "Preview Author",
        "social_link": "https://example.com/preview",
        "category": "productivity",
        "tags": ["preview", "metadata"],
    }
    assert metadata_filenames == ["metadata.yaml"]
    assert client.app.state.store.list_submissions() == []


def test_submission_metadata_preview_marks_bad_system_token_and_falls_back(
    monkeypatch,
) -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "GITHUB_API_TOKEN": "bad-system-token",
        }
    )
    store = InMemoryMarketStore({"options": {"GITHUB_API_TOKEN": "bad-system-token"}})
    client = TestClient(main_module.create_app(settings=settings, store=store))
    client.get("/v1/auth/debug-login?login=alice")
    metadata_text = "\n".join(
        [
            "name: astrbot_plugin_demo",
            "display_name: Preview Demo",
            "desc: Preview metadata description",
            "author: Preview Author",
        ]
    )
    authorizations: list[str] = []

    class FakeResponse:
        def __init__(
            self,
            status_code: int,
            data: dict[str, object],
            headers: dict[str, str] | None = None,
        ) -> None:
            self.status_code = status_code
            self._data = data
            self.headers = headers or {}

        def json(self) -> dict[str, object]:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            authorization = (kwargs.get("headers") or {}).get("authorization", "")
            authorizations.append(authorization)
            if authorization == "Bearer bad-system-token":
                return FakeResponse(401, {"message": "Bad credentials"})
            if url == "https://api.github.com/repos/alice/astrbot_plugin_demo":
                return FakeResponse(
                    200,
                    {"name": "astrbot_plugin_demo", "owner": {"login": "alice"}},
                )
            if url.endswith("/contents/metadata.yaml"):
                return FakeResponse(
                    200,
                    {"content": base64.b64encode(metadata_text.encode()).decode()},
                )
            return FakeResponse(404, {})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)

    response = client.post(
        "/v1/plugins/submissions/metadata-preview",
        json={"repo": "https://github.com/alice/astrbot_plugin_demo"},
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Preview Demo"
    assert response.json()["desc"] == "Preview metadata description"
    assert "Bearer bad-system-token" in authorizations
    assert "" in authorizations
    statuses = main_module.parse_github_api_token_statuses(
        store.list_options()["GITHUB_API_TOKEN_STATUS"]
    )
    status = statuses[main_module.github_api_token_hash("bad-system-token")]
    assert status["disabled"] is True
    assert status["error_code"] == 401
    admin_login = client.get("/v1/auth/debug-login?login=admin")
    store.update_user_role(admin_login.json()["user"]["id"], Role.CORE_ADMIN.value)
    settings_response = client.get("/v1/admin/settings")
    token_status = settings_response.json()["market"]["api_token_statuses"][0]
    assert token_status["token"] == "b**************n"
    assert token_status["disabled"] is True
    assert token_status["error_code"] == 401


def test_github_rate_limited_system_token_keeps_rotating_with_retry_after() -> None:
    class FakeResponse:
        status_code = 429
        headers = {"retry-after": "90"}

        def json(self) -> dict[str, object]:
            return {"message": "secondary rate limit"}

    status = main_module.github_api_token_status_from_response(FakeResponse())

    assert status is not None
    assert status["disabled"] is False
    assert status["status"] == "rate_limited"
    assert status["error_code"] == 429
    assert status["retry_after_seconds"] == 90


def test_github_forbidden_system_token_is_disabled_when_not_rate_limited() -> None:
    class FakeResponse:
        status_code = 403
        headers = {"x-ratelimit-remaining": "42"}

        def json(self) -> dict[str, object]:
            return {"message": "Resource not accessible by personal access token"}

    status = main_module.github_api_token_status_from_response(FakeResponse())

    assert status is not None
    assert status["disabled"] is True
    assert status["status"] == "disabled"
    assert status["error_code"] == 403


def test_submission_metadata_preview_requires_repo_owner() -> None:
    client = make_client()
    client.get("/v1/auth/debug-login?login=alice")

    response = client.post(
        "/v1/plugins/submissions/metadata-preview",
        json={"repo": "https://github.com/bob/astrbot_plugin_demo"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "GitHub account must own the repository"


def test_submission_metadata_preview_is_rate_limited() -> None:
    client = make_client()
    client.app.state.settings = client.app.state.settings.with_updates(
        github_metadata_sync_enabled=False,
    )
    client.get("/v1/auth/debug-login?login=alice")

    for _ in range(main_module.SUBMISSION_METADATA_PREVIEW_RPM):
        response = client.post(
            "/v1/plugins/submissions/metadata-preview",
            json={"repo": "https://github.com/alice/astrbot_plugin_demo"},
        )
        assert response.status_code == 200

    limited = client.post(
        "/v1/plugins/submissions/metadata-preview",
        json={"repo": "https://github.com/alice/astrbot_plugin_demo"},
    )

    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert "Rate limit exceeded" in limited.json()["error"]


def test_submission_uses_explicit_display_name_and_desc_fields(monkeypatch) -> None:
    client = make_client()
    client.get("/v1/auth/debug-login?login=alice")
    metadata_text = "\n".join(
        [
            "name: astrbot_plugin_demo",
            "display_name: Repo metadata description",
            "desc: Repo metadata description",
        ]
    )

    class FakeResponse:
        def __init__(self, status_code: int, data: dict[str, object]) -> None:
            self.status_code = status_code
            self._data = data
            self.headers = {}

        def json(self) -> dict[str, object]:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            if url == "https://api.github.com/repos/alice/astrbot_plugin_demo":
                return FakeResponse(
                    200,
                    {
                        "stargazers_count": 5,
                        "updated_at": "2026-05-17T00:00:00Z",
                        "default_branch": "main",
                    },
                )
            if url.endswith("/contents/metadata.yml"):
                return FakeResponse(
                    200,
                    {"content": base64.b64encode(metadata_text.encode()).decode()},
                )
            return FakeResponse(404, {})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)

    response = client.post("/v1/plugins/submissions", json=plugin_payload())
    plugin = response.json()

    assert response.status_code == 201
    assert plugin["display_name"] == "Repo metadata description"
    assert plugin["desc"] == "Repo metadata description"


def test_github_metadata_uses_system_fallback_token(monkeypatch) -> None:
    seen_authorizations = []

    class FakeResponse:
        def __init__(self, status_code: int, data: dict[str, object]) -> None:
            self.status_code = status_code
            self._data = data
            self.headers = {}

        def json(self) -> dict[str, object]:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            seen_authorizations.append((kwargs.get("headers") or {}).get("authorization"))
            if url == "https://api.github.com/repos/alice/astrbot_plugin_demo":
                return FakeResponse(
                    200,
                    {
                        "stargazers_count": 7,
                        "updated_at": "2026-05-17T00:00:00Z",
                        "default_branch": "main",
                    },
                )
            return FakeResponse(404, {})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "GITHUB_API_TOKEN": "system-readonly-token",
        }
    )
    client = TestClient(main_module.create_app(settings=settings, store=InMemoryMarketStore()))
    login = client.get("/v1/auth/debug-login?login=alice")
    client.app.state.store.update_user_role(login.json()["user"]["id"], Role.ADMIN.value)

    submission = client.post("/v1/plugins/submissions", json=plugin_payload())
    client.post(f"/v1/admin/plugins/{submission.json()['id']}/list")

    assert "Bearer system-readonly-token" in seen_authorizations
    assert client.get("/v1/plugins").json()["items"][0]["stars"] == 7


def test_admin_github_refresh_is_queued_without_frontend_failure(monkeypatch) -> None:
    class FakeResponse:
        status_code = 403
        headers = {"x-ratelimit-remaining": "0"}

        def json(self) -> dict[str, object]:
            return {"message": "API rate limit exceeded"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    user = client.app.state.store.get_user_by_id(login.json()["user"]["id"])
    client.app.state.store.update_user_role(user["id"], Role.ADMIN.value)
    plugin = client.app.state.store.submit_plugin(user, plugin_payload())

    response = client.post(f"/v1/admin/plugins/{plugin['id']}/refresh-github", json={})
    stored = client.app.state.store.get_plugin(plugin["id"])

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "plugin_id": plugin["id"]}
    assert stored["github_sync_status"] == "error"
    assert "GitHub API rate limit" in stored["github_sync_error"]


def test_plugin_owner_can_force_refresh_with_temporary_token(monkeypatch) -> None:
    seen_authorizations = []
    metadata_text = "version: v3.0.0"

    class FakeResponse:
        def __init__(self, status_code: int, data: dict[str, object]) -> None:
            self.status_code = status_code
            self._data = data
            self.headers = {}

        def json(self) -> dict[str, object]:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            seen_authorizations.append((kwargs.get("headers") or {}).get("authorization"))
            if url == "https://api.github.com/repos/alice/astrbot_plugin_demo":
                return FakeResponse(
                    200,
                    {
                        "stargazers_count": 11,
                        "updated_at": "2026-05-17T00:00:00Z",
                        "default_branch": "main",
                    },
                )
            if url.endswith("/contents/metadata.yml"):
                return FakeResponse(
                    200,
                    {"content": base64.b64encode(metadata_text.encode()).decode()},
                )
            return FakeResponse(404, {})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    plugin = client.app.state.store.submit_plugin(
        client.app.state.store.get_user_by_id(login.json()["user"]["id"]),
        plugin_payload(),
    )
    client.app.state.store.update_plugin_status(plugin["id"], "listed", login.json()["user"]["id"])

    refreshed = client.post(
        f"/v1/plugins/{plugin['id']}/refresh-github",
        json={
            "github_token": "temporary-token",
            "save_token": True,
            "refresh_interval_seconds": 300,
        },
    )

    assert refreshed.status_code == 200
    assert refreshed.json()["stars"] == 11
    assert refreshed.json()["version"] == "v3.0.0"
    assert refreshed.json()["github_refresh_interval_seconds"] == 300
    assert "Bearer temporary-token" in seen_authorizations
    stored_user = client.app.state.store.get_user_by_id(login.json()["user"]["id"])
    assert stored_user["github_token"] == "temporary-token"
    assert stored_user["github_refresh_interval_seconds"] == 300


def test_plugin_refresh_reports_github_rate_limit(monkeypatch) -> None:
    class FakeResponse:
        status_code = 403
        headers = {"x-ratelimit-remaining": "0"}

        def json(self) -> dict[str, object]:
            return {"message": "API rate limit exceeded"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice")
    plugin = client.app.state.store.submit_plugin(
        client.app.state.store.get_user_by_id(login.json()["user"]["id"]),
        plugin_payload(),
    )
    client.app.state.store.update_plugin_status(plugin["id"], "listed", login.json()["user"]["id"])

    response = client.post(f"/v1/plugins/{plugin['id']}/refresh-github", json={})

    assert response.status_code == 429
    assert "GitHub API rate limit" in response.json()["error"]


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
        "from": {"email": "noreply@example.com", "name": "Astrhub Plugins Market"},
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


def test_smtp_email_uses_auto_encryption_by_default(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeSmtpClient:
        is_connected = True

        def __init__(self, **kwargs) -> None:
            calls["options"] = kwargs

        async def connect(self) -> None:
            calls["connected"] = True

        async def login(self, username: str, password: str) -> None:
            calls["login"] = (username, password)

        async def send_message(self, message) -> None:
            calls["message"] = message

        async def quit(self) -> None:
            calls["quit"] = True

    monkeypatch.setattr(main_module.aiosmtplib, "SMTP", FakeSmtpClient)
    settings = load_settings(
        {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "user",
            "SMTP_PASSWORD": "secret",
            "SMTP_FROM": "noreply@example.com",
        }
    )

    asyncio.run(main_module.send_email_via_smtp(settings, "to@example.com", "Hello", "Body"))

    assert calls["options"] == {
        "hostname": "smtp.example.com",
        "port": 587,
        "timeout": 10,
        "use_tls": False,
        "validate_certs": True,
    }
    assert calls["login"] == ("user", "secret")
    assert calls["message"]["From"] == "Astrhub Plugins Market <noreply@example.com>"
    assert calls["message"]["Subject"] == "Hello"
    assert calls["quit"] is True


def test_smtp_email_can_force_auth_login(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeSmtpClient:
        is_connected = True

        def __init__(self, **kwargs) -> None:
            calls["options"] = kwargs

        async def connect(self) -> None:
            pass

        async def auth_login(self, username: str, password: str) -> None:
            calls["auth_login"] = (username, password)

        async def send_message(self, message) -> None:
            calls["message"] = message

        async def quit(self) -> None:
            pass

    monkeypatch.setattr(main_module.aiosmtplib, "SMTP", FakeSmtpClient)
    settings = load_settings(
        {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "SMTP_USERNAME": "user",
            "SMTP_PASSWORD": "secret",
            "SMTP_FROM": "noreply@example.com",
            "SMTP_FROM_NAME": "Custom Sender",
            "SMTP_ENCRYPTION": "ssl_tls",
            "SMTP_AUTH_METHOD": "login",
            "SMTP_VALIDATE_CERTS": "false",
        }
    )

    asyncio.run(main_module.send_email_via_smtp(settings, "to@example.com", "Hello", "Body"))

    assert calls["options"] == {
        "hostname": "smtp.example.com",
        "port": 465,
        "timeout": 10,
        "use_tls": True,
        "validate_certs": False,
    }
    assert calls["auth_login"] == ("user", "secret")
    assert calls["message"]["From"] == "Custom Sender <noreply@example.com>"


def test_smtp_email_error_message_includes_server_response(monkeypatch) -> None:
    class FakeSmtpClient:
        is_connected = True

        def __init__(self, **kwargs) -> None:
            pass

        async def connect(self) -> None:
            raise main_module.aiosmtplib.errors.SMTPResponseException(535, "auth failed")

        async def quit(self) -> None:
            pass

    monkeypatch.setattr(main_module.aiosmtplib, "SMTP", FakeSmtpClient)
    settings = load_settings(
        {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM": "noreply@example.com",
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main_module.send_email_via_smtp(settings, "to@example.com", "Hello", "Body"))

    assert exc_info.value.status_code == 502
    assert "SMTPResponseException code=535 message=auth failed" in exc_info.value.detail


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
    assert client.get("/docs/rest").text == "<html>market</html>"
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
    assert "CREATE TABLE IF NOT EXISTS market_notifications" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS market_options" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS market_plugin_likes" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS market_comment_likes" in SCHEMA_SQL
    assert "github_token text NOT NULL DEFAULT ''" in SCHEMA_SQL
    assert "github_refresh_interval_seconds integer NOT NULL DEFAULT 3600" in SCHEMA_SQL
    assert "github_email text NOT NULL DEFAULT ''" in SCHEMA_SQL
    assert "notification_email text NOT NULL DEFAULT ''" in SCHEMA_SQL
    assert "notify_plugin_review boolean NOT NULL DEFAULT true" in SCHEMA_SQL
    assert "notify_comments boolean NOT NULL DEFAULT true" in SCHEMA_SQL
    assert "notify_replies boolean NOT NULL DEFAULT true" in SCHEMA_SQL
    assert "notify_likes boolean NOT NULL DEFAULT true" in SCHEMA_SQL
    assert "notify_unlist boolean NOT NULL DEFAULT true" in SCHEMA_SQL
    assert "email_notify_plugin_review boolean NOT NULL DEFAULT true" in SCHEMA_SQL
    assert "email_notify_pending_review boolean NOT NULL DEFAULT true" in SCHEMA_SQL
    assert "email_notify_comments boolean NOT NULL DEFAULT false" in SCHEMA_SQL
    assert "email_notify_replies boolean NOT NULL DEFAULT false" in SCHEMA_SQL
    assert "email_notify_likes boolean NOT NULL DEFAULT false" in SCHEMA_SQL
    assert "email_notify_unlist boolean NOT NULL DEFAULT true" in SCHEMA_SQL
    assert "muted_reason text NOT NULL DEFAULT ''" in SCHEMA_SQL
    assert "likes integer NOT NULL DEFAULT 0" in SCHEMA_SQL
    assert "read boolean NOT NULL DEFAULT false" in SCHEMA_SQL
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
                TRUNCATE market_api_keys, market_notifications, market_comment_likes,
                         market_plugin_likes, market_comments,
                         market_submissions, market_plugins, market_announcements,
                         market_options, market_users
                RESTART IDENTITY CASCADE
                """
            )

        admin = await store.create_internal_admin("admin", "hash")
        alice = await store.upsert_github_user({"login": "alice", "name": "Alice"})
        assert admin["role"] == Role.CORE_ADMIN
        assert alice["role"] == Role.USER
        await store.update_user_role(alice["id"], Role.ADMIN.value)
        await store.upsert_options({"SITE_NAME": "Database Site"})
        assert (await store.list_options())["SITE_NAME"] == "Database Site"

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

        unlisted = await store.unlist_plugin(plugin["id"], admin["id"], "Needs fixes")
        assert unlisted and unlisted["unlist_reason"] == "Needs fixes"
        notification = await store.create_notification(
            alice["id"],
            "插件已下架",
            "Demo 已被管理员下架。原因：Needs fixes",
            "plugin_unlisted",
            {"plugin_id": plugin["id"], "reason": "Needs fixes"},
        )
        notifications = await store.list_notifications(alice["id"])
        assert notifications[0]["id"] == notification["id"]
        assert notifications[0]["metadata"]["reason"] == "Needs fixes"

        api_key = await store.issue_api_key("Alice Client", alice["id"], ["market:read"])
        assert api_key["key"].startswith("sk-ah-")
        user_keys = await store.list_api_keys_for_user(alice["id"])
        assert user_keys[0]["id"] == api_key["id"]
        assert await store.delete_api_key(alice["id"], api_key["id"]) == 1
        assert await store.list_api_keys_for_user(alice["id"]) == []

        session = await store.create_session(alice["id"])
        assert (await store.get_user_by_session(session["token"]))["github_login"] == "alice"
        assert await store.revoke_session(session["token"]) is True
        assert await store.get_user_by_session(session["token"]) is None
    finally:
        await store.close()
