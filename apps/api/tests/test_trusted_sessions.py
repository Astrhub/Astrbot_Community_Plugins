from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import Role, hash_password
from app.config import (
    DEFAULT_SESSION_MAX_AGE_SECONDS,
    DEFAULT_TRUSTED_SESSION_MAX_AGE_SECONDS,
    MAX_COOKIE_MAX_AGE_SECONDS,
    load_settings,
)
from app.git_sources.models import OAuthTokenSet, OAuthUserProfile
from app.sessions import hash_session_token, parse_device_label
from app.store import InMemoryMarketStore, LocalFileMarketStore, PgRedisMarketStore

TEST_CREDENTIAL_KEY = "trusted-session-test-master-key-123456"


def _make_app(
    env_overrides: dict[str, str] | None = None,
    *,
    store: InMemoryMarketStore | None = None,
) -> tuple[TestClient, InMemoryMarketStore]:
    env = {
        "PUBLIC_LOGIN_ENABLED": "true",
        "EMAIL_REGISTRATION_ENABLED": "true",
        "EMAIL_VERIFICATION_REQUIRED": "false",
        "GITHUB_LOGIN_ENABLED": "true",
        "GITHUB_CLIENT_ID": "gh-client",
        "GITHUB_CLIENT_SECRET": "gh-secret",
        "GITHUB_CALLBACK_URL": "https://market.example.com/v1/auth/github/callback",
        "GITLAB_LOGIN_ENABLED": "true",
        "GITLAB_CLIENT_ID": "gl-client",
        "GITLAB_CLIENT_SECRET": "gl-secret",
        "GITLAB_CALLBACK_URL": "https://market.example.com/v1/auth/oauth/gitlab/callback",
        "OAUTH_CREDENTIAL_KEY": TEST_CREDENTIAL_KEY,
        "WEB_URL": "https://market.example.com",
        "ENABLE_DEV_AUTH": "false",
        "ARTIFACTS_ENABLED": "false",
    }
    if env_overrides:
        env.update(env_overrides)
    settings = load_settings(env)
    backing = store or InMemoryMarketStore(session_ttl_seconds=settings.session_max_age_seconds)
    app = main_module.create_app(settings=settings, store=backing)
    return TestClient(app), backing


def _cookie_max_age(response: Any, cookie_name: str) -> int | None:
    headers = response.headers.get_list("set-cookie")
    for header in headers:
        if not header.startswith(f"{cookie_name}="):
            continue
        match = re.search(r"Max-Age=(\d+)", header, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def test_parse_device_label_and_config_bounds() -> None:
    assert (
        parse_device_label(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
        )
        == "Edge · Windows"
    )
    assert (
        parse_device_label(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
        )
        == "Safari · iPhone"
    )
    assert parse_device_label("") == "未知设备"

    clamped = load_settings({"TRUSTED_SESSION_MAX_AGE_SECONDS": str(60 * 60 * 24 * 999)})
    assert clamped.session_max_age_seconds == DEFAULT_SESSION_MAX_AGE_SECONDS
    assert clamped.trusted_session_max_age_seconds == MAX_COOKIE_MAX_AGE_SECONDS


def test_password_login_cookie_duration_regular_vs_trusted_and_core_admin() -> None:
    client, store = _make_app()
    cookie_name = client.app.state.settings.session_cookie_name
    store.create_internal_admin("rootadmin", hash_password("adminpass123"))
    store.create_email_user(
        username="alice",
        email="alice@example.com",
        password_hash=hash_password("alicepass123"),
    )

    regular = client.post(
        "/v1/auth/internal/login",
        json={"username": "alice", "password": "alicepass123"},
        headers={"user-agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0"},
    )
    assert regular.status_code == 200
    assert _cookie_max_age(regular, cookie_name) == DEFAULT_SESSION_MAX_AGE_SECONDS
    regular_session = regular.json()["session"]
    assert regular_session["trusted"] is False
    assert regular_session["device_label"] == "Firefox · Linux"
    assert "token" not in regular_session
    assert "token_hash" not in regular_session

    trusted_client = TestClient(client.app)
    trusted = trusted_client.post(
        "/v1/auth/internal/login",
        json={"username": "rootadmin", "password": "adminpass123", "trust_device": True},
        headers={
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        },
    )
    assert trusted.status_code == 200
    assert _cookie_max_age(trusted, cookie_name) == DEFAULT_TRUSTED_SESSION_MAX_AGE_SECONDS
    trusted_session = trusted.json()["session"]
    assert trusted_session["trusted"] is True
    assert trusted_session["device_label"] == "Chrome · macOS"
    assert "token" not in trusted_session

    # Only SHA-256 hashes are kept in store state.
    assert all("token" not in item and item["token_hash"] for item in store.state["sessions"])

    session_check = trusted_client.get("/v1/auth/session")
    assert session_check.status_code == 200
    assert session_check.json()["session"]["trusted"] is True
    assert session_check.json()["session"]["is_current"] is True


def test_email_direct_register_honors_trust_device() -> None:
    client, _store = _make_app()
    cookie_name = client.app.state.settings.session_cookie_name

    res = client.post(
        "/v1/auth/email/register",
        json={
            "username": "newdev",
            "email": "newdev@example.com",
            "password": "password123",
            "trust_device": True,
        },
    )
    assert res.status_code == 201
    assert _cookie_max_age(res, cookie_name) == DEFAULT_TRUSTED_SESSION_MAX_AGE_SECONDS
    assert res.json()["session"]["trusted"] is True
    assert "token" not in res.json()["session"]


def test_fixed_absolute_expiry_is_not_extended_on_access() -> None:
    store = InMemoryMarketStore(session_ttl_seconds=60)
    user = store.create_email_user(
        username="bob",
        email="bob@example.com",
        password_hash=hash_password("password123"),
    )
    created = store.create_session(user["id"], trusted=False, ttl_seconds=120)
    initial_expires_at = created["expires_at"]

    assert store.get_user_by_session(created["token"]) is not None
    stored = store.state["sessions"][0]
    assert stored["expires_at"] == initial_expires_at

    # Expire the session and verify access fails immediately.
    stored["expires_at"] = (
        (datetime.now(UTC) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    )
    assert store.get_user_by_session(created["token"]) is None
    assert store.state["sessions"] == []


def test_github_and_third_party_oauth_trust_device_and_bind_preserves_session(
    monkeypatch: Any,
) -> None:
    client, store = _make_app()
    cfg = client.app.state.settings
    cookie_name = cfg.session_cookie_name

    async def fake_exchange(_settings: Any, _code: str) -> str:
        return "gho_fake_token"

    async def fake_profile(_token: str) -> dict[str, Any]:
        return {"id": 1001, "login": "octo", "name": "Octo", "email": "octo@example.com"}

    monkeypatch.setattr(main_module, "exchange_github_code", fake_exchange)
    monkeypatch.setattr(main_module, "fetch_github_profile", fake_profile)

    # 1. Login via GitHub with trust_device=1
    login_resp = client.get(
        "/v1/auth/oauth/github/login?next=/submit&trust_device=1",
        follow_redirects=False,
    )
    assert login_resp.status_code == 302
    assert "trust_device=1" in login_resp.headers["location"]

    gh_login = client.get(login_resp.headers["location"], follow_redirects=False)
    assert gh_login.status_code in {302, 307}
    state = client.cookies[cfg.oauth_state_cookie_name]
    assert client.cookies["github_oauth_trust_device"] == "1"

    cb = client.get(f"/v1/auth/github/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code in {302, 307}
    assert _cookie_max_age(cb, cookie_name) == DEFAULT_TRUSTED_SESSION_MAX_AGE_SECONDS
    trusted_token = client.cookies[cookie_name]
    assert trusted_token
    assert store.get_session_by_token(trusted_token)["trusted"] is True

    # 2. Already logged-in user binds GitLab without trust_device -> must preserve 1-year session!
    class FakeGitLabAdapter:
        def build_authorize_url(self, *, state: str) -> str:
            return f"https://gitlab.com/oauth/authorize?state={state}"

        async def exchange_code(self, code: str) -> OAuthTokenSet:
            return OAuthTokenSet(
                provider="gitlab",
                access_token="gl_token",
                refresh_token="gl_refresh",
                scope="read_user",
                provider_user_id="9001",
                provider_login="octo_gl",
            )

        async def fetch_user_profile(self, access_token: str) -> OAuthUserProfile:
            return OAuthUserProfile(
                provider="gitlab",
                instance_url="https://gitlab.com",
                provider_user_id="9001",
                provider_login="octo_gl",
                display_name="Octo GL",
                email="octo@example.com",
            )

    client.app.state.git_provider_adapter_factory = lambda _settings, _provider: FakeGitLabAdapter()
    gl_start = client.get(
        "/v1/auth/oauth/gitlab/login?next=/settings/personal", follow_redirects=False
    )
    assert gl_start.status_code == 302
    gl_state = client.cookies[f"{cfg.oauth_state_cookie_name}_gitlab"]
    gl_cb = client.get(
        f"/v1/auth/oauth/gitlab/callback?code=glcode&state={gl_state}",
        follow_redirects=False,
    )
    assert gl_cb.status_code == 302
    assert client.cookies[cookie_name] == trusted_token
    sessions = client.get("/v1/me/sessions").json()["items"]
    assert len(sessions) == 1
    assert sessions[0]["trusted"] is True


def test_session_management_endpoints_ownership_cache_headers_and_openapi() -> None:
    client_a1, store = _make_app()
    alice = store.create_email_user(
        username="alice",
        email="alice@example.com",
        password_hash=hash_password("password123"),
    )
    store.create_email_user(
        username="bob",
        email="bob@example.com",
        password_hash=hash_password("password123"),
    )

    # Alice logs in on 3 devices (1 trusted, 2 regular)
    r1 = client_a1.post(
        "/v1/auth/internal/login",
        json={"username": "alice", "password": "password123", "trust_device": True},
        headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/131.0.0.0"},
    )
    assert r1.status_code == 200
    a1_id = r1.json()["session"]["id"]

    client_a2 = TestClient(client_a1.app)
    r2 = client_a2.post(
        "/v1/auth/internal/login",
        json={"username": "alice", "password": "password123", "trust_device": False},
        headers={"user-agent": "Mozilla/5.0 (Android 14) Chrome/131.0.0.0"},
    )
    assert r2.status_code == 200
    a2_id = r2.json()["session"]["id"]

    client_a3 = TestClient(client_a1.app)
    r3 = client_a3.post(
        "/v1/auth/internal/login",
        json={"username": "alice", "password": "password123", "trust_device": True},
    )
    assert r3.status_code == 200

    # List sessions from device 1
    listing = client_a1.get("/v1/me/sessions")
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "private, no-store"
    items = listing.json()["items"]
    assert len(items) == 3
    by_id = {item["id"]: item for item in items}
    assert by_id[a1_id]["is_current"] is True
    assert by_id[a1_id]["trusted"] is True
    assert by_id[a2_id]["is_current"] is False
    assert by_id[a2_id]["trusted"] is False
    assert all("token" not in item and "token_hash" not in item for item in items)

    # Bob cannot revoke Alice's session
    client_bob = TestClient(client_a1.app)
    assert (
        client_bob.post(
            "/v1/auth/internal/login",
            json={"username": "bob", "password": "password123"},
        ).status_code
        == 200
    )
    forbidden = client_bob.delete(f"/v1/me/sessions/{a2_id}")
    assert forbidden.status_code == 404
    assert client_a2.get("/v1/me").status_code == 200

    # Alice revokes device 2
    revoked_other = client_a1.delete(f"/v1/me/sessions/{a2_id}")
    assert revoked_other.status_code == 200
    assert revoked_other.headers["cache-control"] == "private, no-store"
    assert revoked_other.json() == {"revoked": True, "is_current": False}
    assert client_a2.get("/v1/me").status_code == 401
    assert client_a1.get("/v1/me").status_code == 200

    # Alice revokes all other sessions (kicks device 3, keeps device 1)
    revoked_others = client_a1.post("/v1/me/sessions/revoke-others")
    assert revoked_others.status_code == 200
    assert revoked_others.headers["cache-control"] == "private, no-store"
    assert revoked_others.json() == {"revoked_count": 1}
    assert client_a3.get("/v1/me").status_code == 401
    assert len(client_a1.get("/v1/me/sessions").json()["items"]) == 1

    # OpenAPI hides /v1/me/sessions from guests, exposes to authenticated users
    anon_client = TestClient(client_a1.app)
    public_openapi = anon_client.get("/openapi.json").json()
    assert "/v1/me/sessions" not in public_openapi["paths"]
    user_openapi = client_a1.get("/openapi.json").json()
    assert "/v1/me/sessions" in user_openapi["paths"]

    # Password change revokes other devices and keeps current device's trusted status
    client_a4 = TestClient(client_a1.app)
    assert (
        client_a4.post(
            "/v1/auth/internal/login",
            json={"username": "alice", "password": "password123", "trust_device": True},
        ).status_code
        == 200
    )
    cookie_name = client_a1.app.state.settings.session_cookie_name
    pw_resp = client_a1.post(
        "/v1/me/password",
        json={"current_password": "password123", "new_password": "newpassword456"},
    )
    assert pw_resp.status_code == 200
    assert _cookie_max_age(pw_resp, cookie_name) == DEFAULT_TRUSTED_SESSION_MAX_AGE_SECONDS
    assert client_a4.get("/v1/me").status_code == 401
    remaining = client_a1.get("/v1/me/sessions").json()["items"]
    assert len(remaining) == 1
    assert remaining[0]["trusted"] is True
    assert remaining[0]["is_current"] is True

    # Revoking current device clears cookie and logs out
    revoke_self = client_a1.delete(f"/v1/me/sessions/{remaining[0]['id']}")
    assert revoke_self.status_code == 200
    assert revoke_self.json() == {"revoked": True, "is_current": True}
    assert client_a1.get("/v1/me").status_code == 401
    assert alice["id"]


def test_local_file_store_migrates_legacy_sessions_and_persists_trusted_devices(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "market-state.json"
    store = LocalFileMarketStore(data_path, session_ttl_seconds=60)
    store.connect()
    admin = store.create_internal_admin("owner", hash_password("password123"))
    trusted = store.create_session(
        admin["id"],
        trusted=True,
        ttl_seconds=DEFAULT_TRUSTED_SESSION_MAX_AGE_SECONDS,
        user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/131.0.0.0",
        ip_address="127.0.0.1",
    )
    store.close()

    # Inject a legacy plaintext session without expires_at to verify migration on load.
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    raw["sessions"].append(
        {
            "token": "sess_legacy_token_abc",
            "user_id": admin["id"],
            "session_version": 1,
            "created_at": now_iso,
            "last_seen_at": now_iso,
        }
    )
    data_path.write_text(json.dumps(raw), encoding="utf-8")

    reloaded = LocalFileMarketStore(data_path, session_ttl_seconds=3600)
    reloaded.connect()
    assert reloaded.get_user_by_session(trusted["token"]) is not None
    assert reloaded.get_user_by_session("sess_legacy_token_abc") is not None
    listed = reloaded.list_user_sessions(admin["id"])
    assert len(listed) == 2
    assert any(item["trusted"] is True for item in listed)

    revoked_count = reloaded.revoke_other_user_sessions(admin["id"], trusted["token"])
    assert revoked_count == 1
    assert reloaded.get_user_by_session("sess_legacy_token_abc") is None
    assert reloaded.get_user_by_session(trusted["token"]) is not None

    persisted = json.loads(data_path.read_text(encoding="utf-8"))
    assert len(persisted["sessions"]) == 1
    assert "token" not in persisted["sessions"][0]
    assert persisted["sessions"][0]["token_hash"] == hash_session_token(trusted["token"])


def test_pg_redis_store_recovers_trusted_session_from_pg_when_redis_is_flushed() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}
            self.ttls: dict[str, int | None] = {}

        async def get(self, key: str) -> str | None:
            return self.data.get(key)

        async def set(self, key: str, value: str, ex: int | None = None) -> None:
            self.data[key] = value
            self.ttls[key] = ex

        async def delete(self, key: str) -> int:
            self.ttls.pop(key, None)
            return int(self.data.pop(key, None) is not None)

        async def scan_iter(self, match: str, count: int = 200):
            prefix = match.removesuffix("*")
            for key in list(self.data):
                if key.startswith(prefix):
                    yield key

    class FakePool:
        def __init__(self, user_row: dict[str, Any]) -> None:
            self.user_row = dict(user_row)
            self.sessions: dict[str, dict[str, Any]] = {}

        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            if "INSERT INTO market_user_sessions" in query:
                row = {
                    "id": args[0],
                    "user_id": args[1],
                    "token_hash": args[2],
                    "trusted": args[3],
                    "session_version": args[4],
                    "device_label": args[5],
                    "user_agent": args[6],
                    "ip_address": args[7],
                    "created_at": args[8],
                    "last_seen_at": args[8],
                    "expires_at": args[9],
                    "revoked_at": None,
                }
                self.sessions[row["id"]] = row
                return dict(row)
            if "FROM market_user_sessions" in query and "WHERE token_hash = $1" in query:
                for row in self.sessions.values():
                    if row["token_hash"] == args[0] and row["revoked_at"] is None:
                        return dict(row)
                return None
            if "UPDATE market_user_sessions" in query and "WHERE id = $1 AND user_id = $2" in query:
                row = self.sessions.get(args[0])
                if row and row["user_id"] == args[1] and row["revoked_at"] is None:
                    row["revoked_at"] = datetime.now(UTC)
                    return dict(row)
                return None
            if "FROM market_users WHERE id = $1" in query:
                return dict(self.user_row) if args[0] == self.user_row["id"] else None
            raise AssertionError(f"Unexpected fetchrow query: {query}")

        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM market_user_sessions" in query and "WHERE user_id = $1" in query:
                now = datetime.now(UTC)
                return [
                    dict(row)
                    for row in self.sessions.values()
                    if row["user_id"] == args[0]
                    and row["revoked_at"] is None
                    and row["expires_at"] > now
                    and row["session_version"] == args[1]
                ]
            raise AssertionError(f"Unexpected fetch query: {query}")

        async def execute(self, query: str, *args: Any) -> str:
            if "UPDATE market_user_sessions" in query and "SET last_seen_at" in query:
                row = self.sessions.get(args[0])
                if row and row["revoked_at"] is None:
                    row["last_seen_at"] = args[1]
                    return "UPDATE 1"
                return "UPDATE 0"
            if "UPDATE market_user_sessions" in query and "WHERE token_hash = $1" in query:
                for row in self.sessions.values():
                    if row["token_hash"] == args[0] and row["revoked_at"] is None:
                        row["revoked_at"] = datetime.now(UTC)
                        return "UPDATE 1"
                return "UPDATE 0"
            raise AssertionError(f"Unexpected execute query: {query}")

    async def run() -> None:
        user_row = {
            "id": "usr_1",
            "role": Role.USER.value,
            "public_username": "alice",
            "github_login": "",
            "github_name": "Alice",
            "internal_username": "",
            "auth_source": "email",
            "email": "alice@example.com",
            "email_verified_at": None,
            "session_version": 1,
            "password_hash": "",
            "avatar_url": "",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        store = PgRedisMarketStore(
            "postgresql://unused",
            "redis://unused",
            DEFAULT_SESSION_MAX_AGE_SECONDS,
        )
        store.redis = FakeRedis()
        store.pool = FakePool(user_row)

        session = await store.create_session(
            "usr_1",
            trusted=True,
            ttl_seconds=DEFAULT_TRUSTED_SESSION_MAX_AGE_SECONDS,
            user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/131.0.0.0",
            ip_address="203.0.113.10",
        )
        redis_key = store._session_key(session["token_hash"])
        assert redis_key in store.redis.data
        assert store.redis.ttls[redis_key] == 3600
        assert session["token"] not in store.redis.data[redis_key]

        # Simulate Redis flush / eviction: 1-year trusted session must recover from PG!
        store.redis.data.clear()
        store.redis.ttls.clear()
        recovered = await store.get_user_by_session(session["token"])
        assert recovered is not None and recovered["id"] == "usr_1"
        assert redis_key in store.redis.data

        # Revoking by session ID removes both PG active state and Redis cache immediately.
        revoked = await store.revoke_user_session("usr_1", session["id"])
        assert revoked is not None
        assert redis_key not in store.redis.data
        assert await store.get_user_by_session(session["token"]) is None

    asyncio.run(run())
