import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import load_settings
from app.github_authorization import (
    GithubRepositoryAccess,
    authorization_reference,
    oauth_cipher,
    save_authorization,
    verify_publication_access,
)
from app.main import create_app
import app.main as main_module
from app.store import InMemoryMarketStore


def settings(tmp_path):
    return load_settings(
        {
            "APP_ENV_FILE": str(tmp_path / "missing.env"),
            "GITHUB_CLIENT_SECRET": "server-only-oauth-secret",
        }
    )


def repo(**patch):
    return {
        "id": 21,
        "name": "astrbot_plugin_demo",
        "full_name": "my-org/astrbot_plugin_demo",
        "owner": {"id": 10, "login": "my-org", "type": "Organization"},
        "private": False,
        "archived": False,
        "permissions": {"push": True},
        **patch,
    }


async def authorized(tmp_path, handler):
    store = InMemoryMarketStore()
    user = store.upsert_github_user({"id": "1", "login": "alice", "name": "Alice"})
    session = store.create_session(user["id"])
    cfg = settings(tmp_path)
    await save_authorization(
        store, cfg, session["token"], user["id"], "1", "alice", "user-oauth-token"
    )
    reference = authorization_reference(session["token"])
    access = GithubRepositoryAccess(
        store, cfg, user, reference, transport=httpx.MockTransport(handler)
    )
    return store, cfg, user, session, access


def test_repository_list_filters_permissions_and_preserves_pagination(tmp_path):
    def handler(request):
        assert request.headers["authorization"] == "Bearer user-oauth-token"
        if request.url.path.startswith("/user/memberships/"):
            return httpx.Response(
                200, json={"state": "active", "role": "member", "organization": {"id": 10}}
            )
        assert request.url.params["visibility"] == "public"
        return httpx.Response(
            200,
            json=[
                repo(),
                repo(id=22, private=True),
                repo(id=23, archived=True),
                repo(id=24, name="other"),
                repo(id=25, permissions={"pull": True}),
            ],
            headers={"link": '<https://api.github.com/user/repos?page=2>; rel="next"'},
        )

    async def run():
        *_, access = await authorized(tmp_path, handler)
        result = await access.list_repositories(1)
        assert [item["id"] for item in result["items"]] == ["21"]
        assert result["next_page"] == 2
        assert "user-oauth-token" not in json.dumps(result)

    asyncio.run(run())


def test_org_owner_fallback_and_removed_repository_permission(tmp_path):
    role = "admin"

    def handler(request):
        if request.url.path.startswith("/user/memberships/"):
            return httpx.Response(
                200, json={"state": "active", "role": role, "organization": {"id": 10}}
            )
        return httpx.Response(200, json=repo(permissions={"pull": True}))

    async def run():
        nonlocal role
        store, cfg, user, _, access = await authorized(tmp_path, handler)
        assert (await access.verify("https://github.com/my-org/astrbot_plugin_demo"))["id"] == "21"
        role = "member"
        current = GithubRepositoryAccess(
            store, cfg, user, access.reference, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPException) as denied:
            await current.verify("https://github.com/my-org/astrbot_plugin_demo")
        assert denied.value.status_code == 403

    asyncio.run(run())


def test_oauth_is_encrypted_revocable_and_expires_without_site_token_fallback(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={})

    async def run():
        store, cfg, user, session, access = await authorized(tmp_path, handler)
        ciphertext = store.get_github_authorization(access.reference)
        assert "user-oauth-token" not in ciphertext
        with pytest.raises(HTTPException) as error:
            await access.verify("https://github.com/my-org/astrbot_plugin_demo")
        assert error.value.detail["code"] == "github_authorization_required"
        assert store.get_github_authorization(access.reference) is None
        assert len(calls) == 1
        await save_authorization(
            store, cfg, session["token"], user["id"], "1", "alice", "user-oauth-token"
        )
        store.revoke_session(session["token"])
        assert store.get_github_authorization(access.reference) is None
        expired = (
            oauth_cipher(cfg.github_client_secret)
            .encrypt_at_time(
                json.dumps(
                    {"user_id": user["id"], "github_id": "1", "access_token": "expired"}
                ).encode(),
                1,
            )
            .decode()
        )
        store.set_github_authorization(access.reference, expired, 300)
        with pytest.raises(HTTPException):
            await GithubRepositoryAccess(store, cfg, user, access.reference).credential()

    asyncio.run(run())


def test_repository_change_and_cross_origin_redirect_are_not_accepted(tmp_path):
    async def run():
        *_, access = await authorized(
            tmp_path, lambda request: httpx.Response(200, json=repo(id=99))
        )
        with pytest.raises(HTTPException) as changed:
            await access.verify("https://github.com/my-org/astrbot_plugin_demo", "21")
        assert changed.value.status_code == 409
        calls = []

        def redirect(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "https://untrusted.example/repo"})

        *_, access = await authorized(tmp_path, redirect)
        with pytest.raises(HTTPException):
            await access.verify("https://github.com/my-org/astrbot_plugin_demo")
        assert len(calls) == 1

    asyncio.run(run())


def test_repository_endpoint_is_private_and_requires_reconnection_for_old_sessions(tmp_path):
    store = InMemoryMarketStore()
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg, store))
    assert client.get("/v1/me/github/repositories").status_code == 401
    user = store.upsert_github_user({"id": "1", "login": "alice"})
    session = store.create_session(user["id"])
    client.cookies.set(cfg.session_cookie_name, session["token"])
    response = client.get("/v1/me/github/repositories")
    assert response.status_code == 409
    assert response.json()["code"] == "github_authorization_required"
    assert response.headers["cache-control"] == "private, no-store"
    internal = client.post("/v1/internal/artifacts/fake/authorize-publication")
    assert internal.status_code == 403
    assert internal.headers["cache-control"] == "private, no-store"


def test_publication_rechecks_identity_and_can_use_a_new_authorization(tmp_path):
    current_repo = repo()

    async def run():
        def handler(request):
            return httpx.Response(200, json=current_repo)

        store, cfg, user, session, access = await authorized(tmp_path, handler)
        proof = await access.verify("https://github.com/my-org/astrbot_plugin_demo")
        proof["plugin_owner_user_id"] = user["id"]
        store.state["plugins"].append(
            {"id": "plugin-1", "repo": proof["repo"], "owner_user_id": user["id"]}
        )
        artifact = {
            "id": "a1",
            "plugin_id": "plugin-1",
            "submitted_by": user["id"],
            "archive_sha256": "a" * 64,
            "submitted_by_snapshot": {"repository_authorization": proof},
        }

        async def get_artifact(_):
            return deepcopy(artifact)

        state = SimpleNamespace(
            store=store,
            settings=cfg,
            artifact_runtime=SimpleNamespace(repository=SimpleNamespace(get_artifact=get_artifact)),
            github_access_factory=lambda *args: GithubRepositoryAccess(
                *args, transport=httpx.MockTransport(handler)
            ),
        )
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        assert (await verify_publication_access(request, "a1"))["artifact_id"] == "a1"
        store.revoke_session(session["token"])
        with pytest.raises(HTTPException):
            await verify_publication_access(request, "a1")
        new_session = store.create_session(user["id"])
        await save_authorization(
            store, cfg, new_session["token"], user["id"], "1", "alice", "user-oauth-token"
        )
        assert (await verify_publication_access(request, "a1"))["artifact_id"] == "a1"
        current_repo["owner"]["id"] = 90
        with pytest.raises(HTTPException):
            await verify_publication_access(request, "a1")

    asyncio.run(run())


def test_oauth_callback_keeps_credentials_server_side_and_returns_to_submit(tmp_path, monkeypatch):
    cfg = settings(tmp_path).with_updates(
        github_login_enabled=True, github_client_id="client", web_url="https://market.example"
    )
    store = InMemoryMarketStore()

    async def exchange(*_):
        return "callback-user-token"

    async def profile(*_):
        return {"id": 1, "login": "alice"}

    monkeypatch.setattr(main_module, "exchange_github_code", exchange)
    monkeypatch.setattr(main_module, "fetch_github_profile", profile)
    client = TestClient(create_app(cfg, store))
    client.get("/v1/auth/github/login?next=/submit", follow_redirects=False)
    state = client.cookies[cfg.oauth_state_cookie_name]
    response = client.get(
        "/v1/auth/github/callback", params={"code": "code", "state": state}, follow_redirects=False
    )
    assert response.headers["location"] == "https://market.example/submit"
    assert "callback-user-token" not in str(response.headers)
    assert "callback-user-token" not in client.get("/v1/me").text
    reference = authorization_reference(client.cookies[cfg.session_cookie_name])
    assert store.get_github_authorization(reference)
    client.post("/v1/auth/logout")
    assert store.get_github_authorization(reference) is None


def test_submit_rechecks_permission_instead_of_trusting_a_previously_loaded_list(tmp_path):
    permitted = True

    def handler(request):
        item = repo(permissions={"push": permitted})
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[item])
        if request.url.path.startswith("/user/memberships/"):
            return httpx.Response(
                200, json={"state": "active", "role": "member", "organization": {"id": 10}}
            )
        return httpx.Response(200, json=item)

    store, cfg, _, session, _ = asyncio.run(authorized(tmp_path, handler))
    app = create_app(cfg, store)
    app.state.github_access_factory = lambda *args: GithubRepositoryAccess(
        *args, transport=httpx.MockTransport(handler)
    )
    client = TestClient(app)
    client.cookies.set(cfg.session_cookie_name, session["token"])
    assert client.get("/v1/me/github/repositories").json()["items"]
    permitted = False
    response = client.post(
        "/v1/plugins/submissions",
        json={
            "name": "astrbot_plugin_demo",
            "repo": "https://github.com/my-org/astrbot_plugin_demo",
            "repository_id": "21",
            "author": "alice",
            "desc": "Demo",
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == "github_repository_permission_required"


def test_revoked_organization_member_cannot_repoint_existing_listing_to_a_personal_repo(tmp_path):
    def handler(request):
        if request.url.path.startswith("/user/memberships/"):
            return httpx.Response(
                200, json={"state": "active", "role": "member", "organization": {"id": 10}}
            )
        if "/repos/alice/" in request.url.path:
            return httpx.Response(
                200,
                json=repo(
                    id=30,
                    full_name="alice/astrbot_plugin_demo",
                    owner={"id": 1, "login": "alice", "type": "User"},
                ),
            )
        return httpx.Response(200, json=repo(permissions={"pull": True}))

    store, cfg, user, session, _ = asyncio.run(authorized(tmp_path, handler))
    store.state["plugins"].append(
        {
            "id": "astrbot_plugin_demo",
            "name": "astrbot_plugin_demo",
            "repo": "https://github.com/my-org/astrbot_plugin_demo",
            "owner_user_id": user["id"],
            "status": "listed",
        }
    )
    app = create_app(cfg, store)
    app.state.github_access_factory = lambda *args: GithubRepositoryAccess(
        *args, transport=httpx.MockTransport(handler)
    )
    client = TestClient(app)
    client.cookies.set(cfg.session_cookie_name, session["token"])
    response = client.patch(
        "/v1/plugins/astrbot_plugin_demo",
        json={"repo": "https://github.com/alice/astrbot_plugin_demo", "desc": "changed"},
    )
    assert response.status_code == 403
    assert store.get_plugin("astrbot_plugin_demo")["repo"].startswith("https://github.com/my-org/")
