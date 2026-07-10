from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.artifacts.runtime import build_artifact_runtime
from app.config import load_settings
from app.main import create_app
from app.store import InMemoryMarketStore


def test_artifact_settings_are_disabled_and_redacted_by_default() -> None:
    settings = load_settings({})

    status = settings.artifacts.public_status(settings.database_url)

    assert status == {
        "enabled": False,
        "ready": False,
        "storage_backend": "local",
        "cdn_configured": False,
        "database_configured": False,
        "configuration_errors": [],
        "limits": {
            "max_upload_bytes": 32 * 1024 * 1024,
            "max_unpacked_bytes": 128 * 1024 * 1024,
            "max_file_bytes": 8 * 1024 * 1024,
            "max_files": 2000,
        },
    }


def test_s3_artifact_settings_fail_closed_without_required_values() -> None:
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_STORAGE_BACKEND": "s3",
            "ARTIFACT_S3_SECRET_ACCESS_KEY": "must-not-leak",
            "DATABASE_URL": "postgresql://example.invalid/market",
        }
    )

    status = settings.artifacts.public_status(settings.database_url)

    assert status["ready"] is False
    assert status["configuration_errors"] == [
        "cdn_base_url_missing",
        "s3_endpoint_url_missing",
        "s3_access_key_id_missing",
        "quarantine_bucket_missing",
        "published_bucket_missing",
    ]
    assert "must-not-leak" not in str(status)


def test_artifact_runtime_configures_runner_without_starting_worker_loop() -> None:
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.com",
            "ARTIFACT_LOCAL_ROOT": "/tmp/artifacts",
            "DATABASE_URL": "postgresql://example.invalid/market",
        }
    )
    first_store = object()
    second_store = object()
    runtime = build_artifact_runtime(settings, first_store)

    assert runtime.available is False
    assert runtime.generation == 0

    asyncio.run(runtime.start(first_store))
    runtime.rebind_store(second_store)

    assert runtime.store is second_store
    assert runtime.generation == 2
    assert runtime.started is True
    assert runtime.job_runner is not None
    assert runtime.public_status()["components_configured"] is True
    assert runtime.public_status()["worker_ready"] is False


def test_health_reports_redacted_artifact_readiness() -> None:
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_STORAGE_BACKEND": "s3",
            "ARTIFACT_S3_ENDPOINT_URL": "https://private-storage.example.com",
            "ARTIFACT_S3_ACCESS_KEY_ID": "private-access-key",
            "ARTIFACT_S3_SECRET_ACCESS_KEY": "private-secret-key",
            "ARTIFACT_QUARANTINE_BUCKET": "private-quarantine",
            "ARTIFACT_PUBLISHED_BUCKET": "private-published",
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.com",
            "DATABASE_URL": "postgresql://example.invalid/market",
            "REDIS_URL": "redis://example.invalid/0",
        }
    )
    app = create_app(settings=settings, store=InMemoryMarketStore())

    with TestClient(app) as client:
        payload = client.get("/health").json()

    artifacts = payload["artifacts"]
    assert artifacts["enabled"] is True
    assert artifacts["worker_mode"] == "external"
    assert artifacts["worker_ready"] is False
    assert artifacts["worker_configured"] is True
    assert artifacts["storage_ready"] is True
    assert artifacts["available"] is True
    rendered = str(artifacts)
    assert "private-storage" not in rendered
    assert "private-access-key" not in rendered
    assert "private-secret-key" not in rendered
    assert "private-quarantine" not in rendered
    assert "private-published" not in rendered


def test_artifact_api_fails_closed_without_postgresql_store() -> None:
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.com",
            "DATABASE_URL": "postgresql://configured-but-not-active/market",
            "ENABLE_DEV_AUTH": "true",
        }
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        health = client.get("/health").json()["artifacts"]
        response = client.post(
            "/v1/plugins/registrations",
            headers={"x-dev-github-login": "alice"},
            json={
                "name": "astrbot_plugin_demo",
                "display_name": "Demo",
                "desc": "Demo",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_demo",
            },
        )

    assert health["available"] is False
    assert "postgresql_artifact_store_required" in health["configuration_errors"]
    assert response.status_code == 503
