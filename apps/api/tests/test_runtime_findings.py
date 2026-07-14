from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.artifacts.runner_contract import build_runtime_dispatch_result
from app.artifacts.runtime_findings import (
    NormalizedRuntimeFinding,
    normalize_runtime_findings,
    normalize_target_resolution_finding,
)
from app.artifacts.runtime_targets import RuntimeImage, RuntimeTargetResolver
from tests.test_runtime_dispatch import runtime_result
from tests.test_runtime_targets import policy


def normalize(result):
    return normalize_runtime_findings(
        result,
        tool_name="runtime-runner",
        tool_version="probe-v1",
    )


def test_passed_runtime_result_has_no_findings() -> None:
    assert normalize(runtime_result()) == ()


def test_cleanup_failure_is_high_non_deterministic_and_contains_audit_snapshot() -> None:
    result = runtime_result(cleanup_failed=True)
    findings = normalize(result)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "cleanup_failed"
    assert finding.severity == "high"
    assert not finding.deterministic
    assert finding.metadata["target"]["astrbot_version"] == "4.26.5"
    assert finding.metadata["target"]["image_digest"].startswith("sha256:")
    assert finding.metadata["dependency_snapshot"]["after_sha256"] == "d" * 64
    assert "logs_key" not in finding.metadata


def test_dependency_conflicts_are_normalized_per_package_with_stable_fingerprints() -> None:
    payload = runtime_result().model_dump(mode="json")
    payload.pop("result_sha256")
    payload["install"].update(
        {
            "status": "failed",
            "error_code": "astrbot_core_dependency_conflict",
            "message": "core dependency changed",
            "conflicts": [
                {
                    "package": "fastapi",
                    "installed_version": "0.9.0",
                    "requirement": ">=1.0.0",
                    "required_by": "AstrBot",
                }
            ],
        }
    )
    result = build_runtime_dispatch_result(payload)

    first = normalize(result)
    second = normalize(result)

    assert first == second
    assert {finding.rule_id for finding in first} == {"astrbot_core_dependency_conflict"}
    package_finding = next(item for item in first if item.metadata.get("package") == "fastapi")
    assert package_finding.severity == "critical"
    assert package_finding.deterministic


def test_smoke_phase_failures_skip_unreached_phases_and_preserve_distinct_code() -> None:
    payload = runtime_result().model_dump(mode="json")
    payload.pop("result_sha256")
    payload["smoke"].update(
        {
            "status": "failed",
            "error_code": "plugin_initialize_failed",
            "message": "initialize failed",
        }
    )
    payload["smoke"]["initialize"] = {
        "status": "failed",
        "error_code": "plugin_initialize_failed",
        "message": "initialize failed",
    }
    payload["smoke"]["startup"] = {
        "status": "skipped",
        "error_code": "probe_not_reached",
        "message": "not reached",
    }
    result = build_runtime_dispatch_result(payload)

    findings = normalize(result)

    assert [finding.rule_id for finding in findings] == ["plugin_initialize_failed"]
    assert findings[0].metadata["phase"] == "initialize"
    assert findings[0].deterministic


@pytest.mark.parametrize("category", ["docker_socket_exposed", "smoke_network_access"])
def test_runtime_isolation_violations_are_critical(category: str) -> None:
    payload = runtime_result().model_dump(mode="json")
    payload.pop("result_sha256")
    payload["smoke"]["violations"] = [
        {
            "phase": "smoke",
            "category": category,
            "message": "runtime isolation boundary was reachable",
            "count": 1,
        }
    ]
    result = build_runtime_dispatch_result(payload)

    findings = normalize(result)

    assert len(findings) == 1
    assert findings[0].rule_id == category
    assert findings[0].severity == "critical"
    assert findings[0].deterministic


def test_target_resolution_finding_keeps_plugin_and_runtime_versions_separate() -> None:
    review_policy = policy(("4.26.5", "3.12"))
    resolver = RuntimeTargetResolver(
        [
            RuntimeImage(
                astrbot_version="4.26.5",
                python_version="3.12",
                image_digest=f"sha256:{'a' * 64}",
            )
        ]
    )
    resolution = resolver.resolve(
        review_policy,
        {"astrbot_version": ">=5"},
        plugin_version="v9.8.7",
        plugin_normalized_version="9.8.7",
    )

    finding = normalize_target_resolution_finding(
        resolution,
        policy_version_id="policy-1",
        tool_version="resolver-v1",
    )

    assert finding and finding.rule_id == "astrbot_version_incompatible"
    assert finding.severity == "critical"
    assert finding.metadata["plugin_version"] == "v9.8.7"
    assert "astrbot_version" not in finding.metadata


def test_finding_boundaries_reject_credentials_and_oversized_metadata() -> None:
    base = {
        "fingerprint": "a" * 64,
        "rule_id": "plugin_import_failed",
        "severity": "high",
        "category": "plugin_lifecycle",
        "message": "Bearer abcdefghijklmnopqrstuvwxyz",
        "deterministic": True,
        "metadata": {},
    }
    with pytest.raises(ValidationError, match="credential"):
        NormalizedRuntimeFinding.model_validate(base)
    with pytest.raises(ValidationError, match="8192"):
        NormalizedRuntimeFinding.model_validate(
            {**base, "message": "safe", "metadata": {"value": "x" * 9000}}
        )
