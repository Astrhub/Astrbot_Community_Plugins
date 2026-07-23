from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.artifacts.observability import normalize_worker_heartbeat
from app.artifacts.policy import parse_review_policy
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.runtime import build_artifact_runtime
from app.config import load_settings
from app.store import InMemoryMarketStore


def runtime_policy() -> dict:
    return {
        "schema_version": "1",
        "required_stages": ["static", "runtime"],
        "runtime_targets": [{"astrbot": "4.26.5", "python": "3.12"}],
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
        "routing": {"auto_approve": False, "manual_review_at": "low"},
    }


def test_worker_heartbeat_contract_rejects_unbounded_or_sensitive_fields() -> None:
    valid = normalize_worker_heartbeat(
        worker_kind="runtime_runner",
        worker_id="runtime-1",
        components={
            "runtime": {
                "ready": True,
                "reason": "",
                "version": "runtime-runner-v1",
                "data_updated_at": "",
            }
        },
        ttl_seconds=30,
        capacity=2,
        active_count=1,
    )
    assert valid["worker_id"] == "runtime-1"

    with pytest.raises(ValueError, match="review_worker_heartbeat_invalid"):
        normalize_worker_heartbeat(
            worker_kind="runtime_runner",
            worker_id="runtime-1",
            components={
                "runtime": {
                    "ready": True,
                    "reason": "",
                    "version": "runtime-runner-v1",
                    "data_updated_at": "",
                    "endpoint": "https://private.example.test",
                }
            },
            ttl_seconds=30,
            capacity=1,
            active_count=0,
        )


def test_in_memory_heartbeats_use_server_time_and_expire_fail_visible() -> None:
    repository = InMemoryArtifactRepository()

    async def scenario() -> tuple[dict, dict]:
        stored = await repository.upsert_review_worker_heartbeat(
            worker_kind="runtime_runner",
            worker_id="runtime-1",
            components={
                "runtime": {
                    "ready": True,
                    "reason": "",
                    "version": "runtime-runner-v1",
                    "data_updated_at": "",
                }
            },
            ttl_seconds=30,
            capacity=2,
            active_count=1,
        )
        live = (await repository.list_review_worker_heartbeats())[0]
        repository.worker_heartbeats[("runtime_runner", "runtime-1")]["expires_at"] = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        stale = (await repository.list_review_worker_heartbeats())[0]
        return {**stored, "live": live["live"]}, stale

    live, stale = asyncio.run(scenario())

    assert live["live"] is True
    assert live["observed_at"] < live["expires_at"]
    assert stale["live"] is False


def test_runtime_readiness_uses_shared_live_heartbeat_not_api_process_state() -> None:
    async def scenario() -> tuple[list[dict[str, str]], list[dict[str, str]], dict]:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
                "ARTIFACT_LOCAL_ROOT": "/tmp/review-observability",
                "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
                "ARTIFACT_RUNTIME_REVIEW_ENABLED": "true",
                "ARTIFACT_RUNTIME_CONTAINER_IMAGE": f"runtime@sha256:{'1' * 64}",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        runtime = build_artifact_runtime(settings, InMemoryMarketStore())
        await runtime.start(runtime.store)
        policy = parse_review_policy(runtime_policy())
        missing = await runtime.review_policy_readiness_issues(policy)
        await runtime.repository.upsert_review_worker_heartbeat(
            worker_kind="runtime_runner",
            worker_id="runtime-1",
            components={
                "runtime": {
                    "ready": True,
                    "reason": "",
                    "version": "runtime-runner-v1",
                    "data_updated_at": "",
                }
            },
            ttl_seconds=30,
            capacity=2,
            active_count=0,
        )
        ready = await runtime.review_policy_readiness_issues(policy)
        runtime.repository.worker_heartbeats[("runtime_runner", "runtime-1")]["expires_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        )
        operations = await runtime.review_operations_status()
        await runtime.close()
        return missing, ready, operations

    missing, ready, operations = asyncio.run(scenario())

    assert [(item["path"], item["code"]) for item in missing] == [
        ("tools.runtime", "health_unknown")
    ]
    assert ready == []
    runtime_worker = next(
        item for item in operations["health"]["workers"] if item["kind"] == "runtime_runner"
    )
    assert runtime_worker["reasons"] == ["runtime_runner_heartbeat_stale"]


def test_metrics_projection_has_only_fixed_low_cardinality_dimensions() -> None:
    async def scenario() -> dict:
        now = datetime.now(UTC)
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
                "ARTIFACT_LOCAL_ROOT": "/tmp/review-metrics",
                "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        runtime = build_artifact_runtime(settings, InMemoryMarketStore())
        await runtime.start(runtime.store)
        repository = runtime.repository
        repository.jobs["job-safe"] = {
            "id": "job-safe",
            "type": "static_scan",
            "status": "queued",
            "created_at": now,
        }
        repository.jobs["job-high-cardinality"] = {
            "id": "job-high-cardinality",
            "type": "plugin-alice-private",
            "status": "queued",
            "created_at": now,
        }
        repository.jobs["job-terminal"] = {
            "id": "job-terminal",
            "type": "static_scan",
            "status": "failed",
            "created_at": now - timedelta(days=30),
        }
        repository.runs["run-safe"] = {
            "id": "run-safe",
            "type": "static",
            "status": "failed",
            "queued_at": now - timedelta(seconds=5),
            "started_at": now - timedelta(seconds=4),
            "completed_at": now,
            "created_at": now - timedelta(seconds=5),
        }
        repository.runs["run-private"] = {
            "id": "run-private",
            "type": "file-/private/source.py",
            "status": "failed",
            "queued_at": now,
            "created_at": now,
        }
        result = await runtime.review_operations_status()
        await runtime.close()
        return result

    result = asyncio.run(scenario())
    metrics = result["metrics"]

    assert metrics["queue"] == [{"job_type": "static_scan", "status": "queued", "count": 1}]
    assert [item["run_type"] for item in metrics["stages"]] == ["static"]
    rendered = str(metrics)
    assert "alice" not in rendered
    assert "/private/" not in rendered
