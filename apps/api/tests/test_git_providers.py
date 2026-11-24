"""Tests for multi-platform Git OAuth providers, mandatory OAUTH_CREDENTIAL_KEY, and git CDN."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main as main_module
from app.artifacts.runner_contract import ExpectedPluginMetadata
from app.cdn import CdnSubscriptions, process_cdn_reviews
from app.config import load_settings
from app.git_sources.credentials import CredentialStore
from app.git_sources.gitea import GiteaAdapter
from app.git_sources.gitlab import GitLabAdapter
from app.git_sources.models import (
    GitSourceError,
    OAuthTokenSet,
    normalize_atomgit_api_base,
    normalize_git_repo_url,
    parse_repository_ref,
)
from app.git_sources.registry import ensure_valid_provider_token, get_provider_oauth_config
from app.git_sources.v5_providers import AtomGitAdapter, GiteeAdapter
from app.github_authorization import save_authorization
from app.store import InMemoryMarketStore, LocalFileMarketStore

TEST_MASTER_KEY = "test-independent-oauth-master-key-32bytes"


def _make_plugin_zip(
    *,
    name: str = "astrbot_plugin_demo",
    version: str = "v1.0.0",
    author: str = "alice",
    desc: str = "Demo plugin",
    repo: str = "https://gitea.com/alice/astrbot_plugin_gt_cdn",
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{name}/metadata.yaml",
            f"name: {name}\ndesc: {desc}\nauthor: {author}\nversion: {version}\nrepo: {repo}\n",
        )
        zf.writestr(f"{name}/main.py", "def register():\n    return True\n")
    return buf.getvalue()


def _base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "APP_ENV_FILE": str(tmp_path / "missing.env"),
        "WEB_URL": "https://market.example.com",
        "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/test",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "OAUTH_CREDENTIAL_KEY": TEST_MASTER_KEY,
        "GITHUB_LOGIN_ENABLED": "true",
        "GITHUB_CLIENT_ID": "gh-cid",
        "GITHUB_CLIENT_SECRET": "gh-secret",
        "GITHUB_CALLBACK_URL": "https://market.example.com/v1/auth/github/callback",
        "GITLAB_LOGIN_ENABLED": "true",
        "GITLAB_CLIENT_ID": "gl-cid",
        "GITLAB_CLIENT_SECRET": "gl-secret",
        "GITLAB_CALLBACK_URL": "https://market.example.com/v1/auth/oauth/gitlab/callback",
        "GITEE_LOGIN_ENABLED": "true",
        "GITEE_CLIENT_ID": "ge-cid",
        "GITEE_CLIENT_SECRET": "ge-secret",
        "GITEE_CALLBACK_URL": "https://market.example.com/v1/auth/oauth/gitee/callback",
        "ATOMGIT_LOGIN_ENABLED": "true",
        "ATOMGIT_CLIENT_ID": "ag-cid",
        "ATOMGIT_CLIENT_SECRET": "ag-secret",
        "ATOMGIT_CALLBACK_URL": "https://market.example.com/v1/auth/oauth/atomgit/callback",
        "GITEA_LOGIN_ENABLED": "true",
        "GITEA_CLIENT_ID": "gt-cid",
        "GITEA_CLIENT_SECRET": "gt-secret",
        "GITEA_CALLBACK_URL": "https://market.example.com/v1/auth/oauth/gitea/callback",
        **extra,
    }


def _adapter_for(provider: str, settings: Any, handler: Any) -> Any:
    cfg = get_provider_oauth_config(settings, provider)
    transport = httpx.MockTransport(handler)
    cls_map = {
        "gitlab": GitLabAdapter,
        "gitee": GiteeAdapter,
        "atomgit": AtomGitAdapter,
        "gitea": GiteaAdapter,
    }
    return cls_map[provider](cfg, transport=transport)


def test_parse_repository_ref_allowlist_and_normalization() -> None:
    assert normalize_git_repo_url("https://www.github.com/Alice/astrbot_plugin_a.git/") == (
        "https://github.com/Alice/astrbot_plugin_a"
    )
    gl = parse_repository_ref("https://gitlab.com/org/team/sub/astrbot_plugin_nested.git")
    assert gl.provider == "gitlab"
    assert gl.owner == "org/team/sub"
    assert gl.repo_name == "astrbot_plugin_nested"
    assert gl.repo_url == "https://gitlab.com/org/team/sub/astrbot_plugin_nested"

    ag = parse_repository_ref("https://gitcode.com/DevTeam/astrbot_plugin_gc.git/")
    assert ag.provider == "atomgit"
    assert ag.repo_url == "https://atomgit.com/DevTeam/astrbot_plugin_gc"
    assert ag.normalized_repo == "https://atomgit.com/devteam/astrbot_plugin_gc"

    ge = parse_repository_ref("https://gitee.com/bob/astrbot_plugin_ge")
    assert ge.provider == "gitee"
    gt = parse_repository_ref("https://gitea.com/carol/astrbot_plugin_gt")
    assert gt.provider == "gitea"

    for invalid in (
        "http://gitlab.com/alice/astrbot_plugin_http",
        "https://bitbucket.org/alice/astrbot_plugin_bb",
        "https://codeberg.org/alice/astrbot_plugin_cb",
        "https://user:pass@gitlab.com/alice/astrbot_plugin_cred",
        "https://gitlab.com:8443/alice/astrbot_plugin_port",
        "https://gitlab.com/alice/astrbot_plugin_q?foo=bar",
        "https://gitlab.com/alice/astrbot_plugin_frag#readme",
        "https://gitee.com/org/sub/astrbot_plugin_too_deep",
        "https://gitlab.com/alice/../astrbot_plugin_traversal",
    ):
        with pytest.raises(GitSourceError):
            parse_repository_ref(invalid)

    assert normalize_atomgit_api_base("https://api.gitcode.com/") == "https://api.gitcode.com"
    assert normalize_atomgit_api_base("") == "https://api.atomgit.com"
    with pytest.raises(GitSourceError):
        normalize_atomgit_api_base("https://evil.example.com")


def test_runner_contract_accepts_allowlisted_providers_and_rejects_unknown_hosts() -> None:
    for url in (
        "https://github.com/alice/astrbot_plugin_gh",
        "https://gitlab.com/group/sub/astrbot_plugin_gl",
        "https://gitee.com/alice/astrbot_plugin_ge",
        "https://atomgit.com/alice/astrbot_plugin_ag",
        "https://gitcode.com/alice/astrbot_plugin_gc",
        "https://gitea.com/alice/astrbot_plugin_gt",
    ):
        meta = ExpectedPluginMetadata(
            name="astrbot_plugin_demo",
            version="1.0.0",
            source_repo=url,
        )
        assert meta.source_repo.startswith("https://")

    with pytest.raises(ValueError):
        ExpectedPluginMetadata(
            name="astrbot_plugin_demo",
            version="1.0.0",
            source_repo="https://selfhosted.example.com/alice/astrbot_plugin_demo",
        )


def test_oauth_credential_key_is_mandatory_for_all_platforms_including_github(
    tmp_path: Path,
) -> None:
    cfg = load_settings(
        {
            **_base_env(tmp_path),
            "OAUTH_CREDENTIAL_KEY": "too-short",
        }
    )
    store = InMemoryMarketStore()
    client = TestClient(main_module.create_app(settings=cfg, store=store), follow_redirects=False)

    gh_login = client.get("/v1/auth/github/login")
    assert gh_login.status_code == 503
    assert gh_login.json()["code"] == "oauth_credential_key_unconfigured"

    gl_login = client.get("/v1/auth/oauth/gitlab/login")
    assert gl_login.status_code == 503
    assert gl_login.json()["code"] == "oauth_credential_key_unconfigured"

    with pytest.raises(Exception) as exc_info:
        asyncio.run(save_authorization(store, cfg, "sess", "u1", "1", "alice", "token"))
    assert getattr(exc_info.value, "status_code", None) == 503
    assert exc_info.value.detail["code"] == "oauth_credential_key_unconfigured"


def test_gitlab_oauth_login_link_conflict_unbind_and_token_rotation(tmp_path: Path) -> None:
    refresh_calls: list[str] = []
    access_calls: list[str] = []

    def gitlab_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            body = request.content.decode()
            if "grant_type=refresh_token" in body:
                refresh_calls.append(body)
                return httpx.Response(
                    200,
                    json={
                        "access_token": "gl-rotated-token",
                        "refresh_token": "gl-refresh-2",
                        "expires_in": 7200,
                        "scope": "read_user read_api",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "gl-initial-token",
                    "refresh_token": "gl-refresh-1",
                    "expires_in": 30,
                    "scope": "read_user read_api",
                },
            )
        if request.url.path == "/api/v4/user":
            return httpx.Response(
                200,
                json={
                    "id": 9001,
                    "username": "gl_alice",
                    "name": "GitLab Alice",
                    "email": "gl@example.com",
                    "avatar_url": "https://gitlab.com/uploads/avatar.png",
                },
            )
        if request.url.path == "/api/v4/projects":
            access_calls.append(request.headers.get("authorization", ""))
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 501,
                        "path": "astrbot_plugin_nested",
                        "path_with_namespace": "team/sub/astrbot_plugin_nested",
                        "visibility": "public",
                        "archived": False,
                        "description": "Nested GL plugin",
                        "namespace": {"id": 88, "kind": "group"},
                        "permissions": {"group_access": {"access_level": 40}},
                    },
                    {
                        "id": 502,
                        "path": "astrbot_plugin_dev_only",
                        "path_with_namespace": "team/astrbot_plugin_dev_only",
                        "visibility": "public",
                        "archived": False,
                        "namespace": {"id": 88, "kind": "group"},
                        "permissions": {"project_access": {"access_level": 30}},
                    },
                ],
            )
        return httpx.Response(404)

    cfg = load_settings(_base_env(tmp_path))
    store = InMemoryMarketStore()
    app = main_module.create_app(settings=cfg, store=store)
    app.state.git_provider_adapter_factory = lambda s, p: _adapter_for(p, s, gitlab_handler)
    client = TestClient(app, follow_redirects=False)

    # 1. Login via GitLab creates oauth user & encrypts credential
    login_res = client.get("/v1/auth/oauth/gitlab/login?next=/submit")
    assert login_res.status_code == 302
    assert "https://gitlab.com/oauth/authorize" in login_res.headers["location"]
    state = client.cookies[f"{cfg.oauth_state_cookie_name}_gitlab"]

    cb_res = client.get(f"/v1/auth/oauth/gitlab/callback?code=gl-code&state={state}")
    assert cb_res.status_code == 302
    assert cb_res.headers["location"] == "https://market.example.com/submit"

    me = client.get("/v1/me").json()
    assert me["public_username"] == "gl_alice"
    assert me["account_tier"] == "developer"
    assert len(me["oauth_identities"]) == 1
    assert me["oauth_identities"][0]["provider"] == "gitlab"
    assert "gl-initial-token" not in json.dumps(store.state["oauthCredentials"])

    # 2. Expiring token (expires_in=30 < skew 120s) auto-rotates before listing repos
    repos_res = client.get("/v1/me/git/gitlab/repositories")
    assert repos_res.status_code == 200
    items = repos_res.json()["items"]
    assert [item["id"] for item in items] == ["501"]
    assert items[0]["repo"] == "https://gitlab.com/team/sub/astrbot_plugin_nested"
    assert len(refresh_calls) == 1
    assert access_calls == ["Bearer gl-rotated-token"]
    updated_identities = store.list_oauth_identities(me["id"])
    assert updated_identities[0]["rotation_status"] == "rotated"
    assert updated_identities[0]["token_refreshed_at"]

    # 3. Conflict when another logged-in user tries to bind the same GitLab account
    other = store.create_email_user(
        username="bob_email",
        email="bob@example.com",
        password_hash=main_module.hash_password("password123"),
    )
    other_session = store.create_session(other["id"])
    other_client = TestClient(app, follow_redirects=False)
    other_client.cookies.set(cfg.session_cookie_name, other_session["token"])
    other_client.get("/v1/auth/oauth/gitlab/login")
    other_state = other_client.cookies[f"{cfg.oauth_state_cookie_name}_gitlab"]
    conflict = other_client.get(f"/v1/auth/oauth/gitlab/callback?code=gl-code&state={other_state}")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "oauth_identity_conflict"

    # 4. Last-auth-method protection on unbind, then unbind deletes encrypted credential
    identity_id = me["oauth_identities"][0]["id"]
    cred_ref = updated_identities[0]["credential_ref"]
    assert store.get_oauth_credential(cred_ref) is not None
    blocked_unbind = client.delete(f"/v1/me/oauth-identities/{identity_id}")
    assert blocked_unbind.status_code == 409

    set_pwd = client.post(
        "/v1/me/password",
        json={"new_password": "new-password-123"},
    )
    assert set_pwd.status_code == 200
    ok_unbind = client.delete(f"/v1/me/oauth-identities/{identity_id}")
    assert ok_unbind.status_code == 200
    assert store.get_oauth_credential(cred_ref) is None


@pytest.mark.parametrize(
    "provider,repo_url,api_base_override",
    [
        ("gitlab", "https://gitlab.com/org/sub/astrbot_plugin_multi", ""),
        ("gitee", "https://gitee.com/alice/astrbot_plugin_multi", ""),
        ("atomgit", "https://gitcode.com/alice/astrbot_plugin_multi", "https://api.gitcode.com"),
        ("gitea", "https://gitea.com/alice/astrbot_plugin_multi", ""),
    ],
)
def test_third_party_submit_preview_readme_feed_and_redirect_guard(
    tmp_path: Path,
    provider: str,
    repo_url: str,
    api_base_override: str,
) -> None:
    meta_yaml = (
        "name: astrbot_plugin_multi\n"
        "display_name: Multi Git Plugin\n"
        "desc: Works across git providers\n"
        "author: alice\n"
        "version: v1.2.0\n"
        "category: utilities\n"
    ).encode("utf-8")
    readme_md = b"# Multi Git Plugin\n\nRendered via backend."
    i18n_en = json.dumps({"metadata": {"display_name": "Multi Git EN"}}).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        raw_path = request.url.raw_path.decode("ascii", errors="ignore").split("?", 1)[0]
        if provider == "gitlab":
            if raw_path.endswith("/repository/files/metadata.yaml/raw"):
                return httpx.Response(200, content=meta_yaml)
            if raw_path.endswith("/repository/files/README.md/raw"):
                return httpx.Response(200, content=readme_md)
            if raw_path.endswith("/repository/files/.astrbot-plugin%2Fi18n%2Fen.json/raw"):
                return httpx.Response(200, content=i18n_en)
            if path.endswith("/repository/tree"):
                if request.url.params.get("path") == ".astrbot-plugin/i18n":
                    return httpx.Response(200, json=[{"type": "blob", "name": "en.json"}])
                return httpx.Response(
                    200,
                    json=[
                        {"type": "blob", "name": "logo.png"},
                        {"type": "blob", "name": "README.md"},
                        {"type": "blob", "name": "CHANGELOG.md"},
                        {"type": "tree", "name": ".astrbot-plugin"},
                    ],
                )
            if "/api/v4/projects/" in path:
                return httpx.Response(
                    200,
                    json={
                        "id": 777,
                        "path": "astrbot_plugin_multi",
                        "path_with_namespace": "org/sub/astrbot_plugin_multi",
                        "visibility": "public",
                        "archived": False,
                        "default_branch": "main",
                        "star_count": 42,
                        "last_activity_at": "2026-11-24T00:00:00Z",
                        "namespace": {"id": 99, "kind": "group"},
                        "permissions": {"project_access": {"access_level": 40}},
                    },
                )
        elif provider in {"gitee", "atomgit"}:
            if path.endswith("/contents/metadata.yaml"):
                return httpx.Response(
                    200,
                    json={"content": base64.b64encode(meta_yaml).decode(), "encoding": "base64"},
                )
            if path.endswith("/contents/README.md"):
                return httpx.Response(
                    200,
                    json={"content": base64.b64encode(readme_md).decode(), "encoding": "base64"},
                )
            if path.endswith("/contents/.astrbot-plugin/i18n/en.json"):
                return httpx.Response(
                    200,
                    json={"content": base64.b64encode(i18n_en).decode(), "encoding": "base64"},
                )
            if path.endswith("/contents/.astrbot-plugin/i18n"):
                return httpx.Response(200, json=[{"type": "file", "name": "en.json"}])
            if path.endswith("/contents"):
                return httpx.Response(
                    200,
                    json=[
                        {"type": "file", "name": "logo.png"},
                        {"type": "file", "name": "README.md"},
                        {"type": "dir", "name": ".astrbot-plugin"},
                    ],
                )
            if path.endswith("/repos/alice/astrbot_plugin_multi"):
                return httpx.Response(
                    200,
                    json={
                        "id": 777,
                        "name": "astrbot_plugin_multi",
                        "full_name": "alice/astrbot_plugin_multi",
                        "private": False,
                        "archived": False,
                        "default_branch": "main",
                        "stargazers_count": 42,
                        "updated_at": "2026-11-24T00:00:00Z",
                        "owner": {"id": 99, "login": "alice", "type": "User"},
                        "permission": {"push": True, "admin": True},
                    },
                )
        elif provider == "gitea":
            if path.endswith("/raw/metadata.yaml"):
                return httpx.Response(200, content=meta_yaml)
            if path.endswith("/raw/README.md"):
                return httpx.Response(200, content=readme_md)
            if path.endswith("/raw/.astrbot-plugin/i18n/en.json"):
                return httpx.Response(200, content=i18n_en)
            if path.endswith("/contents/.astrbot-plugin/i18n"):
                return httpx.Response(200, json=[{"type": "file", "name": "en.json"}])
            if path.endswith("/contents"):
                return httpx.Response(
                    200,
                    json=[
                        {"type": "file", "name": "logo.png"},
                        {"type": "file", "name": "README.md"},
                        {"type": "dir", "name": ".astrbot-plugin"},
                    ],
                )
            if path.endswith("/repos/alice/astrbot_plugin_multi"):
                return httpx.Response(
                    200,
                    json={
                        "id": 777,
                        "name": "astrbot_plugin_multi",
                        "full_name": "alice/astrbot_plugin_multi",
                        "private": False,
                        "archived": False,
                        "default_branch": "main",
                        "stars_count": 42,
                        "updated_at": "2026-11-24T00:00:00Z",
                        "owner": {"id": 99, "login": "alice", "type": "User"},
                        "permissions": {"push": True, "admin": False},
                    },
                )
        return httpx.Response(404)

    extra = {"PLUGIN_AUTO_APPROVE_ENABLED": "true"}
    if api_base_override:
        extra["ATOMGIT_API_BASE_URL"] = api_base_override
    cfg = load_settings(_base_env(tmp_path, **extra))
    store = InMemoryMarketStore()
    app = main_module.create_app(settings=cfg, store=store)
    app.state.git_provider_adapter_factory = lambda s, p: _adapter_for(p, s, handler)

    user = store.create_oauth_user(provider=provider, provider_login="alice")
    cred_store = CredentialStore(store, cfg.oauth_credential_key)
    cred_ref = asyncio.run(
        cred_store.save_token_set(
            user_id=user["id"],
            token_set=OAuthTokenSet(
                provider=provider,
                access_token=f"{provider}-access-token",
                refresh_token=f"{provider}-refresh-token",
                expires_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                provider_user_id="99",
                provider_login="alice",
            ),
        )
    )
    store.upsert_oauth_identity(
        user["id"],
        provider=provider,
        instance_url=parse_repository_ref(repo_url).instance_url,
        provider_user_id="99",
        provider_login="alice",
        credential_ref=cred_ref,
    )
    session = store.create_session(user["id"])
    client = TestClient(app)
    client.cookies.set(cfg.session_cookie_name, session["token"])

    preview = client.post(
        "/v1/plugins/submissions/metadata-preview",
        json={"repo": repo_url, "repository_id": "777", "strict": True},
    )
    assert preview.status_code == 200
    assert preview.json()["name"] == "astrbot_plugin_multi"
    assert preview.json()["repo"] == normalize_git_repo_url(repo_url)

    submitted = client.post(
        "/v1/plugins/submissions",
        json={
            "cdn_enabled": False,
            "repository_id": "777",
            "name": "astrbot_plugin_multi",
            "display_name": "Multi Git Plugin",
            "desc": "Works across git providers",
            "author": "alice",
            "repo": repo_url,
            "category": "utilities",
            "tags": ["multi"],
        },
    )
    assert submitted.status_code == 201
    plugin = submitted.json()
    assert plugin["repo_provider"] == provider
    assert plugin["repo"] == normalize_git_repo_url(repo_url)
    assert plugin["i18n"]["en"]["display_name"] == "Multi Git EN"

    refreshed = client.post(f"/v1/plugins/{plugin['id']}/refresh-github")
    assert refreshed.status_code == 200
    assert refreshed.json()["stars"] == 42

    # Non-CDN feed outputs normalized repo and omits download_url
    feed = client.get("/plugins.json").json()
    entry = feed["alice/astrbot_plugin_multi"]
    assert entry["repo"] == normalize_git_repo_url(repo_url)
    assert "download_url" not in entry

    # Backend README route works for non-GitHub provider
    readme = client.get(f"/v1/plugins/{plugin['id']}/readme")
    assert readme.status_code == 200
    assert "Rendered via backend." in readme.json()["content"]

    # Normalized repo duplicate check (gitcode.com vs atomgit.com)
    if provider == "atomgit":
        nonlocal_meta = (
            "name: astrbot_plugin_other_name\n"
            "desc: Duplicate repo\n"
            "author: alice\n"
            "version: v1.0.0\n"
        ).encode("utf-8")
        meta_yaml = nonlocal_meta
        dup = client.post(
            "/v1/plugins/submissions",
            json={
                "cdn_enabled": False,
                "repository_id": "777",
                "name": "astrbot_plugin_other_name",
                "desc": "Duplicate repo",
                "author": "alice",
                "repo": "https://atomgit.com/alice/astrbot_plugin_multi.git/",
            },
        )
        assert dup.status_code == 409
        assert dup.json()["code"] == "plugin_identity_conflict"

    # Cross-host redirect is blocked
    bad_adapter = _adapter_for(
        provider,
        cfg,
        lambda _req: httpx.Response(302, headers={"location": "https://evil.example/leak"}),
    )
    with pytest.raises(GitSourceError) as redir_err:
        asyncio.run(bad_adapter.fetch_user_profile("tok"))
    assert redir_err.value.code == f"{provider}_redirect_blocked"


def test_third_party_cdn_submission_binding_revoke_and_transfer(tmp_path: Path) -> None:
    commit_sha = "a" * 40
    archive_bytes = _make_plugin_zip(
        name="astrbot_plugin_gt_cdn",
        version="v1.0.0",
        author="alice",
        desc="Gitea CDN plugin",
    )

    def gitea_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/repos/alice/astrbot_plugin_gt_cdn":
            return httpx.Response(
                200,
                json={
                    "id": 303,
                    "name": "astrbot_plugin_gt_cdn",
                    "full_name": "alice/astrbot_plugin_gt_cdn",
                    "private": False,
                    "archived": False,
                    "default_branch": "main",
                    "stars_count": 7,
                    "updated_at": "2026-11-24T00:00:00Z",
                    "owner": {"id": 55, "login": "alice", "type": "User"},
                    "permissions": {"push": True, "admin": True},
                },
            )
        if path.endswith("/raw/metadata.yaml"):
            return httpx.Response(
                200,
                content=b"name: astrbot_plugin_gt_cdn\ndesc: Gitea CDN plugin\nauthor: alice\nversion: v1.0.0\n",
            )
        if path.endswith("/contents"):
            return httpx.Response(200, json=[])
        if path.endswith("/git/commits/main"):
            return httpx.Response(200, json={"sha": commit_sha})
        if path.endswith(f"/archive/{commit_sha}.zip"):
            return httpx.Response(200, content=archive_bytes)
        return httpx.Response(404)

    cfg = load_settings(
        _base_env(
            tmp_path,
            ARTIFACTS_ENABLED="true",
            ARTIFACT_STORAGE_BACKEND="local",
            ARTIFACT_LOCAL_ROOT=str(tmp_path / "artifacts"),
            ARTIFACT_CDN_BASE_URL="https://cdn.example.test",
            ARTIFACT_AUTHORIZATION_TOKEN="worker-secret",
        )
    )
    store = LocalFileMarketStore(tmp_path / "market.json")
    store.connect()
    app = main_module.create_app(settings=cfg, store=store)
    app.state.git_provider_adapter_factory = lambda s, p: _adapter_for(p, s, gitea_handler)

    alice = store.create_oauth_user(provider="gitea", provider_login="alice")
    bob = store.create_oauth_user(provider="gitea", provider_login="bob")
    admin = store.create_internal_admin("core", main_module.hash_password("password123"))

    cred_store = CredentialStore(store, cfg.oauth_credential_key)
    cred_ref = asyncio.run(
        cred_store.save_token_set(
            user_id=alice["id"],
            token_set=OAuthTokenSet(
                provider="gitea",
                access_token="gt-token",
                refresh_token="gt-refresh",
                expires_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                provider_user_id="55",
                provider_login="alice",
            ),
        )
    )
    store.upsert_oauth_identity(
        alice["id"],
        provider="gitea",
        instance_url="https://gitea.com",
        provider_user_id="55",
        provider_login="alice",
        credential_ref=cred_ref,
    )
    alice_session = store.create_session(alice["id"])
    admin_session = store.create_session(admin["id"])

    with TestClient(app) as client:
        client.cookies.set(cfg.session_cookie_name, alice_session["token"])
        sub = client.post(
            "/v1/plugins/submissions",
            json={
                "cdn_enabled": True,
                "repository_id": "303",
                "name": "astrbot_plugin_gt_cdn",
                "desc": "Gitea CDN plugin",
                "author": "alice",
                "repo": "https://gitea.com/alice/astrbot_plugin_gt_cdn",
            },
        )
        assert sub.status_code == 201
        plugin = sub.json()
        assert plugin["cdn_enabled"] is True

        # Process CDN review -> creates source_type='git' artifact pinned to commit_sha
        processed = asyncio.run(
            process_cdn_reviews(
                app,
                repository_loader=lambda _r: None,  # not used for third-party providers
                source_loader=lambda _r: None,
            )
        )
        assert processed == 1
        sub_row = asyncio.run(CdnSubscriptions(store).get(plugin["id"]))
        assert sub_row is not None
        assert sub_row["state"] == "submitted"
        artifact_id = sub_row["artifact_id"]
        assert artifact_id
        artifact = asyncio.run(app.state.artifact_runtime.repository.get_artifact(artifact_id))
        assert artifact["source_type"] == "git"
        assert artifact["source_commit_sha"] == commit_sha

        # Approve artifact and verify publication access succeeds while binding is active
        app.state.artifact_runtime.repository.artifacts[artifact_id]["review_status"] = "approved"
        authz_ok = client.post(
            f"/v1/internal/artifacts/{artifact_id}/authorize-publication",
            headers={"authorization": "Bearer worker-secret"},
        )
        assert authz_ok.status_code == 200
        assert authz_ok.json()["artifact_id"] == artifact_id

        # LocalFileMarketStore persists credentials and bindings across reload
        reloaded = LocalFileMarketStore(tmp_path / "market.json")
        reloaded.connect()
        assert reloaded.get_oauth_credential(cred_ref) is not None
        active_binding = reloaded.get_active_git_repository_binding(
            "https://gitea.com/alice/astrbot_plugin_gt_cdn"
        )
        assert active_binding is not None
        assert active_binding["binding_generation"] == 1

        # Admin lists and transfers binding -> bumps generation & blocks old CDN publication
        admin_client = TestClient(app)
        admin_client.cookies.set(cfg.session_cookie_name, admin_session["token"])
        listed = admin_client.get("/v1/admin/git-bindings")
        assert listed.status_code == 200
        binding_id = listed.json()["items"][0]["id"]

        transferred = admin_client.post(
            f"/v1/admin/git-bindings/{binding_id}/transfer",
            json={"target_user_id": bob["id"], "reason": "Ownership transfer"},
        )
        assert transferred.status_code == 200
        new_binding = transferred.json()["binding"]
        assert new_binding["user_id"] == bob["id"]
        assert new_binding["binding_generation"] == 2

        authz_blocked = client.post(
            f"/v1/internal/artifacts/{artifact_id}/authorize-publication",
            headers={"authorization": "Bearer worker-secret"},
        )
        assert authz_blocked.status_code == 403
        assert authz_blocked.json()["code"] == "git_binding_revoked"

        revoked = admin_client.post(
            f"/v1/admin/git-bindings/{new_binding['id']}/revoke",
            json={"reason": "Security revoke"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["binding"]["revoked_at"] is not None


def test_admin_settings_keep_provider_secrets_and_master_key_out_of_options(
    tmp_path: Path,
) -> None:
    cfg = load_settings(_base_env(tmp_path))
    store = InMemoryMarketStore()
    store.create_internal_admin("core", main_module.hash_password("password123"))
    app = main_module.create_app(settings=cfg, store=store)
    client = TestClient(app)
    client.post("/v1/auth/internal/login", json={"username": "core", "password": "password123"})

    current = client.get("/v1/admin/settings").json()
    assert current["auth"]["oauth_credential_key_configured"] is True
    assert current["gitlab"]["client_secret"] == main_module.MASKED_SECRET
    assert current["atomgit"]["api_base_url"] == "https://api.atomgit.com"

    payload = {
        "site": current["site"],
        "auth": {
            **current["auth"],
            "gitlab_login_enabled": True,
            "atomgit_login_enabled": True,
        },
        "github": current["github"],
        "gitlab": {
            **current["gitlab"],
            "client_id": "new-gl-id",
            "callback_url": "https://market.example.com/v1/auth/oauth/gitlab/callback",
        },
        "gitee": current["gitee"],
        "atomgit": {
            **current["atomgit"],
            "client_id": "new-ag-id",
            "callback_url": "https://market.example.com/v1/auth/oauth/atomgit/callback",
            "api_base_url": "https://api.gitcode.com",
        },
        "gitea": current["gitea"],
        "market": current["market"],
        "email": current["email"],
    }
    saved = client.put("/v1/admin/settings", json=payload)
    assert saved.status_code == 200
    assert saved.json()["settings"]["atomgit"]["api_base_url"] == "https://api.gitcode.com"

    options = store.list_options()
    for secret_key in (
        "OAUTH_CREDENTIAL_KEY",
        "GITHUB_CLIENT_SECRET",
        "GITLAB_CLIENT_SECRET",
        "GITEE_CLIENT_SECRET",
        "ATOMGIT_CLIENT_SECRET",
        "GITEA_CLIENT_SECRET",
    ):
        assert secret_key not in options
    assert options["ATOMGIT_API_BASE_URL"] == "https://api.gitcode.com"


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_overseas_media_proxy_for_gitlab_gitea_and_avatars(tmp_path: Path) -> None:
    def gitlab_handler(request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode("ascii", errors="ignore").split("?", 1)[0]
        if raw_path.endswith("/repository/files/logo.png/raw"):
            return httpx.Response(200, content=_TINY_PNG)
        if raw_path.endswith("/repository/files/docs%2Fshot.png/raw"):
            return httpx.Response(200, content=_TINY_PNG)
        if raw_path.endswith("/repository/files/docs%2Ffake.png/raw"):
            return httpx.Response(200, content=b"<svg><script>alert(1)</script></svg>")
        if "/api/v4/projects/" in raw_path:
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(404)

    def avatar_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://gitlab.com/uploads/alice.png":
            return httpx.Response(200, content=_TINY_PNG)
        return httpx.Response(404)

    cfg = load_settings(_base_env(tmp_path))
    store = InMemoryMarketStore()
    app = main_module.create_app(settings=cfg, store=store)
    app.state.git_provider_adapter_factory = lambda s, p: _adapter_for(p, s, gitlab_handler)
    app.state.media_proxy_transport = httpx.MockTransport(avatar_handler)

    alice = store.create_oauth_user(
        provider="gitlab",
        provider_login="gl_alice",
        avatar_url="https://gitlab.com/uploads/alice.png",
    )
    gitee_user = store.create_oauth_user(
        provider="gitee",
        provider_login="ge_bob",
        avatar_url="https://gitee.com/uploads/bob.png",
    )
    gl_plugin = store.submit_plugin(
        alice,
        {
            "name": "astrbot_plugin_gl_proxy",
            "desc": "GitLab proxy test",
            "author": "gl_alice",
            "repo": "https://gitlab.com/team/astrbot_plugin_gl_proxy",
            "logo": "https://gitlab.com/team/astrbot_plugin_gl_proxy/-/raw/main/logo.png",
        },
    )
    store.update_plugin_status(gl_plugin["id"], "listed", alice["id"])
    ge_plugin = store.submit_plugin(
        gitee_user,
        {
            "name": "astrbot_plugin_ge_direct",
            "desc": "Gitee direct test",
            "author": "ge_bob",
            "repo": "https://gitee.com/ge_bob/astrbot_plugin_ge_direct",
            "logo": "https://gitee.com/ge_bob/astrbot_plugin_ge_direct/raw/master/logo.png",
        },
    )
    store.update_plugin_status(ge_plugin["id"], "listed", gitee_user["id"])

    client = TestClient(app)
    logo_res = client.get(f"/v1/plugins/{gl_plugin['id']}/logo")
    assert logo_res.status_code == 200
    assert logo_res.headers["content-type"] == "image/png"
    assert logo_res.headers["cache-control"] == "public, max-age=3600"
    assert logo_res.headers["x-content-type-options"] == "nosniff"
    etag = logo_res.headers["etag"]
    assert (
        client.get(
            f"/v1/plugins/{gl_plugin['id']}/logo", headers={"if-none-match": etag}
        ).status_code
        == 304
    )

    # Domestic provider (Gitee) is NOT proxied
    assert client.get(f"/v1/plugins/{ge_plugin['id']}/logo").status_code == 404

    # README relative image proxy + strict security checks
    asset_res = client.get(f"/v1/plugins/{gl_plugin['id']}/readme-asset?path=docs/shot.png")
    assert asset_res.status_code == 200
    assert asset_res.headers["content-type"] == "image/png"
    assert asset_res.headers["cache-control"] == "public, max-age=3600"
    assert (
        client.get(f"/v1/plugins/{gl_plugin['id']}/readme-asset?path=../secret.png").status_code
        == 400
    )
    assert (
        client.get(f"/v1/plugins/{gl_plugin['id']}/readme-asset?path=docs/evil.svg").status_code
        == 400
    )
    assert (
        client.get(f"/v1/plugins/{gl_plugin['id']}/readme-asset?path=docs/fake.png").status_code
        == 415
    )

    # Overseas avatar proxy (public author + private /v1/me/avatar)
    avatar_res = client.get("/v1/authors/gl_alice/avatar")
    assert avatar_res.status_code == 200
    assert avatar_res.headers["content-type"] == "image/png"
    assert avatar_res.headers["cache-control"] == "public, max-age=3600"
    assert client.get("/v1/authors/ge_bob/avatar").status_code == 404

    alice_session = store.create_session(alice["id"])
    client.cookies.set(cfg.session_cookie_name, alice_session["token"])
    me_avatar = client.get("/v1/me/avatar")
    assert me_avatar.status_code == 200
    assert me_avatar.headers["cache-control"] == "private, no-store"


def test_cdn_admin_whitelist_bypass_and_fail_closed_checks(tmp_path: Path) -> None:
    commit_sha = "b" * 40
    archive_bytes = _make_plugin_zip(
        name="astrbot_plugin_admin_fast",
        version="v1.0.0",
        author="admin_dev",
        desc="Admin whitelist fast release",
        repo="https://gitea.com/admin_dev/astrbot_plugin_admin_fast",
    )

    def gitea_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/repos/admin_dev/astrbot_plugin_admin_fast":
            return httpx.Response(
                200,
                json={
                    "id": 909,
                    "name": "astrbot_plugin_admin_fast",
                    "full_name": "admin_dev/astrbot_plugin_admin_fast",
                    "private": False,
                    "archived": False,
                    "default_branch": "main",
                    "stars_count": 3,
                    "updated_at": "2026-11-24T00:00:00Z",
                    "owner": {"id": 99, "login": "admin_dev", "type": "User"},
                    "permissions": {"push": True, "admin": True},
                },
            )
        if path.endswith("/raw/metadata.yaml"):
            return httpx.Response(
                200,
                content=(
                    b"name: astrbot_plugin_admin_fast\n"
                    b"desc: Admin whitelist fast release\n"
                    b"author: admin_dev\n"
                    b"version: v1.0.0\n"
                ),
            )
        if path.endswith("/contents"):
            return httpx.Response(200, json=[])
        if path.endswith("/git/commits/main"):
            return httpx.Response(200, json={"sha": commit_sha})
        if path.endswith(f"/archive/{commit_sha}.zip"):
            return httpx.Response(200, content=archive_bytes)
        return httpx.Response(404)

    cfg = load_settings(
        _base_env(
            tmp_path,
            ARTIFACTS_ENABLED="true",
            ARTIFACT_STORAGE_BACKEND="local",
            ARTIFACT_LOCAL_ROOT=str(tmp_path / "artifacts"),
            ARTIFACT_CDN_BASE_URL="https://cdn.example.test",
            ARTIFACT_AUTHORIZATION_TOKEN="worker-secret",
        )
    )
    store = InMemoryMarketStore()
    app = main_module.create_app(settings=cfg, store=store)
    app.state.git_provider_adapter_factory = lambda s, p: _adapter_for(p, s, gitea_handler)

    admin_user = store.create_oauth_user(provider="gitea", provider_login="admin_dev")
    store.update_user_role(admin_user["id"], "admin")
    normal_user = store.create_oauth_user(provider="gitea", provider_login="normal_dev")

    cred_store = CredentialStore(store, cfg.oauth_credential_key)
    for u, uid, login in ((admin_user, "99", "admin_dev"), (normal_user, "100", "normal_dev")):
        cred_ref = asyncio.run(
            cred_store.save_token_set(
                user_id=u["id"],
                token_set=OAuthTokenSet(
                    provider="gitea",
                    access_token=f"tok-{login}",
                    expires_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                    provider_user_id=uid,
                    provider_login=login,
                ),
            )
        )
        store.upsert_oauth_identity(
            u["id"],
            provider="gitea",
            instance_url="https://gitea.com",
            provider_user_id=uid,
            provider_login=login,
            credential_ref=cred_ref,
        )

    admin_session = store.create_session(admin_user["id"])
    normal_session = store.create_session(normal_user["id"])

    with TestClient(app) as client:
        # 1. Normal user cannot request cdn_whitelist
        client.cookies.set(cfg.session_cookie_name, normal_session["token"])
        forbidden = client.post(
            "/v1/plugins/submissions",
            json={
                "cdn_enabled": True,
                "cdn_whitelist": True,
                "repository_id": "909",
                "name": "astrbot_plugin_admin_fast",
                "desc": "Forbidden whitelist",
                "author": "normal_dev",
                "repo": "https://gitea.com/admin_dev/astrbot_plugin_admin_fast",
            },
        )
        assert forbidden.status_code == 403

        # 2. Admin submits with cdn_enabled=True and cdn_whitelist=True
        client.cookies.set(cfg.session_cookie_name, admin_session["token"])
        created = client.post(
            "/v1/plugins/submissions",
            json={
                "cdn_enabled": True,
                "cdn_whitelist": True,
                "repository_id": "909",
                "name": "astrbot_plugin_admin_fast",
                "desc": "Admin whitelist fast release",
                "author": "admin_dev",
                "repo": "https://gitea.com/admin_dev/astrbot_plugin_admin_fast",
            },
        )
        assert created.status_code == 201
        plugin = created.json()
        assert plugin["cdn_enabled"] is True
        assert plugin["cdn_whitelist"] is True

        # 3. Process CDN review -> precheck skips static scan and directly approves & enqueues publish
        processed = asyncio.run(
            process_cdn_reviews(
                app,
                repository_loader=lambda _r: None,
                source_loader=lambda _r: None,
            )
        )
        assert processed == 1
        sub_row = asyncio.run(CdnSubscriptions(store).get(plugin["id"]))
        assert sub_row is not None and sub_row["state"] == "submitted"
        artifact_id = sub_row["artifact_id"]

        # Run single precheck job via artifact runtime job runner
        runner = app.state.artifact_runtime.job_runner
        if runner is None:
            from app.artifacts.jobs import ArtifactJobRunner
            from app.artifacts.archive import ArchivePrechecker
            from app.artifacts.scanner import StaticScanner

            runner = ArtifactJobRunner(
                repository=app.state.artifact_runtime.repository,
                storage=app.state.artifact_runtime.storage,
                prechecker=ArchivePrechecker(app.state.artifact_runtime.service.limits),
                scanner=StaticScanner(),
                cdn_base_url=cfg.artifacts.cdn_base_url,
            )
        handled = asyncio.run(runner.run_once(limit=1))
        assert handled >= 1

        repo = app.state.artifact_runtime.repository
        artifact = asyncio.run(repo.get_artifact(artifact_id))
        assert artifact is not None
        assert artifact["review_status"] == "approved"
        jobs = [job for job in repo.jobs.values() if job["artifact_id"] == artifact_id]
        job_types = {job["type"] for job in jobs}
        assert "publish" in job_types
        assert "static_scan" not in job_types
        decisions = asyncio.run(repo.list_review_decisions(artifact_id))
        assert any(
            d["action"] == "auto_approve" and (d.get("metadata") or {}).get("cdn_whitelist") is True
            for d in decisions
        )

        # 4. Admin can turn off whitelist via PATCH /v1/plugins/{id}/cdn while keeping CDN enabled
        toggled = client.patch(
            f"/v1/plugins/{plugin['id']}/cdn",
            json={"enabled": True, "whitelist": False},
        )
        assert toggled.status_code == 200
        assert toggled.json()["cdn_enabled"] is True
        assert toggled.json()["cdn_whitelist"] is False

        # 5. Core admin cannot enable cdn_whitelist on a non-admin owner's plugin
        core_admin = store.create_oauth_user(provider="gitea", provider_login="core_owner")
        store.update_user_role(core_admin["id"], "core_admin")
        core_session = store.create_session(core_admin["id"])
        normal_plugin = store.submit_plugin(
            normal_user,
            {
                "name": "astrbot_plugin_normal_owned",
                "desc": "Normal user plugin",
                "author": "normal_dev",
                "repo": "https://gitea.com/normal_dev/astrbot_plugin_normal_owned",
                "cdn_enabled": True,
            },
        )
        client.cookies.set(cfg.session_cookie_name, core_session["token"])
        denied_owner = client.patch(
            f"/v1/plugins/{normal_plugin['id']}/cdn",
            json={"enabled": True, "whitelist": True},
        )
        assert denied_owner.status_code == 403
        assert denied_owner.json()["code"] == "cdn_whitelist_admin_owner_required"

        # 6. Fail-closed: if an admin with cdn_whitelist enabled is demoted before precheck runs,
        # the new artifact falls back to normal static scanning instead of bypassing review.
        client.cookies.set(cfg.session_cookie_name, admin_session["token"])
        reenabled = client.patch(
            f"/v1/plugins/{plugin['id']}/cdn",
            json={"enabled": True, "whitelist": True},
        )
        assert reenabled.status_code == 200
        assert reenabled.json()["cdn_whitelist"] is True

        store.update_user_role(admin_user["id"], "user")
        v2_zip = _make_plugin_zip(
            name="astrbot_plugin_admin_fast",
            version="v1.0.1",
            author="admin_dev",
            desc="Admin whitelist demoted fallback",
            repo="https://gitea.com/admin_dev/astrbot_plugin_admin_fast",
        )

        async def _chunks(data: bytes):
            yield data

        from types import SimpleNamespace

        updated_plugin = store.get_plugin(plugin["id"])
        demoted_user = store.get_user_by_id(admin_user["id"])
        assert updated_plugin is not None and demoted_user is not None
        artifact2 = asyncio.run(
            app.state.artifact_runtime.service.submit_git(
                plugin=updated_plugin,
                user=demoted_user,
                source=SimpleNamespace(
                    repo_url=updated_plugin["repo"],
                    requested_ref="main",
                    commit_sha="c" * 40,
                ),
                archive_chunks=_chunks(v2_zip),
            )
        )
        # Drain artifact1's queued publish job + artifact2's precheck job
        handled2 = asyncio.run(runner.run_once(limit=2))
        assert handled2 == 2
        checked2 = asyncio.run(repo.get_artifact(artifact2["id"]))
        assert checked2 is not None
        assert checked2["review_status"] == "scanning"
        jobs2 = [job for job in repo.jobs.values() if job["artifact_id"] == artifact2["id"]]
        job_types2 = {job["type"] for job in jobs2}
        assert "static_scan" in job_types2
        assert "publish" not in job_types2


def test_malformed_port_and_relative_redirects_handling(tmp_path: Path) -> None:
    for bad_url in (
        "https://github.com:abc/org/astrbot_plugin_x",
        "https://gitlab.com:99999/org/astrbot_plugin_x",
    ):
        with pytest.raises(GitSourceError) as exc_info:
            parse_repository_ref(bad_url)
        assert exc_info.value.status_code == 400

    with pytest.raises(GitSourceError) as exc_info:
        normalize_atomgit_api_base("https://api.atomgit.com:bad")
    assert exc_info.value.status_code == 400

    cfg = load_settings(_base_env(tmp_path))

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/projects/start":
            return httpx.Response(302, headers={"Location": "final.json"})
        if request.url.path == "/api/v4/projects/final.json":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v4/projects/evil":
            return httpx.Response(302, headers={"Location": "//evil.example/leak"})
        if request.url.path == "/team/repo/-/archive/start":
            return httpx.Response(302, headers={"Location": "pkg.zip"})
        if request.url.path == "/team/repo/-/archive/pkg.zip":
            return httpx.Response(200, content=b"PK\x03\x04zipdata")
        return httpx.Response(404)

    adapter = _adapter_for("gitlab", cfg, redirect_handler)
    resp = asyncio.run(adapter.safe_request("GET", "https://gitlab.com/api/v4/projects/start"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    with pytest.raises(GitSourceError) as blocked:
        asyncio.run(adapter.safe_request("GET", "https://gitlab.com/api/v4/projects/evil"))
    assert blocked.value.code == "gitlab_redirect_blocked"

    from app.git_sources.models import ResolvedGitArchiveSource

    async def _collect_archive() -> bytes:
        chunks = []
        async for chunk in adapter.stream_archive(
            ResolvedGitArchiveSource(
                provider="gitlab",
                repo_url="https://gitlab.com/team/repo",
                normalized_repo="https://gitlab.com/team/repo",
                full_path="team/repo",
                repo_name="repo",
                requested_ref="main",
                commit_sha="a" * 40,
                archive_url="https://gitlab.com/team/repo/-/archive/start",
            )
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    assert asyncio.run(_collect_archive()) == b"PK\x03\x04zipdata"


def test_credential_key_mismatch_preserves_row_and_returns_503() -> None:
    store = InMemoryMarketStore()
    user = store.create_oauth_user(provider="gitlab", provider_login="gl_key_user")
    good_store = CredentialStore(store, "correct-master-key-at-least-16")
    ref = asyncio.run(
        good_store.save_token_set(
            user_id=user["id"],
            token_set=OAuthTokenSet(
                provider="gitlab",
                access_token="secret-tok",
                refresh_token="secret-rt",
            ),
        )
    )

    bad_store = CredentialStore(store, "wrong-master-key-at-least-16")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            bad_store.load_token_set(
                ref,
                expected_user_id=user["id"],
                expected_provider="gitlab",
            )
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "oauth_credential_decrypt_failed"
    assert store.get_oauth_credential(ref) is not None

    recovered = asyncio.run(
        good_store.load_token_set(
            ref,
            expected_user_id=user["id"],
            expected_provider="gitlab",
        )
    )
    assert recovered is not None
    assert recovered.access_token == "secret-tok"


def test_concurrent_token_refresh_serializes_rotation(tmp_path: Path) -> None:
    cfg = load_settings(_base_env(tmp_path))
    store = InMemoryMarketStore()
    user = store.create_oauth_user(provider="gitlab", provider_login="gl_race")
    cred_store = CredentialStore(store, cfg.oauth_credential_key)
    expiring_at = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
    ref = asyncio.run(
        cred_store.save_token_set(
            user_id=user["id"],
            token_set=OAuthTokenSet(
                provider="gitlab",
                access_token="old-access",
                refresh_token="single-use-refresh-1",
                expires_at=expiring_at,
            ),
        )
    )
    store.upsert_oauth_identity(
        user["id"],
        provider="gitlab",
        instance_url="https://gitlab.com",
        provider_user_id="777",
        provider_login="gl_race",
        credential_ref=ref,
        expires_at=expiring_at,
    )

    refresh_calls = 0

    def token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        if request.url.path == "/oauth/token":
            refresh_calls += 1
            body = request.content.decode("utf-8", errors="ignore")
            if refresh_calls == 1 and "single-use-refresh-1" in body:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "rotated-access-2",
                        "refresh_token": "single-use-refresh-2",
                        "expires_in": 7200,
                    },
                )
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(404)

    adapter = _adapter_for("gitlab", cfg, token_handler)

    async def _run_concurrent() -> tuple[tuple[dict, OAuthTokenSet], tuple[dict, OAuthTokenSet]]:
        return await asyncio.gather(
            ensure_valid_provider_token(store, cfg, user, "gitlab", adapter=adapter),
            ensure_valid_provider_token(store, cfg, user, "gitlab", adapter=adapter),
        )

    (_, tok1), (_, tok2) = asyncio.run(_run_concurrent())
    assert refresh_calls == 1
    assert tok1.access_token == "rotated-access-2"
    assert tok2.access_token == "rotated-access-2"


def test_user_merge_rebinds_credentials_and_atomic_revoke(tmp_path: Path) -> None:
    cfg = load_settings(_base_env(tmp_path))
    store = InMemoryMarketStore()
    source_user = store.create_oauth_user(provider="gitlab", provider_login="gl_merge_src")
    target_admin = store.create_oauth_user(provider="gitea", provider_login="gt_merge_dst")
    store.update_user_role(target_admin["id"], "admin")

    cred_store = CredentialStore(store, cfg.oauth_credential_key)
    ref = asyncio.run(
        cred_store.save_token_set(
            user_id=source_user["id"],
            token_set=OAuthTokenSet(
                provider="gitlab",
                access_token="merge-tok",
                refresh_token="merge-rt",
            ),
        )
    )
    identity = store.upsert_oauth_identity(
        source_user["id"],
        provider="gitlab",
        instance_url="https://gitlab.com",
        provider_user_id="888",
        provider_login="gl_merge_src",
        credential_ref=ref,
    )

    app = main_module.create_app(settings=cfg, store=store)
    from types import SimpleNamespace

    asyncio.run(
        main_module.transfer_user_owned_records(
            SimpleNamespace(app=app),  # type: ignore[arg-type]
            source_user["id"],
            target_admin["id"],
        )
    )
    loaded = asyncio.run(
        cred_store.load_token_set(
            ref,
            expected_user_id=target_admin["id"],
            expected_provider="gitlab",
        )
    )
    assert loaded is not None
    assert loaded.access_token == "merge-tok"

    revoked = store.revoke_oauth_identity(target_admin["id"], identity["id"])
    assert revoked is not None
    assert store.get_oauth_credential(ref) is None


def test_overlapping_oauth_callbacks_clean_up_orphans(tmp_path: Path) -> None:
    cfg = load_settings(_base_env(tmp_path))
    store = InMemoryMarketStore()

    def gitea_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={"access_token": "gt-tok", "refresh_token": "gt-rt", "expires_in": 3600},
            )
        if request.url.path == "/api/v1/user":
            return httpx.Response(
                200,
                json={"id": 4242, "login": "race_user", "full_name": "Race User", "email": ""},
            )
        return httpx.Response(404)

    app = main_module.create_app(settings=cfg, store=store)
    app.state.git_provider_adapter_factory = lambda s, p: _adapter_for(p, s, gitea_handler)

    winner = store.create_oauth_user(provider="gitea", provider_login="race_user")
    orig_get = store.get_oauth_identity_by_provider_user
    orig_upsert = store.upsert_oauth_identity
    calls = {"get": 0}

    def racing_get(provider: str, instance_url: str, provider_user_id: str):
        calls["get"] += 1
        if calls["get"] == 1:
            return None
        return orig_get(provider, instance_url, provider_user_id)

    def racing_upsert(user_id: str, **kwargs):
        orig_upsert(winner["id"], **kwargs)
        raise RuntimeError("duplicate provider identity")

    store.get_oauth_identity_by_provider_user = racing_get  # type: ignore[method-assign]
    store.upsert_oauth_identity = racing_upsert  # type: ignore[method-assign]

    client = TestClient(app, follow_redirects=False)
    client.cookies.set("astrbot_market_oauth_state_gitea", "state-race")
    res = client.get("/v1/auth/oauth/gitea/callback?code=abc&state=state-race")
    assert res.status_code == 302
    assert len(store.state["users"]) == 1
    assert store.state["users"][0]["id"] == winner["id"]
    assert store.state["oauthCredentials"] == {}


def test_mixed_case_repo_publication_and_repo_change_clears_stale_id(tmp_path: Path) -> None:
    commit_sha = "d" * 40
    archive_bytes = _make_plugin_zip(
        name="astrbot_plugin_mixed",
        version="v1.0.0",
        author="MyTeam",
        desc="Mixed case repo test",
        repo="https://gitea.com/MyTeam/astrbot_plugin_Mixed",
    )

    def gitea_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/repos/MyTeam/astrbot_plugin_Mixed":
            return httpx.Response(
                200,
                json={
                    "id": 5150,
                    "name": "astrbot_plugin_Mixed",
                    "full_name": "MyTeam/astrbot_plugin_Mixed",
                    "private": False,
                    "archived": False,
                    "default_branch": "main",
                    "stars_count": 2,
                    "updated_at": "2026-11-24T00:00:00Z",
                    "owner": {"id": 55, "login": "MyTeam", "type": "User"},
                    "permissions": {"push": True, "admin": True},
                },
            )
        if path.endswith("/raw/metadata.yaml"):
            return httpx.Response(
                200,
                content=(
                    b"name: astrbot_plugin_mixed\n"
                    b"desc: Mixed case repo test\n"
                    b"author: MyTeam\n"
                    b"version: v1.0.0\n"
                ),
            )
        if path.endswith("/contents"):
            return httpx.Response(200, json=[])
        if path.endswith("/git/commits/main"):
            return httpx.Response(200, json={"sha": commit_sha})
        if path.endswith(f"/archive/{commit_sha}.zip"):
            return httpx.Response(200, content=archive_bytes)
        return httpx.Response(404)

    cfg = load_settings(
        _base_env(
            tmp_path,
            ARTIFACTS_ENABLED="true",
            ARTIFACT_STORAGE_BACKEND="local",
            ARTIFACT_LOCAL_ROOT=str(tmp_path / "artifacts"),
            ARTIFACT_CDN_BASE_URL="https://cdn.example.test",
            ARTIFACT_AUTHORIZATION_TOKEN="worker-secret",
        )
    )
    store = InMemoryMarketStore()
    app = main_module.create_app(settings=cfg, store=store)
    app.state.git_provider_adapter_factory = lambda s, p: _adapter_for(p, s, gitea_handler)

    dev = store.create_oauth_user(provider="gitea", provider_login="MyTeam")
    store.update_user_role(dev["id"], "admin")
    cred_store = CredentialStore(store, cfg.oauth_credential_key)
    cred_ref = asyncio.run(
        cred_store.save_token_set(
            user_id=dev["id"],
            token_set=OAuthTokenSet(
                provider="gitea",
                access_token="tok-mixed",
                expires_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                provider_user_id="55",
                provider_login="MyTeam",
            ),
        )
    )
    store.upsert_oauth_identity(
        dev["id"],
        provider="gitea",
        instance_url="https://gitea.com",
        provider_user_id="55",
        provider_login="MyTeam",
        credential_ref=cred_ref,
    )
    session = store.create_session(dev["id"])

    with TestClient(app) as client:
        client.cookies.set(cfg.session_cookie_name, session["token"])
        created = client.post(
            "/v1/plugins/submissions",
            json={
                "cdn_enabled": True,
                "cdn_whitelist": True,
                "repository_id": "5150",
                "name": "astrbot_plugin_mixed",
                "desc": "Mixed case repo test",
                "author": "MyTeam",
                "repo": "https://gitea.com/MyTeam/astrbot_plugin_Mixed",
            },
        )
        assert created.status_code == 201
        plugin = created.json()
        # Original casing is preserved for display and AstrBot Feed
        assert plugin["repo"] == "https://gitea.com/MyTeam/astrbot_plugin_Mixed"
        assert plugin["repo_external_id"] == "5150"

        processed = asyncio.run(
            process_cdn_reviews(
                app,
                repository_loader=lambda _r: None,
                source_loader=lambda _r: None,
            )
        )
        assert processed == 1
        sub_row = asyncio.run(CdnSubscriptions(store).get(plugin["id"]))
        assert sub_row is not None and sub_row["artifact_id"]
        artifact_id = sub_row["artifact_id"]

        runner = app.state.artifact_runtime.job_runner
        if runner is None:
            from app.artifacts.archive import ArchivePrechecker
            from app.artifacts.jobs import ArtifactJobRunner
            from app.artifacts.scanner import StaticScanner

            runner = ArtifactJobRunner(
                repository=app.state.artifact_runtime.repository,
                storage=app.state.artifact_runtime.storage,
                prechecker=ArchivePrechecker(app.state.artifact_runtime.service.limits),
                scanner=StaticScanner(),
                cdn_base_url=cfg.artifacts.cdn_base_url,
            )
        asyncio.run(runner.run_once(limit=1))

        # Publication access revalidation must accept mixed-case plugin['repo'] vs lowercased source_repo
        from app.github_authorization import verify_publication_access
        from types import SimpleNamespace

        fake_req = SimpleNamespace(app=app)
        verified = asyncio.run(verify_publication_access(fake_req, artifact_id))  # type: ignore[arg-type]
        assert verified["artifact_id"] == artifact_id
        assert verified["repo"] == "https://gitea.com/MyTeam/astrbot_plugin_Mixed"

        # Changing repo without passing repo_external_id must clear stale ID and update provider
        updated = store.update_plugin_metadata(
            plugin["id"],
            {"repo": "https://gitlab.com/OtherOrg/astrbot_plugin_Mixed"},
        )
        assert updated is not None
        assert updated["repo"] == "https://gitlab.com/OtherOrg/astrbot_plugin_Mixed"
        assert updated["repo_provider"] == "gitlab"
        assert updated["repo_external_id"] == ""

    migration_sql = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "migrations"
        / "20261124_012_git_providers_and_oauth_credentials.sql"
    ).read_text(encoding="utf-8")
    assert "ROW_NUMBER() OVER" in migration_sql
    assert migration_sql.index("ROW_NUMBER() OVER") < migration_sql.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS market_plugins_normalized_repo_key"
    )
