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
from app.git_sources.registry import get_provider_oauth_config
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
