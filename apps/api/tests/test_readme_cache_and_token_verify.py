from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

import httpx
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import Role
from app.config import load_settings
from app.store import InMemoryMarketStore


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []
        self.expirations: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.expirations[key] = ex

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return int(self.values.pop(key, None) is not None)


def response(url: str, status_code: int, **kwargs: Any) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("GET", url),
        **kwargs,
    )


def fake_async_client(
    handler: Callable[[str, dict[str, str]], httpx.Response],
) -> type:
    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            return handler(url, headers or {})

    return FakeAsyncClient


def make_store(*, tokens: str = "") -> tuple[InMemoryMarketStore, dict[str, Any]]:
    store = InMemoryMarketStore({"options": {"GITHUB_API_TOKEN": tokens}} if tokens else None)
    owner = store.upsert_github_user({"id": "owner-1", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        owner,
        {
            "name": "astrbot_plugin_readme_cache",
            "display_name": "README Cache",
            "desc": "README cache fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_readme_cache",
            "tags": [],
        },
    )
    return store, plugin


def make_client(store: InMemoryMarketStore) -> TestClient:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "GITHUB_METADATA_SYNC_ENABLED": "true",
        }
    )
    return TestClient(main_module.create_app(settings=settings, store=store))


def test_plugin_readme_uses_redis_cache_and_refreshes(monkeypatch) -> None:
    store, plugin = make_store(tokens="token-a")
    redis = FakeRedis()
    store.redis = redis
    calls: list[tuple[str, str]] = []
    content = "# Cached README\n"

    def handler(url: str, headers: dict[str, str]) -> httpx.Response:
        calls.append((url, headers.get("authorization", "")))
        assert url.endswith("/readme")
        return response(
            url,
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
                "download_url": (
                    "https://raw.githubusercontent.com/alice/"
                    "astrbot_plugin_readme_cache/main/README.md"
                ),
            },
        )

    monkeypatch.setattr(main_module.httpx, "AsyncClient", fake_async_client(handler))

    with make_client(store) as client:
        first = client.get(f"/v1/plugins/{plugin['id']}/readme")
        second = client.get(f"/v1/plugins/{plugin['id']}/readme")
        refreshed = client.post(f"/v1/plugins/{plugin['id']}/readme/refresh")

    assert first.status_code == 200
    assert first.json() == {
        "content": content,
        "source_url": (
            "https://raw.githubusercontent.com/alice/astrbot_plugin_readme_cache/main/README.md"
        ),
        "fetched_at": first.json()["fetched_at"],
        "cached": False,
    }
    assert second.json()["cached"] is True
    assert refreshed.json()["cached"] is False
    assert len(calls) == 2
    assert calls == [(calls[0][0], "Bearer token-a"), (calls[1][0], "Bearer token-a")]
    assert redis.deleted
    assert set(redis.expirations.values()) == {main_module.README_CACHE_TTL_SECONDS}


def test_plugin_readme_missing_path_returns_404(monkeypatch) -> None:
    store, plugin = make_store()

    def handler(url: str, headers: dict[str, str]) -> httpx.Response:
        if url == "https://api.github.com/repos/alice/astrbot_plugin_readme_cache":
            return response(url, 200, json={"default_branch": "develop"})
        return response(url, 404, text="not found")

    monkeypatch.setattr(main_module.httpx, "AsyncClient", fake_async_client(handler))

    with make_client(store) as client:
        missing = client.get(
            f"/v1/plugins/{plugin['id']}/readme",
            params={"path": "docs/missing.md"},
        )
        unsafe = client.get(
            f"/v1/plugins/{plugin['id']}/readme",
            params={"path": "../secret.md"},
        )

    assert missing.status_code == 404
    assert unsafe.status_code == 400


def test_plugin_readme_raw_fallback_does_not_forward_token(monkeypatch) -> None:
    store, plugin = make_store(tokens="token-a")
    requests: list[tuple[str, str]] = []

    def handler(url: str, headers: dict[str, str]) -> httpx.Response:
        requests.append((url, headers.get("authorization", "")))
        if url.endswith("/readme"):
            return response(url, 404, text="not found")
        if url == "https://api.github.com/repos/alice/astrbot_plugin_readme_cache":
            return response(url, 200, json={"default_branch": "develop"})
        if url.endswith("/develop/README.md"):
            return response(url, 200, text="# Raw fallback\n")
        return response(url, 404, text="not found")

    monkeypatch.setattr(main_module.httpx, "AsyncClient", fake_async_client(handler))

    with make_client(store) as client:
        result = client.get(f"/v1/plugins/{plugin['id']}/readme")

    assert result.status_code == 200
    assert result.json()["content"] == "# Raw fallback\n"
    assert requests[0][1] == "Bearer token-a"
    assert requests[1][1] == "Bearer token-a"
    assert requests[2][0].startswith("https://raw.githubusercontent.com/")
    assert requests[2][1] == ""


def test_github_token_verify_requires_core_admin_and_valid_index(monkeypatch) -> None:
    store, _ = make_store(tokens="token-a")
    normal = store.upsert_github_user({"id": "user-1", "login": "bob", "name": "Bob"})
    core = store.upsert_github_user({"id": "core-1", "login": "root", "name": "Root"})
    store.update_user_role(core["id"], Role.CORE_ADMIN.value)

    def handler(url: str, headers: dict[str, str]) -> httpx.Response:
        raise AssertionError("GitHub must not be called for rejected requests")

    monkeypatch.setattr(main_module.httpx, "AsyncClient", fake_async_client(handler))

    with make_client(store) as client:
        unauthenticated = client.post(
            "/v1/admin/settings/github-tokens/verify",
            json={"index": 0},
        )
        forbidden = client.post(
            "/v1/admin/settings/github-tokens/verify",
            headers={"x-dev-github-login": normal["github_login"]},
            json={"index": 0},
        )
        invalid_index = client.post(
            "/v1/admin/settings/github-tokens/verify",
            headers={"x-dev-github-login": core["github_login"]},
            json={"index": 1},
        )

    assert unauthenticated.status_code == 401
    assert forbidden.status_code == 403
    assert invalid_index.status_code == 400


def test_github_token_verify_updates_active_and_disabled_statuses(monkeypatch) -> None:
    store, _ = make_store(tokens="token-a\ntoken-b")
    core = store.upsert_github_user({"id": "core-1", "login": "root", "name": "Root"})
    store.update_user_role(core["id"], Role.CORE_ADMIN.value)
    seen_tokens: list[str] = []

    def handler(url: str, headers: dict[str, str]) -> httpx.Response:
        assert url == "https://api.github.com/rate_limit"
        authorization = headers.get("authorization", "")
        seen_tokens.append(authorization)
        if authorization == "Bearer token-a":
            return response(url, 200, json={"rate": {"remaining": 5000}})
        return response(url, 401, json={"message": "Bad credentials"})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", fake_async_client(handler))
    headers = {"x-dev-github-login": core["github_login"]}

    with make_client(store) as client:
        active = client.post(
            "/v1/admin/settings/github-tokens/verify",
            headers=headers,
            json={"index": 0},
        )
        disabled = client.post(
            "/v1/admin/settings/github-tokens/verify",
            headers=headers,
            json={"index": 1},
        )

    assert active.status_code == 200
    assert active.json()["api_token_statuses"][0]["status"] == "active"
    assert active.json()["api_token_statuses"][0]["checked_at"]
    assert disabled.status_code == 200
    statuses = disabled.json()["api_token_statuses"]
    assert statuses[0]["status"] == "active"
    assert statuses[1]["status"] == "disabled"
    assert statuses[1]["disabled"] is True
    assert statuses[1]["error_code"] == 401
    assert seen_tokens == ["Bearer token-a", "Bearer token-b"]
