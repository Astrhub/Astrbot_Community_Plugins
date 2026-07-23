from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import logging
from pathlib import Path
from typing import Any

import pytest

from app.artifacts.models import ArtifactStateError, ReviewStatus
from app.artifacts.policy import ReviewPolicyV1, review_policy_sha256
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.routing_evaluator import RouteKind, RoutingEvaluation, RoutingEvaluator
from app.artifacts.stages import RoutingStage, StageContext, StageOutcomeKind
from app.artifacts.storage import LocalArtifactStorage
from app.store import InMemoryMarketStore


POLICY_ID = "policy-routing-v1"


def _policy(
    *,
    auto_approve: bool = False,
    degraded_action: str = "manual_review",
) -> ReviewPolicyV1:
    return ReviewPolicyV1.model_validate(
        {
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
            "routing": {
                "auto_approve": auto_approve,
                "manual_review_at": "medium",
                "deterministic_reject_at": "critical",
                "degraded_action": degraded_action,
                "require_complete_coverage": True,
            },
        }
    )


def _artifact(**updates: Any) -> dict[str, Any]:
    value = {
        "id": "artifact-routing-v1",
        "plugin_id": "astrbot_plugin_demo",
        "version": "v1.0.0",
        "normalized_version": "1.0.0",
        "repo_version": "v1.0.0",
        "policy_version_id": POLICY_ID,
        "review_status": ReviewStatus.SCANNING.value,
        "risk_level": "none",
    }
    value.update(updates)
    return value


def _runs() -> list[dict[str, Any]]:
    return [
        {
            "id": "run-static",
            "artifact_id": "artifact-routing-v1",
            "type": "static",
            "status": "succeeded",
            "tool_name": "static",
            "tool_version": "p1.1",
            "policy_version_id": POLICY_ID,
            "coverage": {
                "outcome": "completed",
                "stage_name": "static",
                "risk_level": "none",
            },
        },
        {
            "id": "run-runtime",
            "artifact_id": "artifact-routing-v1",
            "type": "runtime",
            "status": "succeeded",
            "tool_name": "runtime-runner",
            "tool_version": "runtime-v1",
            "policy_version_id": POLICY_ID,
            "astrbot_version": "4.26.5",
            "python_version": "3.12",
            "coverage": {
                "outcome": "completed",
                "stage_name": "runtime",
                "complete": True,
            },
        },
    ]


def _finding(
    *,
    severity: str,
    deterministic: bool,
    source: str,
    fingerprint: str = "finding-risk",
) -> dict[str, Any]:
    return {
        "id": f"id-{fingerprint}",
        "artifact_id": "artifact-routing-v1",
        "run_id": "run-static" if deterministic else "run-llm",
        "fingerprint": fingerprint,
        "severity": severity,
        "status": "open",
        "source": source,
        "deterministic": deterministic,
        "message": "Review risk",
    }


def _evaluate(
    *,
    policy: ReviewPolicyV1 | None = None,
    artifact: dict[str, Any] | None = None,
    runs: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
):
    return RoutingEvaluator().evaluate(
        artifact=artifact or _artifact(),
        policy=policy or _policy(),
        runs=runs or _runs(),
        findings=findings or [],
    )


def test_clean_result_still_requires_manual_review_when_auto_approve_is_disabled() -> None:
    result = _evaluate()

    assert result.kind is RouteKind.MANUAL_REVIEW
    assert result.target_status == ReviewStatus.PENDING_REVIEW.value
    assert "auto_approve_disabled" in result.reason_codes
    assert result.input_run_ids == ("run-runtime", "run-static")
    assert len(result.coverage_sha256) == 64


def test_clean_complete_runtime_policy_can_auto_approve() -> None:
    result = _evaluate(policy=_policy(auto_approve=True))

    assert result.kind is RouteKind.AUTO_APPROVE
    assert result.target_status == ReviewStatus.APPROVED.value
    assert result.reason_codes == ("all_auto_approve_gates_passed",)
    assert result.complete is True
    assert result.version_match is True


def test_runtime_matrix_uses_exact_run_versions_with_production_coverage_shape() -> None:
    policy_data = _policy(auto_approve=True).model_dump(mode="json")
    policy_data["runtime_targets"].append({"astrbot": "4.27.0", "python": "3.12"})
    runs = _runs()
    runs.append(
        {
            **deepcopy(runs[-1]),
            "id": "run-runtime-4-27",
            "astrbot_version": "4.27.0",
        }
    )

    complete = _evaluate(policy=ReviewPolicyV1.model_validate(policy_data), runs=runs)
    missing_target = _evaluate(
        policy=ReviewPolicyV1.model_validate(policy_data),
        runs=runs[:2],
    )

    assert complete.kind is RouteKind.AUTO_APPROVE
    assert complete.input_run_ids == ("run-runtime", "run-runtime-4-27", "run-static")
    assert missing_target.kind is RouteKind.MANUAL_REVIEW
    assert "runtime:4.27.0:python-3.12" in missing_target.coverage["missing_units"]


def test_only_deterministic_critical_can_auto_reject() -> None:
    deterministic = _evaluate(
        findings=[
            _finding(
                severity="critical",
                deterministic=True,
                source="runtime",
            )
        ]
    )
    advisory_runs = _runs()
    advisory_runs.append(
        {
            "id": "run-llm",
            "artifact_id": "artifact-routing-v1",
            "type": "llm_file",
            "status": "succeeded",
            "tool_name": "structured-llm",
            "tool_version": "llm-v1",
            "policy_version_id": POLICY_ID,
            "coverage": {
                "outcome": "completed",
                "stage_name": "llm_file:file:main",
                "complete": True,
            },
        }
    )
    advisory = _evaluate(
        policy=_policy(auto_approve=True),
        runs=advisory_runs,
        findings=[
            _finding(
                severity="critical",
                deterministic=False,
                source="llm",
            )
        ],
    )

    assert deterministic.kind is RouteKind.AUTO_REJECT
    assert deterministic.target_status == ReviewStatus.REJECTED.value
    assert deterministic.input_fingerprints == ("finding-risk",)
    assert advisory.kind is RouteKind.MANUAL_REVIEW
    assert "finding_requires_manual_review" in advisory.reason_codes


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("degraded", "required_stage_degraded"),
        ("incomplete", "required_stage_incomplete"),
        ("missing", "required_stage_missing"),
        ("version", "artifact_repo_version_mismatch"),
    ],
)
def test_incomplete_or_drifted_inputs_never_auto_approve(
    mutation: str,
    expected_reason: str,
) -> None:
    runs = deepcopy(_runs())
    artifact = _artifact()
    if mutation == "degraded":
        runs[1]["status"] = "failed"
        runs[1]["coverage"] = {
            "outcome": "degraded",
            "stage_name": "runtime",
            "complete": False,
        }
    elif mutation == "incomplete":
        runs[1]["coverage"]["complete"] = False
    elif mutation == "missing":
        runs = runs[:1]
    else:
        artifact["repo_version"] = "v1.1.0"

    result = _evaluate(
        policy=_policy(auto_approve=True),
        artifact=artifact,
        runs=runs,
    )

    assert result.kind is RouteKind.MANUAL_REVIEW
    assert expected_reason in result.reason_codes


def test_completed_import_graph_with_incomplete_coverage_requires_manual_review() -> None:
    policy_data = _policy(auto_approve=True).model_dump(mode="json")
    policy_data["required_stages"] = ["static", "diff", "import_graph", "runtime"]
    runs = [
        *_runs(),
        {
            "id": "run-diff",
            "artifact_id": "artifact-routing-v1",
            "type": "diff",
            "status": "succeeded",
            "tool_name": "artifact-diff",
            "tool_version": "artifact-diff-v1",
            "policy_version_id": POLICY_ID,
            "coverage": {
                "outcome": "completed",
                "stage_name": "diff",
                "complete": True,
            },
        },
        {
            "id": "run-import-graph",
            "artifact_id": "artifact-routing-v1",
            "type": "import_graph",
            "status": "succeeded",
            "tool_name": "python-ast-import-graph",
            "tool_version": "python-ast-import-graph-v1",
            "policy_version_id": POLICY_ID,
            "coverage": {
                "outcome": "completed",
                "stage_name": "import_graph",
                "complete": False,
                "full_review_required": True,
                "reasons": ["dynamic_import:main.py:1"],
            },
        },
    ]

    result = _evaluate(
        policy=ReviewPolicyV1.model_validate(policy_data),
        runs=runs,
    )

    assert result.kind is RouteKind.MANUAL_REVIEW
    assert "required_stage_incomplete" in result.reason_codes


def test_fail_closed_policy_rejects_production_runtime_failure() -> None:
    runs = deepcopy(_runs())
    runs[1]["status"] = "failed"
    runs[1]["coverage"] = {
        "outcome": "failed",
        "stage_name": "runtime",
        "complete": False,
    }

    result = _evaluate(
        policy=_policy(degraded_action="fail_closed"),
        runs=runs,
    )

    assert result.kind is RouteKind.AUTO_REJECT
    assert "policy_fail_closed" in result.reason_codes


def test_finding_threshold_and_policy_snapshot_filter_are_explicit() -> None:
    below_threshold = _evaluate(
        policy=_policy(auto_approve=True),
        findings=[_finding(severity="low", deterministic=True, source="static", fingerprint="low")],
    )
    cross_policy_runs = _runs()
    cross_policy_runs.append(
        {
            "id": "run-old-policy",
            "artifact_id": "artifact-routing-v1",
            "type": "static",
            "status": "succeeded",
            "policy_version_id": "policy-old",
            "coverage": {"outcome": "completed", "stage_name": "static"},
        }
    )
    cross_policy = _evaluate(
        policy=_policy(auto_approve=True),
        runs=cross_policy_runs,
        findings=[
            {
                **_finding(
                    severity="critical",
                    deterministic=True,
                    source="static",
                    fingerprint="old-critical",
                ),
                "run_id": "run-old-policy",
            }
        ],
    )

    assert below_threshold.kind is RouteKind.AUTO_APPROVE
    assert cross_policy.kind is RouteKind.AUTO_APPROVE
    assert cross_policy.input_fingerprints == ()


@pytest.mark.parametrize("outcome", ["blocked", "degraded"])
def test_llm_stage_failure_never_becomes_an_automatic_rejection(outcome: str) -> None:
    policy_data = _policy(degraded_action="fail_closed").model_dump(mode="json")
    policy_data["required_stages"].append("llm_package")
    policy_data["llm"] = {
        "enabled": True,
        "model": "review-model",
        "max_tokens": 4096,
        "max_cost_microusd": 100_000,
        "input_cost_microusd_per_million_tokens": 1_000_000,
        "output_cost_microusd_per_million_tokens": 4_000_000,
    }
    runs = _runs()
    runs.append(
        {
            "id": "run-llm-package",
            "artifact_id": "artifact-routing-v1",
            "type": "llm_package",
            "status": "succeeded" if outcome == "blocked" else "failed",
            "tool_name": "structured-llm",
            "tool_version": "llm-v1",
            "policy_version_id": POLICY_ID,
            "coverage": {
                "outcome": outcome,
                "stage_name": "llm_package",
                "complete": False,
            },
        }
    )

    result = _evaluate(policy=ReviewPolicyV1.model_validate(policy_data), runs=runs)

    assert result.kind is RouteKind.MANUAL_REVIEW
    assert any(reason.startswith("advisory_stage_") for reason in result.reason_codes)


@pytest.mark.parametrize("outcome", ["blocked", "degraded"])
def test_category_stage_failure_is_always_advisory(outcome: str) -> None:
    policy_data = _policy(degraded_action="fail_closed").model_dump(mode="json")
    policy_data["required_stages"].append("category")
    policy_data["category"] = {"enabled": True, "model": "category-model"}
    runs = _runs()
    runs.append(
        {
            "id": "run-category",
            "artifact_id": "artifact-routing-v1",
            "type": "category",
            "status": "succeeded" if outcome == "blocked" else "failed",
            "tool_name": "category",
            "tool_version": "category-v1",
            "policy_version_id": POLICY_ID,
            "coverage": {
                "outcome": outcome,
                "stage_name": "category",
                "complete": False,
            },
        }
    )

    result = _evaluate(policy=ReviewPolicyV1.model_validate(policy_data), runs=runs)

    assert result.kind is RouteKind.MANUAL_REVIEW
    assert any(reason.startswith("advisory_stage_") for reason in result.reason_codes)


def test_llm_file_failure_is_always_advisory() -> None:
    policy_data = _policy(degraded_action="fail_closed").model_dump(mode="json")
    policy_data["required_stages"].extend(["llm_package", "llm_file"])
    policy_data["llm"] = {
        "enabled": True,
        "model": "review-model",
        "max_tokens": 4096,
        "max_cost_microusd": 100_000,
        "input_cost_microusd_per_million_tokens": 1_000_000,
        "output_cost_microusd_per_million_tokens": 4_000_000,
    }
    runs = _runs()
    runs.extend(
        [
            {
                "id": "run-llm-package",
                "artifact_id": "artifact-routing-v1",
                "type": "llm_package",
                "status": "succeeded",
                "policy_version_id": POLICY_ID,
                "coverage": {
                    "outcome": "completed",
                    "stage_name": "llm_package",
                    "complete": True,
                },
            },
            {
                "id": "run-llm-file",
                "artifact_id": "artifact-routing-v1",
                "type": "llm_file",
                "status": "failed",
                "policy_version_id": POLICY_ID,
                "coverage": {
                    "outcome": "degraded",
                    "stage_name": "llm_file",
                    "complete": False,
                },
            },
        ]
    )

    result = _evaluate(policy=ReviewPolicyV1.model_validate(policy_data), runs=runs)

    assert result.kind is RouteKind.MANUAL_REVIEW
    assert "advisory_stage_degraded" in result.reason_codes


async def _repository_fixture(
    *,
    auto_approve: bool = True,
) -> tuple[
    InMemoryArtifactRepository,
    dict[str, Any],
    dict[str, Any],
    RoutingEvaluation,
]:
    store = InMemoryMarketStore()
    owner = store.upsert_github_user({"id": "owner-1", "login": "alice", "name": "Alice"})
    plugin = store.register_plugin(
        owner,
        {
            "name": "astrbot_plugin_demo",
            "display_name": "Demo",
            "desc": "Demo plugin",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_demo",
            "category": "other",
        },
    )
    store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.0.0"})
    repository = InMemoryArtifactRepository(store)
    policy_model = _policy(auto_approve=auto_approve)
    policy = await repository.create_review_policy(
        {
            "version": "routing-policy-v1",
            "schema_version": "1",
            "status": "active",
            "is_default": True,
            "policy": policy_model.model_dump(mode="json"),
            "policy_sha256": review_policy_sha256(policy_model),
            "validation_summary": {"valid": True},
            "validated_at": datetime.now(UTC).isoformat(),
            "activated_at": datetime.now(UTC).isoformat(),
        }
    )
    artifact = await repository.create_artifact(
        {
            "plugin_id": plugin["id"],
            "version": "v1.0.0",
            "normalized_version": "1.0.0",
            "source_type": "upload",
            "source_repo": plugin["repo"],
            "archive_sha256": "a" * 64,
            "tree_sha256": "b" * 64,
            "size_bytes": 128,
            "quarantine_key": "artifacts/routing/source.zip",
            "submitted_by": owner["id"],
            "policy_version_id": policy["id"],
        }
    )
    await repository.transition_review_status(artifact["id"], "prechecking")
    artifact = await repository.transition_review_status(artifact["id"], "scanning")
    assert artifact is not None
    runs: list[dict[str, Any]] = []
    for value in _runs():
        current = {
            **value,
            "artifact_id": artifact["id"],
            "policy_version_id": policy["id"],
        }
        runs.append(await repository.create_review_run(current))
    current_artifact = await repository.get_artifact(artifact["id"])
    assert current_artifact is not None
    evaluation = RoutingEvaluator().evaluate(
        artifact=current_artifact,
        policy=policy_model,
        runs=await repository.list_review_runs(artifact["id"]),
        findings=[],
    )
    assert evaluation.kind is (RouteKind.AUTO_APPROVE if auto_approve else RouteKind.MANUAL_REVIEW)
    return repository, current_artifact, policy, evaluation


def _stage_context(
    repository: InMemoryArtifactRepository,
    storage: LocalArtifactStorage,
    artifact: dict[str, Any],
    policy: dict[str, Any],
    *,
    attempts: int = 1,
) -> StageContext:
    return StageContext.create(
        job={
            "id": "job-routing-v1",
            "artifact_id": artifact["id"],
            "type": "route_review",
            "attempts": attempts,
            "policy_version_id": policy["id"],
            "payload": {
                "stage": "routing",
                "tool_version": "routing-v1",
                "stage_states": {"static": "completed", "runtime": "completed"},
            },
        },
        artifact=artifact,
        policy=policy,
        repository=repository,
        storage=storage,
        tools={},
        logger=logging.getLogger("test-routing"),
    )


def test_atomic_auto_approve_is_idempotent_and_enqueues_existing_publish_job() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        repository, artifact, policy, evaluation = await _repository_fixture()
        results = await asyncio.gather(
            *(
                repository.auto_approve_artifact(
                    artifact["id"],
                    reason=evaluation.summary,
                    expected_repo_version="v1.0.0",
                    expected_normalized_version="1.0.0",
                    expected_version="v1.0.0",
                    idempotency_key="routing-auto-approve-once",
                    policy_version_id=policy["id"],
                    input_run_ids=evaluation.input_run_ids,
                    input_fingerprints=evaluation.input_fingerprints,
                    coverage_sha256=evaluation.coverage_sha256,
                    metadata={"routing": dict(evaluation.coverage)},
                    risk_level=evaluation.risk_level,
                )
                for _ in range(12)
            )
        )
        return (
            [item for item in results if item is not None],
            await repository.list_review_decisions(artifact["id"]),
            await repository.list_artifact_jobs(artifact["id"]),
        )

    results, decisions, jobs = asyncio.run(scenario())

    assert len(results) == 12
    assert {item["review_status"] for item in results} == {"approved"}
    assert [item["action"] for item in decisions] == ["auto_approve"]
    assert decisions[0]["source"] == "policy"
    assert decisions[0]["input_run_ids"] == ["run-runtime", "run-static"]
    assert len(decisions[0]["coverage_sha256"]) == 64
    assert [item["type"] for item in jobs] == ["publish"]
    assert jobs[0]["idempotency_key"].startswith("publish:")
    assert jobs[0]["payload"] == {"expected_repo_version": "v1.0.0"}


def test_atomic_auto_approve_rejects_version_drift_without_partial_state() -> None:
    async def scenario() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        repository, artifact, policy, evaluation = await _repository_fixture()
        with pytest.raises(ValueError, match="repo_version_changed"):
            await repository.auto_approve_artifact(
                artifact["id"],
                reason=evaluation.summary,
                expected_repo_version="v1.1.0",
                expected_normalized_version="1.0.0",
                expected_version="v1.0.0",
                idempotency_key="routing-auto-approve-drift",
                policy_version_id=policy["id"],
                input_run_ids=evaluation.input_run_ids,
                input_fingerprints=evaluation.input_fingerprints,
                coverage_sha256=evaluation.coverage_sha256,
                metadata={"routing": dict(evaluation.coverage)},
                risk_level=evaluation.risk_level,
            )
        current = await repository.get_artifact(artifact["id"])
        assert current is not None
        return (
            current,
            await repository.list_review_decisions(artifact["id"]),
            await repository.list_artifact_jobs(artifact["id"]),
        )

    artifact, decisions, jobs = asyncio.run(scenario())

    assert artifact["review_status"] == "scanning"
    assert decisions == []
    assert jobs == []


def test_concurrent_auto_approve_and_admin_reject_have_one_legal_winner() -> None:
    async def scenario() -> tuple[list[Any], dict[str, Any], list[dict[str, Any]]]:
        repository, artifact, policy, evaluation = await _repository_fixture()
        results = await asyncio.gather(
            repository.auto_approve_artifact(
                artifact["id"],
                reason=evaluation.summary,
                expected_repo_version="v1.0.0",
                expected_normalized_version="1.0.0",
                expected_version="v1.0.0",
                idempotency_key="routing-auto-approve-race",
                policy_version_id=policy["id"],
                input_run_ids=evaluation.input_run_ids,
                input_fingerprints=evaluation.input_fingerprints,
                coverage_sha256=evaluation.coverage_sha256,
                metadata={"routing": dict(evaluation.coverage)},
                risk_level=evaluation.risk_level,
            ),
            repository.decide_artifact(
                artifact["id"],
                action="reject",
                target_status="rejected",
                reason="Administrator rejected concurrently",
                reviewer={"id": "admin-1", "internal_username": "admin"},
                idempotency_key="admin-reject-race",
                policy_version_id=policy["id"],
            ),
            return_exceptions=True,
        )
        current = await repository.get_artifact(artifact["id"])
        assert current is not None
        return results, current, await repository.list_review_decisions(artifact["id"])

    results, artifact, decisions = asyncio.run(scenario())

    assert artifact["review_status"] in {"approved", "rejected"}
    assert len(decisions) == 1
    assert sum(isinstance(item, ArtifactStateError) for item in results) == 1


def test_routing_stage_manual_path_records_run_without_automatic_decision(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        repository, artifact, policy, _ = await _repository_fixture(auto_approve=False)
        outcome = await RoutingStage().execute(
            _stage_context(
                repository,
                LocalArtifactStorage(tmp_path, "https://cdn.example.test"),
                artifact,
                policy,
            )
        )
        current = await repository.get_artifact(artifact["id"])
        assert current is not None
        return (
            outcome,
            current,
            await repository.list_review_runs(artifact["id"]),
            await repository.list_review_decisions(artifact["id"]),
        )

    outcome, artifact, runs, decisions = asyncio.run(scenario())

    assert outcome.kind is StageOutcomeKind.COMPLETED
    assert artifact["review_status"] == "pending_review"
    assert decisions == []
    routing = next(item for item in runs if item["type"] == "routing")
    assert routing["status"] == "succeeded"
    assert routing["coverage"]["route"] == "manual_review"
    assert routing["coverage"]["reason_codes"] == ["auto_approve_disabled"]


def test_routing_stage_auto_rejects_deterministic_critical_with_audit_inputs(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
        repository, artifact, policy, _ = await _repository_fixture(auto_approve=True)
        static_run = next(
            item
            for item in await repository.list_review_runs(artifact["id"])
            if item["type"] == "static"
        )
        await repository.replace_findings(
            artifact["id"],
            static_run["id"],
            [
                {
                    "fingerprint": "deterministic-critical-route",
                    "severity": "critical",
                    "category": "code_execution",
                    "message": "Confirmed deterministic critical risk",
                    "source": "runtime",
                    "deterministic": True,
                }
            ],
        )
        outcome = await RoutingStage().execute(
            _stage_context(
                repository,
                LocalArtifactStorage(tmp_path, "https://cdn.example.test"),
                artifact,
                policy,
            )
        )
        current = await repository.get_artifact(artifact["id"])
        decisions = await repository.list_review_decisions(artifact["id"])
        assert current is not None
        alert = next(
            item for item in repository.outbox.values() if item["event_type"] == "artifact_rejected"
        )
        return outcome, current, decisions[0], alert

    outcome, artifact, decision, alert = asyncio.run(scenario())

    assert outcome.kind is StageOutcomeKind.COMPLETED
    assert artifact["review_status"] == "rejected"
    assert artifact["risk_level"] == "critical"
    assert decision["action"] == "auto_reject"
    assert decision["source"] == "policy"
    assert decision["input_fingerprints"] == ["deterministic-critical-route"]
    assert decision["input_run_ids"] == ["run-runtime", "run-static"]
    assert decision["metadata"]["routing"]["route"] == "auto_reject"
    assert alert["payload"]["critical"] is True
    assert alert["payload"]["risk_level"] == "critical"


def test_routing_stage_auto_approve_is_recoverable_after_job_ack_loss(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, Any, dict[str, Any], list[Any], list[Any], list[Any]]:
        repository, artifact, policy, _ = await _repository_fixture(auto_approve=True)
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        first = await RoutingStage().execute(_stage_context(repository, storage, artifact, policy))
        current = await repository.get_artifact(artifact["id"])
        assert current is not None
        recovered = await RoutingStage().execute(
            _stage_context(repository, storage, current, policy, attempts=2)
        )
        return (
            first,
            recovered,
            current,
            await repository.list_review_runs(artifact["id"]),
            await repository.list_review_decisions(artifact["id"]),
            await repository.list_artifact_jobs(artifact["id"]),
        )

    first, recovered, artifact, runs, decisions, jobs = asyncio.run(scenario())

    assert first.kind is StageOutcomeKind.COMPLETED
    assert artifact["review_status"] == "approved"
    assert recovered.kind is StageOutcomeKind.COMPLETED
    assert recovered.coverage["recovered"] is True
    assert sum(item["type"] == "routing" and item["status"] == "succeeded" for item in runs) == 1
    assert [item["action"] for item in decisions] == ["auto_approve"]
    assert [item["type"] for item in jobs] == ["publish"]


def test_routing_stage_recovers_crash_after_decision_before_run_completion(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, dict[str, Any], list[Any], list[Any], list[Any]]:
        repository, artifact, policy, evaluation = await _repository_fixture(auto_approve=True)
        await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "routing",
                "status": "running",
                "attempt": 1,
                "tool_name": "routing",
                "tool_version": "routing-v1",
                "policy_version_id": policy["id"],
                "input_sha256": evaluation.coverage_sha256,
                "idempotency_key": "routing-run:job-routing-v1:attempt-1",
                "coverage": {"stage_name": "routing"},
            }
        )
        decision_key = (
            f"routing:{artifact['id']}:{policy['id']}:auto_approve:{evaluation.coverage_sha256}"
        )
        await repository.auto_approve_artifact(
            artifact["id"],
            reason=evaluation.summary,
            expected_repo_version="v1.0.0",
            expected_normalized_version="1.0.0",
            expected_version="v1.0.0",
            idempotency_key=decision_key,
            policy_version_id=policy["id"],
            input_run_ids=evaluation.input_run_ids,
            input_fingerprints=evaluation.input_fingerprints,
            coverage_sha256=evaluation.coverage_sha256,
            metadata={"routing": dict(evaluation.coverage)},
            risk_level=evaluation.risk_level,
        )
        current = await repository.get_artifact(artifact["id"])
        assert current is not None
        recovered = await RoutingStage().execute(
            _stage_context(
                repository,
                LocalArtifactStorage(tmp_path, "https://cdn.example.test"),
                current,
                policy,
                attempts=2,
            )
        )
        refreshed = await repository.get_artifact(artifact["id"])
        assert refreshed is not None
        return (
            recovered,
            refreshed,
            await repository.list_review_runs(artifact["id"]),
            await repository.list_review_decisions(artifact["id"]),
            await repository.list_artifact_jobs(artifact["id"]),
        )

    recovered, artifact, runs, decisions, jobs = asyncio.run(scenario())

    assert recovered.kind is StageOutcomeKind.COMPLETED
    assert recovered.coverage["recovered"] is True
    assert artifact["automated_review_completed_at"] is not None
    routing_runs = [item for item in runs if item["type"] == "routing"]
    assert len(routing_runs) == 1
    assert routing_runs[0]["status"] == "succeeded"
    assert [item["action"] for item in decisions] == ["auto_approve"]
    assert [item["type"] for item in jobs] == ["publish"]


def test_routing_stage_does_not_recover_an_admin_decision_as_policy(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, list[Any], list[Any], list[Any]]:
        repository, artifact, policy, evaluation = await _repository_fixture(auto_approve=True)
        await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "routing",
                "status": "running",
                "attempt": 1,
                "tool_name": "routing",
                "tool_version": "routing-v1",
                "policy_version_id": policy["id"],
                "input_sha256": evaluation.coverage_sha256,
                "idempotency_key": "routing-run:job-routing-v1:attempt-1",
                "coverage": {"stage_name": "routing"},
            }
        )
        await repository.decide_artifact(
            artifact["id"],
            action="reject",
            target_status="rejected",
            reason="Administrator won the concurrent decision",
            reviewer={"id": "admin-1", "internal_username": "admin"},
            idempotency_key="admin-won-routing-race",
            policy_version_id=policy["id"],
        )
        current = await repository.get_artifact(artifact["id"])
        assert current is not None
        outcome = await RoutingStage().execute(
            _stage_context(
                repository,
                LocalArtifactStorage(tmp_path, "https://cdn.example.test"),
                current,
                policy,
                attempts=2,
            )
        )
        return (
            outcome,
            await repository.list_review_runs(artifact["id"]),
            await repository.list_review_decisions(artifact["id"]),
            await repository.list_artifact_jobs(artifact["id"]),
        )

    outcome, runs, decisions, jobs = asyncio.run(scenario())

    assert outcome.kind is StageOutcomeKind.TERMINAL_FAILURE
    assert outcome.error_code == "artifact_route_conflict"
    routing_runs = [item for item in runs if item["type"] == "routing"]
    assert len(routing_runs) == 1
    assert routing_runs[0]["status"] == "failed"
    assert routing_runs[0]["coverage"]["observed_status"] == "rejected"
    assert [item["action"] for item in decisions] == ["reject"]
    assert jobs == []
