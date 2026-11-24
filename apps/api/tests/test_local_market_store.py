from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import Role, hash_password
from app.config import load_settings
from app.store import LocalFileMarketStore

ROOT = Path(__file__).parents[3]


def test_local_file_store_persists_market_state_and_sessions_across_restarts(tmp_path) -> None:
    data_path = tmp_path / "data" / "market-state.json"
    store = LocalFileMarketStore(data_path, session_ttl_seconds=3600)
    store.connect()

    admin = store.create_internal_admin("owner", hash_password("password123"))
    dev = store.upsert_github_user({"id": "42", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        dev,
        {
            "name": "astrbot_plugin_local_demo",
            "display_name": "Local Demo",
            "desc": "A persisted local plugin",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_local_demo",
            "tags": ["local"],
        },
    )
    store.update_plugin_status(plugin["id"], "listed", admin["id"])
    comment = store.add_comment(plugin["id"], dev["id"], "Works offline too")
    store.like_plugin(plugin["id"], admin["id"])
    store.like_comment(comment["id"], admin["id"])
    store.publish_announcement("Welcome", "Self-hosted market ready", admin["id"])
    store.create_notification_once(
        dev["id"],
        "Listed",
        "Plugin is listed",
        "plugin_review",
        {"plugin_id": plugin["id"]},
        dedupe_key="listed:local_demo",
    )
    api_key = store.issue_api_key("AstrBot", admin["id"], ["market:read"])
    store.upsert_options({"SITE_NAME": "我的插件市场"})
    session = store.create_session(admin["id"])

    loaded_user = store.get_user_by_session(session["token"])
    assert loaded_user is not None
    # Transient request metadata attached to returned user dict must not leak into disk state.
    loaded_user["_repository_authorization"] = {"mode": "site_admin"}
    store.close()

    reloaded = LocalFileMarketStore(data_path, session_ttl_seconds=3600)
    reloaded.connect()

    assert reloaded.get_user_by_internal_username("owner") is not None
    assert reloaded.get_user_by_session(session["token"])["id"] == admin["id"]
    listed = reloaded.list_public_plugins()
    assert len(listed) == 1
    assert listed[0]["id"] == plugin["id"]
    assert listed[0]["likes"] == 1
    assert listed[0]["comments_count"] == 1
    assert len(reloaded.list_comments(plugin["id"])) == 1
    assert reloaded.list_comments(plugin["id"])[0]["likes"] == 1
    assert len(reloaded.list_announcements()) == 1
    assert reloaded.count_notifications(dev["id"]) == 1
    assert reloaded.list_api_keys()[0]["key"] == api_key["key"]
    assert reloaded.list_options()["SITE_NAME"] == "我的插件市场"

    persisted_raw = json.loads(data_path.read_text(encoding="utf-8"))
    assert all("_repository_authorization" not in user for user in persisted_raw["users"])


def test_local_file_store_prunes_expired_sessions(tmp_path) -> None:
    data_path = tmp_path / "market-state.json"
    store = LocalFileMarketStore(data_path, session_ttl_seconds=60)
    store.connect()
    admin = store.create_internal_admin("owner", hash_password("password123"))
    session = store.create_session(admin["id"])
    store.close()

    raw = json.loads(data_path.read_text(encoding="utf-8"))
    expired_time = (datetime.now(UTC) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    raw["sessions"][0]["created_at"] = expired_time
    raw["sessions"][0]["last_seen_at"] = expired_time
    data_path.write_text(json.dumps(raw), encoding="utf-8")

    reloaded = LocalFileMarketStore(data_path, session_ttl_seconds=60)
    reloaded.connect()

    assert reloaded.get_user_by_session(session["token"]) is None
    assert json.loads(data_path.read_text(encoding="utf-8"))["sessions"] == []


def test_local_mode_web_setup_and_restart_persistence(tmp_path) -> None:
    env_file = tmp_path / ".env"
    data_path = tmp_path / "market-state.json"
    settings = load_settings(
        {
            "MARKET_STORAGE_MODE": "local",
            "LOCAL_DATA_PATH": str(data_path),
            "APP_ENV_FILE": str(env_file),
            "ARTIFACTS_ENABLED": "false",
        }
    )

    with TestClient(main_module.create_app(settings=settings)) as client:
        health = client.get("/health").json()
        assert health["storage_mode"] == "local"
        assert health["database"] == "local"
        assert health["redis"] == "local"
        assert health["setup"] == "required"

        site = client.get("/v1/site").json()
        assert site["setup_required"] is True
        assert site["storage_mode"] == "local"

        status = client.get("/v1/setup/status").json()
        assert status["required"] is True
        assert status["storage_mode"] == "local"
        assert status["missing"] == ["core_admin"]
        assert status["local_data_path"] == str(data_path)

        saved = client.post(
            "/v1/setup",
            json={
                "site": {"name": "我的私有市场", "icon_url": "/logo.webp"},
                "admin": {"username": "owner", "password": "password123"},
            },
        )
        assert saved.status_code == 200
        assert saved.json()["activated"] is True
        assert saved.json()["restart_required"] is False

        env_text = env_file.read_text(encoding="utf-8")
        assert "MARKET_STORAGE_MODE=local" in env_text
        assert "CORE_ADMIN_USERNAME=owner" in env_text
        assert "CORE_ADMIN_PASSWORD_HASH=pbkdf2_sha256" in env_text
        assert "DATABASE_URL" not in env_text
        assert "REDIS_URL" not in env_text
        assert client.get("/v1/setup/status").status_code == 404
        assert client.get("/v1/site").json()["setup_required"] is False

        login = client.post(
            "/v1/auth/internal/login",
            json={"username": "owner", "password": "password123"},
        )
        assert login.status_code == 200
        assert login.json()["user"]["role"] == Role.CORE_ADMIN

        admin_status = client.get("/v1/admin/setup/status").json()
        assert admin_status["storage_mode"] == "local"
        assert admin_status["restart_required"] is False

    # Restart with only the persisted JSON file (even without .env) and confirm setup stays closed.
    restarted_settings = load_settings(
        {
            "MARKET_STORAGE_MODE": "local",
            "LOCAL_DATA_PATH": str(data_path),
            "APP_ENV_FILE": str(tmp_path / "fresh.env"),
            "ARTIFACTS_ENABLED": "false",
        }
    )
    with TestClient(main_module.create_app(settings=restarted_settings)) as restarted:
        assert restarted.get("/health").json()["setup"] == "complete"
        assert restarted.get("/v1/site").json()["setup_required"] is False
        assert restarted.get("/v1/site").json()["name"] == "我的私有市场"
        login = restarted.post(
            "/v1/auth/internal/login",
            json={"username": "owner", "password": "password123"},
        )
        assert login.status_code == 200
        assert login.json()["user"]["role"] == Role.CORE_ADMIN


def test_local_mode_env_bootstrap_and_direct_admin_submission(tmp_path, monkeypatch) -> None:
    data_path = tmp_path / "market-state.json"
    env_file = tmp_path / ".env"

    async def fake_fetch_preview(*_args, **_kwargs):
        return {
            "name": "astrbot_plugin_selfhost",
            "display_name": "Selfhost Plugin",
            "desc": "Loaded directly in local personal mode",
            "author": "selfhost",
            "version": "1.2.3",
        }

    async def fake_safe_metadata(*_args, **_kwargs):
        return {"stars": 12, "version": "1.2.3"}

    monkeypatch.setattr(main_module, "fetch_github_plugin_metadata_files", fake_fetch_preview)
    monkeypatch.setattr(main_module, "safe_fetch_plugin_github_metadata", fake_safe_metadata)

    settings = load_settings(
        {
            "MARKET_STORAGE_MODE": "local",
            "LOCAL_DATA_PATH": str(data_path),
            "APP_ENV_FILE": str(env_file),
            "CORE_ADMIN_USERNAME": "selfhost",
            "CORE_ADMIN_PASSWORD": "supersecret123",
            "PLUGIN_AUTO_APPROVE_ENABLED": "true",
            "ARTIFACTS_ENABLED": "false",
        }
    )

    with TestClient(main_module.create_app(settings=settings)) as client:
        assert client.get("/v1/site").json()["setup_required"] is False
        assert "supersecret123" not in data_path.read_text(encoding="utf-8")

        login = client.post(
            "/v1/auth/internal/login",
            json={"username": "selfhost", "password": "supersecret123"},
        )
        assert login.status_code == 200

        preview = client.post(
            "/v1/plugins/submissions/metadata-preview",
            json={"repo": "https://github.com/astrhub/astrbot_plugin_selfhost"},
        )
        assert preview.status_code == 200
        assert preview.json()["name"] == "astrbot_plugin_selfhost"

        created = client.post(
            "/v1/plugins/submissions",
            json={
                "cdn_enabled": False,
                "name": "astrbot_plugin_selfhost",
                "display_name": "Selfhost Plugin",
                "desc": "Loaded directly in local personal mode",
                "author": "selfhost",
                "repo": "https://github.com/astrhub/astrbot_plugin_selfhost",
                "tags": ["personal"],
            },
        )
        assert created.status_code == 201
        assert created.json()["status"] == "listed"

        feed = client.get("/plugins.json")
        assert feed.status_code == 200
        assert "$meta" in feed.json()
        assert "selfhost/astrbot_plugin_selfhost" in feed.json()

        # Regular non-admin internal user still cannot bypass GitHub ownership checks.
        created_user = client.post(
            "/v1/core/users",
            json={"username": "guest", "password": "password123", "role": "user"},
        )
        assert created_user.status_code == 201
        client.cookies.clear()
        assert (
            client.post(
                "/v1/auth/internal/login",
                json={"username": "guest", "password": "password123"},
            ).status_code
            == 200
        )
        blocked = client.post(
            "/v1/plugins/submissions",
            json={
                "cdn_enabled": False,
                "name": "astrbot_plugin_blocked",
                "desc": "Blocked",
                "author": "guest",
                "repo": "https://github.com/astrhub/astrbot_plugin_blocked",
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "oauth_binding_required"

    # Restart and verify listed plugin and AstrBot feed survive.
    with TestClient(main_module.create_app(settings=settings)) as restarted:
        plugins = restarted.get("/v1/plugins").json()["items"]
        assert [item["name"] for item in plugins] == ["astrbot_plugin_selfhost"]
        assert "selfhost/astrbot_plugin_selfhost" in restarted.get("/plugins.json").json()


def test_compose_files_remove_hardcoded_domain_and_provide_local_single_process_edition() -> None:
    compose_raw = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "plugins.eloina.cn" not in compose_raw
    compose = yaml.safe_load(compose_raw)
    assert "VITE_BASE_URL" not in compose["services"]["app"]["build"]["args"]
    assert compose["services"]["app"]["environment"]["WEB_URL"] == (
        "${WEB_URL:-http://127.0.0.1:8787}"
    )

    local_compose = yaml.safe_load((ROOT / "docker-compose-local.yml").read_text(encoding="utf-8"))
    assert list(local_compose["services"].keys()) == ["app"]
    app_service = local_compose["services"]["app"]
    assert "VITE_BASE_URL" not in app_service["build"]["args"]
    assert app_service["environment"]["WEB_URL"] == "${WEB_URL:-http://127.0.0.1:8787}"
    assert app_service["environment"]["MARKET_STORAGE_MODE"] == "local"
    assert app_service["environment"]["ARTIFACTS_ENABLED"] == "false"
    assert app_service["environment"]["LOCAL_DATA_PATH"] == (
        "/var/lib/astrbot-market/data/market-state.json"
    )
    assert app_service["environment"]["APP_ENV_FILE"] == "/var/lib/astrbot-market/data/.env"
    assert "market-local-data:/var/lib/astrbot-market/data" in app_service["volumes"]

    probe_dockerfile = (ROOT / "Dockerfile.runtime-probe").read_text(encoding="utf-8")
    assert "FROM python:3.12.10-slim-bookworm@sha256:" in probe_dockerfile


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is unavailable")
def test_local_compose_renders_with_default_environment() -> None:
    subprocess.run(
        ["docker", "compose", "-f", "docker-compose-local.yml", "config", "--quiet"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
