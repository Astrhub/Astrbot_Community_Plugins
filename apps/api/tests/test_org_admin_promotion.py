from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
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
            "GITHUB_ADMIN_ORG": "Astrhub",
            "GITHUB_API_TOKEN": "site-token",
            "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }
    )
    app = main_module.create_app(settings=settings, store=InMemoryMarketStore())
    install_github_access_double(app)
    return TestClient(app)


def test_promote_org_admin_falls_back_to_site_token_when_oauth_membership_is_private(monkeypatch):
    async def mock_is_github_org_member(org: str, token: str) -> bool:
        assert org == "Astrhub"
        assert token == "user-oauth-token"
        return False

    class MockAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url: str, headers: dict[str, str]):
            assert url == "https://api.github.com/orgs/Astrhub/members/testuser"
            assert headers["authorization"] == "Bearer site-token"
            return httpx.Response(204)

    monkeypatch.setattr(main_module, "is_github_org_member", mock_is_github_org_member)
    monkeypatch.setattr(main_module.httpx, "AsyncClient", MockAsyncClient)

    client = make_client()
    store = client.app.state.store
    user = store.upsert_github_user({"login": "testuser", "name": "Test User"})
    assert user["role"] == "user"

    request = SimpleNamespace(app=client.app)
    asyncio.run(main_module.promote_org_admin_if_needed(request, user, "user-oauth-token"))

    updated = store.get_user_by_id(user["id"])
    assert updated["role"] == "admin"


def test_promote_org_admin_does_not_promote_non_member_with_site_token(monkeypatch):
    async def mock_is_github_org_member(org: str, token: str) -> bool:
        return False

    class MockAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url: str, headers: dict[str, str]):
            assert headers["authorization"] == "Bearer site-token"
            return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(main_module, "is_github_org_member", mock_is_github_org_member)
    monkeypatch.setattr(main_module.httpx, "AsyncClient", MockAsyncClient)

    client = make_client()
    store = client.app.state.store
    user = store.upsert_github_user({"login": "nonmember", "name": "Non Member"})

    request = SimpleNamespace(app=client.app)
    asyncio.run(main_module.promote_org_admin_if_needed(request, user, "user-oauth-token"))

    updated = store.get_user_by_id(user["id"])
    assert updated["role"] == "user"


def test_me_endpoint_auto_promotes_org_member_and_caches_check(monkeypatch):
    promoted = []
    checked = []

    async def mock_promote_org_admin_if_needed(request, user, access_token=""):
        checked.append(user["id"])
        request.app.state.store.update_user_role(user["id"], "admin")
        promoted.append(user["id"])

    monkeypatch.setattr(
        main_module, "promote_org_admin_if_needed", mock_promote_org_admin_if_needed
    )

    with make_client() as client:
        login = client.get("/v1/auth/debug-login?login=orgmember")
        assert login.status_code == 200
        user_id = login.json()["user"]["id"]

        me1 = client.get("/v1/me")
        assert me1.status_code == 200
        assert me1.json()["role"] == "admin"
        assert promoted == [user_id]

        me2 = client.get("/v1/me")
        assert me2.status_code == 200
        assert me2.json()["role"] == "admin"
        assert checked == [user_id]
