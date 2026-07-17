from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.artifacts.policy import ReviewPolicyStage
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
    assert set(artifacts["review"]) == {"enabled", "configured", "ready", "degraded"}
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


def test_llm_component_rejects_unsupported_provider_and_unsafe_endpoint() -> None:
    base = {
        "ARTIFACT_LLM_REVIEW_ENABLED": "true",
        "ARTIFACT_LLM_CONFIG_REF": "config:llm-default",
        "ARTIFACT_LLM_MODEL": "category-model-v1",
        "ARTIFACT_LLM_API_KEY": "private-key",
    }
    unsupported = load_settings(
        {
            **base,
            "ARTIFACT_LLM_PROVIDER": "unknown-provider",
            "ARTIFACT_LLM_ENDPOINT_URL": "https://llm.example.test/v1/chat/completions",
        }
    ).artifacts.review.component_configuration()["llm"]
    unsafe_url = load_settings(
        {
            **base,
            "ARTIFACT_LLM_PROVIDER": "openai-compatible",
            "ARTIFACT_LLM_ENDPOINT_URL": "http://metadata.internal/v1/chat/completions",
        }
    ).artifacts.review.component_configuration()["llm"]

    assert unsupported["configured"] is False
    assert "llm_provider_unsupported" in unsupported["reasons"]
    assert unsafe_url["configured"] is False
    assert "llm_endpoint_url_invalid" in unsafe_url["reasons"]


def test_runtime_wires_category_adapter_into_external_worker(tmp_path) -> None:
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path / "artifacts"),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
            "ARTIFACT_LLM_REVIEW_ENABLED": "true",
            "ARTIFACT_LLM_CONFIG_REF": "config:llm-default",
            "ARTIFACT_LLM_PROVIDER": "openai-compatible",
            "ARTIFACT_LLM_MODEL": "category-model-v1",
            "ARTIFACT_LLM_ENDPOINT_URL": "https://llm.example.test/v1/chat/completions",
            "ARTIFACT_LLM_API_KEY": "private-key",
            "DATABASE_URL": "postgresql://example.invalid/market",
        }
    )

    async def scenario() -> tuple[bool, bool]:
        runtime = build_artifact_runtime(settings, InMemoryMarketStore())
        await runtime.start(runtime.store)
        try:
            runner = runtime.job_runner
            assert runner is not None
            stage_wired = "category" in runner._review_stages
            snapshot = runner.review_orchestrator.tool_snapshots.get(ReviewPolicyStage.CATEGORY)
            return stage_wired, bool(snapshot and snapshot.ready)
        finally:
            await runtime.close()

    assert asyncio.run(scenario()) == (True, True)


def test_runtime_keeps_category_handler_when_provider_is_unavailable(tmp_path) -> None:
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path / "artifacts"),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
            "DATABASE_URL": "postgresql://example.invalid/market",
        }
    )

    async def scenario() -> tuple[bool, bool]:
        runtime = build_artifact_runtime(settings, InMemoryMarketStore())
        await runtime.start(runtime.store)
        try:
            runner = runtime.job_runner
            assert runner is not None
            return (
                "category" in runner._handlers,
                ReviewPolicyStage.CATEGORY in runner.review_orchestrator.tool_snapshots,
            )
        finally:
            await runtime.close()

    assert asyncio.run(scenario()) == (True, False)


def test_api_runtime_does_not_load_or_probe_configured_malware_tools(tmp_path) -> None:
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path / "artifacts"),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
            "ARTIFACT_CLAMAV_ENABLED": "true",
            "ARTIFACT_CLAMAV_HOST": "clamav.internal",
            "ARTIFACT_YARA_ENABLED": "true",
            "ARTIFACT_YARA_RULESET_VERSION": "market-v1",
            "ARTIFACT_YARA_RULESET_PATH": str(tmp_path / "must-not-be-read.yar"),
            "ARTIFACT_YARA_RULESET_SOURCE": "core-admin",
            "ARTIFACT_YARA_RULESET_ACTIVATED_AT": "2026-07-16T00:00:00Z",
            "DATABASE_URL": "postgresql://example.invalid/market",
        }
    )

    async def scenario() -> tuple[set[str], set[ReviewPolicyStage]]:
        runtime = build_artifact_runtime(settings, InMemoryMarketStore())
        await runtime.start(runtime.store)
        try:
            runner = runtime.job_runner
            assert runner is not None
            return set(runner._handlers), set(runner.review_orchestrator.tool_snapshots)
        finally:
            await runtime.close()

    handlers, snapshots = asyncio.run(scenario())

    assert {"clamav_scan", "yara_scan"} <= handlers
    assert ReviewPolicyStage.CLAMAV not in snapshots
    assert ReviewPolicyStage.YARA not in snapshots


def test_worker_runtime_loads_audited_yara_snapshot_without_probing_clamd(tmp_path) -> None:
    rules = tmp_path / "market-v1.yar"
    rules.write_text("rule fixture { condition: false }", encoding="utf-8")
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path / "artifacts"),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
            "ARTIFACT_CLAMAV_ENABLED": "true",
            "ARTIFACT_CLAMAV_CONFIG_REF": "config:clamav-test",
            "ARTIFACT_CLAMAV_HOST": "clamav.internal",
            "ARTIFACT_YARA_ENABLED": "true",
            "ARTIFACT_YARA_RULESET_VERSION": "market-v1",
            "ARTIFACT_YARA_RULESET_PATH": str(rules),
            "ARTIFACT_YARA_RULESET_SOURCE": "core-admin",
            "ARTIFACT_YARA_RULESET_ACTIVATED_AT": "2026-07-16T00:00:00Z",
            "DATABASE_URL": "postgresql://example.invalid/market",
        }
    )

    async def scenario() -> dict[ReviewPolicyStage, object]:
        runtime = build_artifact_runtime(
            settings,
            InMemoryMarketStore(),
            worker_execution_enabled=True,
        )
        await runtime.start(runtime.store)
        try:
            runner = runtime.job_runner
            assert runner is not None
            return dict(runner.review_orchestrator.tool_snapshots)
        finally:
            await runtime.close()

    snapshots = asyncio.run(scenario())

    assert snapshots[ReviewPolicyStage.CLAMAV].ready is True
    assert snapshots[ReviewPolicyStage.CLAMAV].version == "clamd-instream-v1"
    assert snapshots[ReviewPolicyStage.YARA].ready is True
    assert snapshots[ReviewPolicyStage.YARA].version.startswith("yara-subprocess-v1:market-v1:")


def test_yara_component_rejects_unversioned_activation_metadata(tmp_path) -> None:
    component = load_settings(
        {
            "ARTIFACT_YARA_ENABLED": "true",
            "ARTIFACT_YARA_RULESET_VERSION": "market-v1",
            "ARTIFACT_YARA_RULESET_PATH": str(tmp_path / "rules.yar"),
            "ARTIFACT_YARA_RULESET_SOURCE": "https://author.invalid/rules",
            "ARTIFACT_YARA_RULESET_ACTIVATED_AT": "yesterday",
        }
    ).artifacts.review.component_configuration()["yara"]

    assert component["configured"] is False
    assert "yara_ruleset_source_invalid" in component["reasons"]
    assert "yara_ruleset_activation_invalid" in component["reasons"]


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
        status = await runtime.review_operations_status()
        await runtime.close()
        return status["health"]

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
                "ARTIFACT_RUNTIME_CONTAINER_IMAGE": f"astrbot-runtime@sha256:{'1' * 64}",
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
        unknown = await runtime.review_operations_status()
        await runtime.repository.upsert_review_worker_heartbeat(
            worker_kind="runtime_runner",
            worker_id="runtime-health-test",
            components={
                "runtime": {
                    "ready": True,
                    "reason": "",
                    "version": "runtime-runner-v1",
                    "data_updated_at": "",
                }
            },
            ttl_seconds=30,
            capacity=1,
            active_count=0,
        )
        ready = await runtime.review_operations_status()
        await runtime.close()
        return unknown["health"], ready["health"]

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
