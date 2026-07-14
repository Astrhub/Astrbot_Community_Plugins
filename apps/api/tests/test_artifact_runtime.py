from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.artifacts.policy_service import ReviewPolicyService
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
        "review": {
            "enabled": False,
            "configured": False,
            "ready": False,
            "degraded": False,
            "auto_approve_enabled": False,
            "components": {
                name: {
                    "enabled": False,
                    "configured": False,
                    "ready": False,
                    "degraded": False,
                    "status": "disabled",
                    "reasons": [],
                }
                for name in ("runtime", "llm", "clamav", "yara", "dependency", "policy")
            },
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
            "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
            "ARTIFACT_RUNTIME_REVIEW_ENABLED": "true",
            "ARTIFACT_RUNTIME_CONTAINER_IMAGE": "private.registry/runtime@sha256:secret",
            "ARTIFACT_LLM_REVIEW_ENABLED": "true",
            "ARTIFACT_LLM_PROVIDER": "private-provider",
            "ARTIFACT_LLM_MODEL": "private-model",
            "ARTIFACT_LLM_ENDPOINT_URL": "https://private-llm.example.com",
            "ARTIFACT_LLM_API_KEY": "private-llm-token",
            "ARTIFACT_CLAMAV_ENABLED": "true",
            "ARTIFACT_CLAMAV_HOST": "private-clamav.internal",
            "ARTIFACT_YARA_ENABLED": "true",
            "ARTIFACT_YARA_RULESET_VERSION": "private-rules-v1",
            "ARTIFACT_YARA_RULESET_PATH": "/private/yara/rules.yar",
            "ARTIFACT_DEPENDENCY_REVIEW_ENABLED": "true",
            "ARTIFACT_DEPENDENCY_ADVISORY_URL": "https://private-advisory.example.com",
            "ARTIFACT_DEPENDENCY_API_TOKEN": "private-advisory-token",
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
    assert artifacts["review"]["degraded"] is True
    assert artifacts["review"]["components"]["policy"]["reasons"] == ["active_policy_missing"]
    rendered = str(artifacts)
    assert "private-storage" not in rendered
    assert "private-access-key" not in rendered
    assert "private-secret-key" not in rendered
    assert "private-quarantine" not in rendered
    assert "private-published" not in rendered
    for secret in (
        "private.registry",
        "private-provider",
        "private-model",
        "private-llm",
        "private-clamav",
        "private-rules",
        "/private/yara",
        "private-advisory",
    ):
        assert secret not in rendered


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


def review_policy_payload(*, runtime: bool, auto_approve: bool = False) -> dict:
    stages = ["static"]
    targets = []
    if runtime:
        stages.append("runtime")
        targets.append({"astrbot": "4.26.5", "python": "3.12"})
    return {
        "schema_version": "1",
        "required_stages": stages,
        "runtime_targets": targets,
        "limits": {
            "cpu": 1,
            "memory_mb": 768,
            "pids": 128,
            "timeout_seconds": 120,
        },
        "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
        "llm": {"enabled": False},
        "malware": {"clamav": False},
        "dependency": {"enabled": False},
        "routing": {"auto_approve": auto_approve, "manual_review_at": "low"},
    }


def test_static_only_active_policy_can_be_ready_without_external_tools() -> None:
    async def scenario() -> dict:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.com",
                "ARTIFACT_LOCAL_ROOT": "/tmp/artifacts",
                "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        store = InMemoryMarketStore()
        runtime = build_artifact_runtime(settings, store)
        await runtime.start(store)
        service = ReviewPolicyService(runtime.repository)
        actor = {"id": "core", "role": "core_admin", "username": "core"}
        draft = await service.create_draft(
            version="health-static-v1",
            policy=review_policy_payload(runtime=False),
            actor=actor,
            request_id="health-static-create",
            idempotency_key="health-static-create",
        )
        await service.activate(
            draft["id"],
            actor=actor,
            request_id="health-static-activate",
            idempotency_key="health-static-activate",
            reason="Activate static health policy",
        )
        status = await runtime.health_status()
        await runtime.close()
        return status

    review = asyncio.run(scenario())["review"]

    assert review["configured"] is True
    assert review["ready"] is True
    assert review["degraded"] is False
    assert review["components"]["policy"]["status"] == "ready"
    assert review["auto_approve_enabled"] is False
    assert review["auto_approve_effective"] is False


def test_configured_runtime_stays_degraded_until_real_health_is_reported() -> None:
    async def scenario() -> tuple[dict, dict]:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.com",
                "ARTIFACT_LOCAL_ROOT": "/tmp/artifacts",
                "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
                "ARTIFACT_RUNTIME_REVIEW_ENABLED": "true",
                "ARTIFACT_RUNTIME_CONTAINER_IMAGE": "astrbot-runtime@sha256:1234",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        store = InMemoryMarketStore()
        runtime = build_artifact_runtime(settings, store)
        await runtime.start(store)
        service = ReviewPolicyService(runtime.repository)
        actor = {"id": "core", "role": "core_admin", "username": "core"}
        draft = await service.create_draft(
            version="health-runtime-v1",
            policy=review_policy_payload(runtime=True, auto_approve=True),
            actor=actor,
            request_id="health-runtime-create",
            idempotency_key="health-runtime-create",
        )
        await service.activate(
            draft["id"],
            actor=actor,
            request_id="health-runtime-activate",
            idempotency_key="health-runtime-activate",
            reason="Activate runtime health policy",
        )
        unknown = await runtime.health_status()
        runtime.set_tool_health("runtime", ready=True)
        ready = await runtime.health_status()
        await runtime.close()
        return unknown, ready

    unknown, ready = asyncio.run(scenario())
    unknown_review = unknown["review"]
    ready_review = ready["review"]

    assert unknown_review["components"]["runtime"] == {
        "enabled": True,
        "configured": True,
        "ready": False,
        "degraded": True,
        "status": "degraded",
        "reasons": ["health_unknown"],
    }
    assert unknown_review["ready"] is False
    assert ready_review["components"]["runtime"]["ready"] is True
    assert ready_review["ready"] is True
    assert ready_review["policy_auto_approve_enabled"] is True
    assert ready_review["auto_approve_enabled"] is False
    assert ready_review["auto_approve_effective"] is False
