"""Comprehensive tests for email accounts, verification switches, multi-OAuth binding, and 5-tier hierarchy."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import hash_password
from app.config import load_settings
from app.store import InMemoryMarketStore, LocalFileMarketStore
from tests.github_access_helpers import install_github_access_double

CODE_PATTERN = re.compile(r"(\d{6})")


def _make_app(
    overrides: dict[str, str] | None = None,
    *,
    store: InMemoryMarketStore | LocalFileMarketStore | None = None,
) -> tuple[TestClient, InMemoryMarketStore | LocalFileMarketStore]:
    env = {
        "ENABLE_DEV_AUTH": "true",
        "CORS_ORIGIN": "http://127.0.0.1:3000",
        "WEB_URL": "https://market.example",
        "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "GITHUB_LOGIN_ENABLED": "true",
        "GITHUB_CLIENT_ID": "gh-client",
        "GITHUB_CLIENT_SECRET": "gh-secret-value",
        "EMAIL_REGISTRATION_ENABLED": "true",
        "EMAIL_VERIFICATION_REQUIRED": "true",
        "EMAIL_PROVIDER": "smtp",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_FROM": "noreply@example.com",
        "EMAIL_DAILY_LIMIT": "0",
        "EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER": "5",
        **(overrides or {}),
    }
    settings = load_settings(env)
    market_store = store if store is not None else InMemoryMarketStore()
    app = main_module.create_app(settings=settings, store=market_store)
    install_github_access_double(app)
    return TestClient(app, follow_redirects=False), market_store


def _capture_emails(monkeypatch: Any) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []

    async def fake_smtp(_settings: Any, receiver: str, subject: str, content: str) -> None:
        sent.append({"to": receiver, "subject": subject, "body": content})

    monkeypatch.setattr(main_module, "send_email_via_smtp", fake_smtp)
    return sent


def _extract_code(mail: dict[str, str]) -> str:
    match = CODE_PATTERN.search(mail["body"])
    assert match, f"No 6-digit code in mail: {mail}"
    return match.group(1)


def _bind_github(
    client: TestClient,
    monkeypatch: Any,
    *,
    gh_id: str = "42001",
    login: str = "alice_gh",
    name: str = "GitHub Alice",
    avatar_url: str = "https://avatars.githubusercontent.com/u/42001",
    email: str = "alice-gh@example.com",
) -> None:
    async def fake_exchange(*_args: Any, **_kwargs: Any) -> str:
        return "gh-oauth-token"

    async def fake_profile(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "id": gh_id,
            "login": login,
            "name": name,
            "avatar_url": avatar_url,
            "email": email,
        }

    monkeypatch.setattr(main_module, "exchange_github_code", fake_exchange)
    monkeypatch.setattr(main_module, "fetch_github_profile", fake_profile)
    login_resp = client.get("/v1/auth/github/login?next=/settings/personal")
    assert login_resp.status_code in {302, 307}
    state = client.cookies[client.app.state.settings.oauth_state_cookie_name]
    callback = client.get("/v1/auth/github/callback", params={"code": "code-123", "state": state})
    assert callback.status_code in {302, 307}


def test_direct_email_registration_and_login_by_username_or_email() -> None:
    client, _store = _make_app(
        {
            "EMAIL_REGISTRATION_ENABLED": "true",
            "EMAIL_VERIFICATION_REQUIRED": "false",
            "EMAIL_PROVIDER": "disabled",
        }
    )
    site = client.get("/v1/site").json()
    assert site["auth"]["email_registration_enabled"] is True
    assert site["auth"]["email_verification_required"] is False
    assert site["auth"]["email_service_enabled"] is False

    # 2-step endpoint is rejected when verification switch is off
    req_rejected = client.post(
        "/v1/auth/email/register/request",
        json={"username": "alice_mail", "email": "Alice@Example.com", "password": "password123"},
    )
    assert req_rejected.status_code == 400
    assert req_rejected.json()["code"] == "email_verification_not_required"

    # 1-step direct registration succeeds and sets session cookie
    reg = client.post(
        "/v1/auth/email/register",
        json={"username": "alice_mail", "email": "Alice@Example.com", "password": "password123"},
    )
    assert reg.status_code == 201
    user = reg.json()["user"]
    assert user["public_username"] == "alice_mail"
    assert user["auth_source"] == "email"
    assert user["email"] == "Alice@Example.com"
    assert user["email_verified"] is False
    assert user["account_tier"] == "user"
    assert user["is_developer"] is False
    assert user["has_oauth_binding"] is False
    assert user["has_password"] is True

    # Case-insensitive email & username uniqueness returns 409
    dup_email = client.post(
        "/v1/auth/email/register",
        json={"username": "other_user", "email": "alice@example.com", "password": "password123"},
    )
    assert dup_email.status_code == 409
    assert dup_email.json()["code"] == "email_already_exists"

    dup_user = client.post(
        "/v1/auth/email/register",
        json={"username": "ALICE_MAIL", "email": "other@example.com", "password": "password123"},
    )
    assert dup_user.status_code == 409
    assert dup_user.json()["code"] == "username_already_exists"

    # Login works with both username and email (case-insensitive)
    client.post("/v1/auth/logout")
    by_email = client.post(
        "/v1/auth/internal/login",
        json={"username": "alice@example.com", "password": "password123"},
    )
    assert by_email.status_code == 200
    assert by_email.json()["user"]["public_username"] == "alice_mail"

    client.post("/v1/auth/logout")
    by_username = client.post(
        "/v1/auth/internal/login",
        json={"username": "alice_mail", "password": "password123"},
    )
    assert by_username.status_code == 200
    assert by_username.json()["user"]["account_tier"] == "user"


def test_verified_email_registration_challenges_security_and_limits(monkeypatch: Any) -> None:
    sent = _capture_emails(monkeypatch)
    client, _store = _make_app(
        {
            "EMAIL_REGISTRATION_ENABLED": "true",
            "EMAIL_VERIFICATION_REQUIRED": "true",
            "EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER": "2",
        }
    )

    # Direct register is blocked when verification is required
    direct_blocked = client.post(
        "/v1/auth/email/register",
        json={"username": "bob_mail", "email": "bob@example.com", "password": "password123"},
    )
    assert direct_blocked.status_code == 400
    assert direct_blocked.json()["code"] == "email_verification_required"

    # Request registration code
    req = client.post(
        "/v1/auth/email/register/request",
        json={"username": "bob_mail", "email": "bob@example.com", "password": "password123"},
    )
    assert req.status_code == 202
    reg_id = req.json()["registration_id"]
    assert len(sent) == 1
    code = _extract_code(sent[0])

    # Cooldown rejects immediate resend with Retry-After
    resend = client.post(
        "/v1/auth/email/register/request",
        json={"username": "bob_mail", "email": "bob@example.com", "password": "password123"},
    )
    assert resend.status_code == 429
    assert resend.json()["code"] == "verification_resend_cooldown"
    assert int(resend.headers.get("retry-after", "0")) > 0

    # Cross-purpose code rejected on password reset verify
    cross_purpose = client.post(
        "/v1/auth/email/password-reset/verify",
        json={"reset_id": reg_id, "code": code, "new_password": "newpassword123"},
    )
    assert cross_purpose.status_code == 400
    assert cross_purpose.json()["code"] == "challenge_expired"

    # Wrong code increments attempts; right code completes registration
    wrong = client.post(
        "/v1/auth/email/register/verify",
        json={"registration_id": reg_id, "code": "000000" if code != "000000" else "111111"},
    )
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "invalid_verification_code"

    verified = client.post(
        "/v1/auth/email/register/verify",
        json={"registration_id": reg_id, "code": code},
    )
    assert verified.status_code == 201
    user = verified.json()["user"]
    assert user["email_verified"] is True
    assert user["email_verified_at"] is not None

    # Replay fails
    replay = client.post(
        "/v1/auth/email/register/verify",
        json={"registration_id": reg_id, "code": code},
    )
    assert replay.status_code == 400
    assert replay.json()["code"] == "challenge_expired"

    # Anti-enumeration: requesting registration with existing email still returns 202 without sending mail
    client.app.state.store.state["cooldowns"].clear()
    before_count = len(sent)
    enum_req = client.post(
        "/v1/auth/email/register/request",
        json={"username": "another_bob", "email": "bob@example.com", "password": "password123"},
    )
    assert enum_req.status_code == 202
    assert len(sent) == before_count


def test_verification_max_attempts_and_send_failure_rollback(monkeypatch: Any) -> None:
    client, store = _make_app(
        {"EMAIL_REGISTRATION_ENABLED": "true", "EMAIL_VERIFICATION_REQUIRED": "true"}
    )

    async def failing_smtp(*_args: Any, **_kwargs: Any) -> None:
        raise main_module.error(502, "SMTP unavailable")

    monkeypatch.setattr(main_module, "send_email_via_smtp", failing_smtp)
    failed = client.post(
        "/v1/auth/email/register/request",
        json={"username": "retry_user", "email": "retry@example.com", "password": "password123"},
    )
    assert failed.status_code == 502
    assert not store.state["emailChallenges"]
    assert not store.state["cooldowns"]

    # Retry succeeds immediately after mail provider recovers
    sent = _capture_emails(monkeypatch)
    req = client.post(
        "/v1/auth/email/register/request",
        json={"username": "retry_user", "email": "retry@example.com", "password": "password123"},
    )
    assert req.status_code == 202
    reg_id = req.json()["registration_id"]
    real_code = _extract_code(sent[0])
    bad_code = "000000" if real_code != "000000" else "999999"

    for _attempt in range(4):
        resp = client.post(
            "/v1/auth/email/register/verify",
            json={"registration_id": reg_id, "code": bad_code},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_verification_code"

    fifth = client.post(
        "/v1/auth/email/register/verify",
        json={"registration_id": reg_id, "code": bad_code},
    )
    assert fifth.status_code == 429
    assert fifth.json()["code"] == "verification_attempts_exceeded"

    # Challenge was deleted so even the real code can no longer be used
    after = client.post(
        "/v1/auth/email/register/verify",
        json={"registration_id": reg_id, "code": real_code},
    )
    assert after.status_code == 400
    assert after.json()["code"] == "challenge_expired"


def test_five_tier_lifecycle_oauth_binding_unbinding_and_api_keys(monkeypatch: Any) -> None:
    client, store = _make_app(
        {
            "EMAIL_REGISTRATION_ENABLED": "true",
            "EMAIL_VERIFICATION_REQUIRED": "false",
            "PLUGIN_AUTO_APPROVE_ENABLED": "true",
        }
    )

    # 1. Guest submits -> 401
    assert (
        client.post(
            "/v1/plugins/submissions",
            json={
                "name": "astrbot_plugin_demo",
                "desc": "Demo",
                "author": "alice",
                "repo": "https://github.com/alice_gh/astrbot_plugin_demo",
            },
        ).status_code
        == 401
    )

    # 2. Register email user -> user tier
    reg = client.post(
        "/v1/auth/email/register",
        json={"username": "alice_local", "email": "alice@example.com", "password": "password123"},
    )
    assert reg.status_code == 201
    client.patch(
        "/v1/me/profile",
        json={"github_name": "Custom Alice", "avatar_url": "https://example.com/custom.png"},
    )

    # Check permissions for user tier
    perms = client.get("/v1/permissions").json()
    assert perms["account_tier"] == "user"
    assert perms["is_developer"] is False
    assert perms["has_oauth_binding"] is False
    assert perms["can_submit_plugin"] is False
    assert perms["can_create_write_api_key"] is False

    # User tier can create read-only API key, CANNOT create write API key
    read_key = client.post("/v1/me/api-keys", json={"name": "ReadOnly", "scopes": ["market:read"]})
    assert read_key.status_code == 201
    assert read_key.json()["scopes"] == ["market:read"]

    write_key_blocked = client.post(
        "/v1/me/api-keys", json={"name": "WriteBlocked", "scopes": ["market:read", "market:write"]}
    )
    assert write_key_blocked.status_code == 403
    assert write_key_blocked.json()["code"] == "developer_required"

    # User tier CANNOT submit plugin or preview metadata
    preview_blocked = client.post(
        "/v1/plugins/submissions/metadata-preview",
        json={"repo": "https://github.com/alice_gh/astrbot_plugin_demo"},
    )
    assert preview_blocked.status_code == 403
    assert preview_blocked.json()["code"] == "oauth_binding_required"

    submit_blocked = client.post(
        "/v1/plugins/submissions",
        json={
            "name": "astrbot_plugin_demo",
            "desc": "Demo",
            "author": "alice_local",
            "repo": "https://github.com/alice_gh/astrbot_plugin_demo",
        },
    )
    assert submit_blocked.status_code == 403
    assert submit_blocked.json()["code"] == "oauth_binding_required"

    # 3. Bind GitHub OAuth -> upgrades to developer without overwriting local profile
    _bind_github(client, monkeypatch)
    me_after_bind = client.get("/v1/me").json()
    assert me_after_bind["auth_source"] == "email"
    assert me_after_bind["public_username"] == "alice_local"
    assert me_after_bind["github_name"] == "Custom Alice"
    assert me_after_bind["avatar_url"] == "https://example.com/custom.png"
    assert me_after_bind["account_tier"] == "developer"
    assert me_after_bind["is_developer"] is True
    assert me_after_bind["has_oauth_binding"] is True
    assert len(me_after_bind["oauth_identities"]) == 1
    identity_id = me_after_bind["oauth_identities"][0]["id"]

    # Developer can create write API key
    write_key = client.post(
        "/v1/me/api-keys", json={"name": "DevWrite", "scopes": ["market:read", "market:write"]}
    )
    assert write_key.status_code == 201
    write_secret = write_key.json()["key"]

    # Developer can submit plugin (owner_username uses public_username)
    submitted = client.post(
        "/v1/plugins/submissions",
        json={
            "name": "astrbot_plugin_demo",
            "desc": "Demo plugin",
            "author": "alice_local",
            "repo": "https://github.com/alice_gh/astrbot_plugin_demo",
        },
    )
    assert submitted.status_code == 201
    plugin = submitted.json()
    assert plugin["status"] == "listed"
    assert plugin["owner_username"] == "alice_local"

    # 4. Unbind GitHub OAuth (allowed because user has local password)
    unbind = client.delete(f"/v1/me/oauth-identities/{identity_id}")
    assert unbind.status_code == 200
    after_unbind = unbind.json()["user"]
    assert after_unbind["has_oauth_binding"] is False
    assert after_unbind["has_active_plugins"] is True
    # Still developer while listed plugin remains active!
    assert after_unbind["account_tier"] == "developer"
    assert after_unbind["is_developer"] is True

    # Cannot submit new plugin or change repo without active OAuth binding
    new_submit = client.post(
        "/v1/plugins/submissions",
        json={
            "name": "astrbot_plugin_second",
            "desc": "Second",
            "author": "alice_local",
            "repo": "https://github.com/alice_gh/astrbot_plugin_second",
        },
    )
    assert new_submit.status_code == 403
    assert new_submit.json()["code"] == "oauth_binding_required"

    # Can still self-unlist existing plugin AND disable CDN without OAuth token!
    cdn_off = client.patch(f"/v1/plugins/{plugin['id']}/cdn", json={"enabled": False})
    assert cdn_off.status_code == 200
    unlisted = client.post(f"/v1/plugins/{plugin['id']}/unlist", json={"reason": "暂停维护"})
    assert unlisted.status_code == 200
    assert unlisted.json()["status"] == "unlisted"

    # Cannot re-list without rebinding OAuth
    relist_blocked = client.post(f"/v1/plugins/{plugin['id']}/request-list")
    assert relist_blocked.status_code == 403
    assert relist_blocked.json()["code"] == "oauth_binding_required"

    # After all plugins are unlisted and no OAuth remains -> drops back to user tier
    me_downgraded = client.get("/v1/me").json()
    assert me_downgraded["account_tier"] == "user"
    assert me_downgraded["is_developer"] is False

    # Previously issued write API key fails live market:write verification after downgrade
    all_keys = store.list_api_keys()
    ok_read, status_read, _ = asyncio.run(
        main_module.verify_api_key_access(store, f"Bearer {write_secret}", all_keys, "market:read")
    )
    assert ok_read is True and status_read == 200
    ok_write, status_write, _ = asyncio.run(
        main_module.verify_api_key_access(store, f"Bearer {write_secret}", all_keys, "market:write")
    )
    assert ok_write is False and status_write == 403


def test_oauth_lockout_protection_and_first_time_password_setup() -> None:
    client, _store = _make_app()
    login = client.get("/v1/auth/debug-login", params={"login": "gh_only_dev"})
    assert login.status_code == 200
    me = login.json()["user"]
    assert me["has_password"] is False
    assert me["account_tier"] == "developer"
    identity_id = me["oauth_identities"][0]["id"]

    # Unbinding last OAuth without password is blocked
    blocked = client.delete(f"/v1/me/oauth-identities/{identity_id}")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "last_auth_method_protected"

    # Set first-time password (+ optional email)
    pw_set = client.post(
        "/v1/me/password",
        json={"new_password": "localpassword123", "email": "ghdev@example.com"},
    )
    assert pw_set.status_code == 200
    assert pw_set.json()["user"]["has_password"] is True
    assert pw_set.json()["user"]["email"] == "ghdev@example.com"
    assert pw_set.json()["user"]["email_verified"] is False

    # Now unbinding last OAuth succeeds
    unbound = client.delete(f"/v1/me/oauth-identities/{identity_id}")
    assert unbound.status_code == 200
    assert unbound.json()["user"]["has_oauth_binding"] is False
    assert unbound.json()["user"]["account_tier"] == "user"


def test_password_reset_verifies_unverified_email_and_revokes_all_sessions(
    monkeypatch: Any,
) -> None:
    sent = _capture_emails(monkeypatch)
    client, _store = _make_app(
        {
            "EMAIL_REGISTRATION_ENABLED": "true",
            "EMAIL_VERIFICATION_REQUIRED": "false",
            "EMAIL_PROVIDER": "smtp",
        }
    )

    # Register unverified account (session 1)
    reg = client.post(
        "/v1/auth/email/register",
        json={
            "username": "reset_alice",
            "email": "reset@example.com",
            "password": "oldpassword123",
        },
    )
    assert reg.status_code == 201
    assert reg.json()["user"]["email_verified"] is False
    session1_token = reg.json()["session"]["token"]

    # Create a second session on another client
    client2 = TestClient(client.app)
    login2 = client2.post(
        "/v1/auth/internal/login",
        json={"username": "reset@example.com", "password": "oldpassword123"},
    )
    assert login2.status_code == 200
    session2_token = login2.json()["session"]["token"]

    # Request password reset (works even for unverified email!)
    req = client.post(
        "/v1/auth/email/password-reset/request",
        json={"email": "reset@example.com"},
    )
    assert req.status_code == 202
    assert len(sent) == 1
    code = _extract_code(sent[0])

    # Verify reset
    done = client.post(
        "/v1/auth/email/password-reset/verify",
        json={"reset_id": req.json()["reset_id"], "code": code, "new_password": "newpassword456"},
    )
    assert done.status_code == 200

    # Both old sessions are immediately revoked via session_version bump
    cookie_name = client.app.state.settings.session_cookie_name
    client.cookies.set(cookie_name, session1_token)
    assert client.get("/v1/me").status_code == 401
    client2.cookies.set(cookie_name, session2_token)
    assert client2.get("/v1/me").status_code == 401

    # New password works and email is now verified
    relogin = client.post(
        "/v1/auth/internal/login",
        json={"username": "reset@example.com", "password": "newpassword456"},
    )
    assert relogin.status_code == 200
    assert relogin.json()["user"]["email_verified"] is True


def test_email_change_verified_and_unverified_modes(monkeypatch: Any) -> None:
    sent = _capture_emails(monkeypatch)
    client, _store = _make_app(
        {
            "EMAIL_REGISTRATION_ENABLED": "true",
            "EMAIL_VERIFICATION_REQUIRED": "true",
            "EMAIL_PROVIDER": "smtp",
        }
    )

    # Register verified account
    req = client.post(
        "/v1/auth/email/register/request",
        json={"username": "carol", "email": "old@example.com", "password": "password123"},
    )
    reg_code = _extract_code(sent[-1])
    assert (
        client.post(
            "/v1/auth/email/register/verify",
            json={"registration_id": req.json()["registration_id"], "code": reg_code},
        ).status_code
        == 201
    )

    # Wrong current password rejected
    bad_pw = client.post(
        "/v1/me/email/change/request",
        json={"current_password": "wrongpassword", "new_email": "new@example.com"},
    )
    assert bad_pw.status_code == 400
    assert bad_pw.json()["code"] == "invalid_current_password"

    # Valid email change request sends verification code to new email
    change_req = client.post(
        "/v1/me/email/change/request",
        json={"current_password": "password123", "new_email": "new@example.com"},
    )
    assert change_req.status_code == 200
    assert change_req.json()["verification_required"] is True
    assert sent[-1]["to"] == "new@example.com"
    change_code = _extract_code(sent[-1])

    # Verify updates email, marks verified, and sends best-effort notice to old verified email
    change_done = client.post(
        "/v1/me/email/change/verify",
        json={"change_id": change_req.json()["change_id"], "code": change_code},
    )
    assert change_done.status_code == 200
    assert change_done.json()["user"]["email"] == "new@example.com"
    assert change_done.json()["user"]["email_verified"] is True
    assert any(mail["to"] == "old@example.com" and "变更" in mail["subject"] for mail in sent)

    # When verification switch is turned off, email change completes in 1 step and clears verified flag
    client.app.state.store.upsert_options({"EMAIL_VERIFICATION_REQUIRED": "false"})
    direct_change = client.post(
        "/v1/me/email/change/request",
        json={"current_password": "password123", "new_email": "direct@example.com"},
    )
    assert direct_change.status_code == 200
    assert direct_change.json()["completed"] is True
    assert direct_change.json()["verification_required"] is False
    assert direct_change.json()["user"]["email"] == "direct@example.com"
    assert direct_change.json()["user"]["email_verified"] is False


def test_author_page_api_and_web_404_and_notification_email_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html><body>SPA</body></html>", encoding="utf-8")
    monkeypatch.setattr(main_module, "MARKET_WEB_DIST", dist_dir)

    client, store = _make_app(
        {"EMAIL_REGISTRATION_ENABLED": "true", "EMAIL_VERIFICATION_REQUIRED": "false"}
    )
    reg = client.post(
        "/v1/auth/email/register",
        json={
            "username": "non_gh_author",
            "email": "unverified@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201
    user = store.get_user_by_public_username("non_gh_author")
    assert user is not None

    # Notification email fallback skips unverified email
    assert main_module.notification_email_address(user) == ""
    user["github_email"] = "fallback@github.example"
    assert main_module.notification_email_address(user) == "fallback@github.example"
    user["email_verified_at"] = "2026-11-24T00:00:00Z"
    assert main_module.notification_email_address(user) == "unverified@example.com"
    user["notification_email"] = "explicit@example.com"
    assert main_module.notification_email_address(user) == "explicit@example.com"

    # Author API for non-GitHub user returns empty github_url & oauth_profiles
    author_resp = client.get("/v1/authors/non_gh_author/plugins")
    assert author_resp.status_code == 200
    author_data = author_resp.json()
    assert author_data["username"] == "non_gh_author"
    assert author_data["github_url"] == ""
    assert author_data["oauth_profiles"] == []

    # Frontend route returns 200 for existing author and real 404 for unknown author
    assert client.get("/author/non_gh_author").status_code == 200
    assert client.get("/author/does_not_exist_12345").status_code == 404
    assert client.get("/v1/authors/does_not_exist_12345/plugins").status_code == 404


def test_cross_column_username_disambiguation_and_shared_site_email_limit(
    monkeypatch: Any,
) -> None:
    sent = _capture_emails(monkeypatch)
    client, store = _make_app(
        {
            "EMAIL_REGISTRATION_ENABLED": "true",
            "EMAIL_VERIFICATION_REQUIRED": "true",
            "EMAIL_DAILY_LIMIT": "1",
        }
    )

    # Create email user "alice" first, then GitHub user "alice" -> disambiguated public_username
    store.create_email_user(
        username="alice",
        email="alice@example.com",
        password_hash=hash_password("password123"),
    )
    gh_user = store.upsert_github_user({"id": "555", "login": "alice", "name": "Alice GH"})
    assert gh_user["public_username"] != "alice"
    assert gh_user["public_username"].startswith("alice_")

    # First verification email consumes the shared site daily limit of 1
    req1 = client.post(
        "/v1/auth/email/register/request",
        json={"username": "user_one", "email": "one@example.com", "password": "password123"},
    )
    assert req1.status_code == 202
    assert len(sent) == 1

    # Second email (different user) is blocked by shared site EMAIL_DAILY_LIMIT
    req2 = client.post(
        "/v1/auth/email/register/request",
        json={"username": "user_two", "email": "two@example.com", "password": "password123"},
    )
    assert req2.status_code == 429
    assert req2.json()["code"] == "email_daily_limit_exceeded"


def test_local_file_store_persists_email_accounts_and_oauth_identities(tmp_path: Path) -> None:
    data_path = tmp_path / "market-state.json"
    store1 = LocalFileMarketStore(data_path)
    store1.connect()
    user = store1.create_email_user(
        username="local_email_user",
        email="local@example.com",
        password_hash=hash_password("password123"),
        email_verified_at="2026-11-24T00:00:00Z",
    )
    store1.upsert_oauth_identity(
        user_id=user["id"],
        provider="github",
        instance_url="https://github.com",
        provider_user_id="777",
        provider_login="local_gh",
    )
    store1.set_user_password(user["id"], hash_password("newpass123"), bump_session_version=True)

    store2 = LocalFileMarketStore(data_path)
    store2.connect()
    reloaded = store2.get_user_by_email("LOCAL@EXAMPLE.COM")
    assert reloaded is not None
    assert reloaded["public_username"] == "local_email_user"
    assert reloaded["session_version"] == 2
    identities = store2.list_oauth_identities(reloaded["id"])
    assert len(identities) == 1
    assert identities[0]["provider_login"] == "local_gh"
