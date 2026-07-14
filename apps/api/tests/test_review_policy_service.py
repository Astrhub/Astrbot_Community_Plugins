from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.artifacts.models import (
    ArtifactErrorCode,
    ReviewPolicyEventAction,
    ReviewPolicyStatus,
)
from app.artifacts.policy_service import (
    ReviewPolicyPermissionError,
    ReviewPolicyService,
    ReviewPolicyServiceError,
)
from app.artifacts.repository import InMemoryArtifactRepository

CORE_ADMIN = {
    "id": "core-1",
    "role": "core_admin",
    "username": "core",
}
NORMAL_ADMIN = {
    "id": "admin-1",
    "role": "admin",
    "github_login": "reviewer",
}


def policy_payload(astrbot_version: str = "4.26.5") -> dict:
    return {
        "schema_version": "1",
        "required_stages": ["static", "runtime", "dependency"],
        "runtime_targets": [{"astrbot": astrbot_version, "python": "3.12"}],
        "limits": {
            "cpu": 1,
            "memory_mb": 768,
            "pids": 128,
            "timeout_seconds": 120,
        },
        "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
        "llm": {"enabled": False},
        "malware": {"clamav": False},
        "dependency": {"enabled": True, "max_severity": "high"},
        "routing": {"auto_approve": False, "manual_review_at": "low"},
    }


def artifact_payload(digest: str) -> dict:
    return {
        "plugin_id": "plugin-policy-test",
        "version": "v1.0.0",
        "normalized_version": "1.0.0",
        "source_type": "upload",
        "source_repo": "https://github.com/alice/astrbot_plugin_policy_test",
        "archive_sha256": digest * 64,
        "size_bytes": 128,
        "quarantine_key": f"artifacts/{digest * 8}/source.zip",
    }


def test_only_core_admin_can_mutate_review_policies() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def scenario() -> None:
        with pytest.raises(ReviewPolicyPermissionError):
            await service.create_draft(
                version="policy-1",
                policy=policy_payload(),
                actor=NORMAL_ADMIN,
                request_id="request-create-denied",
                idempotency_key="policy-create-denied",
            )

        draft = await service.create_draft(
            version="policy-1",
            policy=policy_payload(),
            actor=CORE_ADMIN,
            request_id="request-create",
            idempotency_key="policy-create",
        )
        with pytest.raises(ReviewPolicyPermissionError):
            await service.validate_draft(
                draft["id"],
                actor=NORMAL_ADMIN,
                request_id="request-validate-denied",
                idempotency_key="policy-validate-denied",
            )
        with pytest.raises(ReviewPolicyPermissionError):
            await service.retire(
                draft["id"],
                actor=NORMAL_ADMIN,
                request_id="request-retire-denied",
                idempotency_key="policy-retire-denied",
                reason="Denied",
            )

    asyncio.run(scenario())
    assert len(repository.policies) == 1


def test_policy_draft_validation_activation_and_retry_are_audited() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def scenario() -> tuple[dict, dict, list[dict]]:
        draft = await service.create_draft(
            version="policy-1",
            policy=policy_payload(),
            actor=CORE_ADMIN,
            request_id="request-create",
            idempotency_key="policy-create",
            reason="Initial advanced policy",
        )
        active = await service.activate(
            draft["id"],
            actor=CORE_ADMIN,
            request_id="request-activate",
            idempotency_key="policy-activate",
            reason="Enable advanced review",
        )
        repeated = await service.activate(
            draft["id"],
            actor=CORE_ADMIN,
            request_id="request-activate",
            idempotency_key="policy-activate",
            reason="Enable advanced review",
        )
        return active, repeated, await repository.list_review_policy_events(draft["id"])

    active, repeated, events = asyncio.run(scenario())

    assert active["status"] == ReviewPolicyStatus.ACTIVE.value
    assert active["validation_summary"]["valid"] is True
    assert repeated["id"] == active["id"]
    assert [event["action"] for event in events] == [
        ReviewPolicyEventAction.CREATE.value,
        ReviewPolicyEventAction.VALIDATE.value,
        ReviewPolicyEventAction.ACTIVATE.value,
    ]
    assert len(repository.policy_events) == 3


def test_active_policy_can_be_explicitly_retired_with_an_audit_event() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def scenario() -> tuple[dict, dict | None, list[dict]]:
        draft = await service.create_draft(
            version="policy-retire",
            policy=policy_payload(),
            actor=CORE_ADMIN,
            request_id="request-retire-create",
            idempotency_key="policy-retire-create",
        )
        active = await service.activate(
            draft["id"],
            actor=CORE_ADMIN,
            request_id="request-retire-activate",
            idempotency_key="policy-retire-activate",
            reason="Activate before explicit retirement",
        )
        retired = await service.retire(
            active["id"],
            actor=CORE_ADMIN,
            request_id="request-retire",
            idempotency_key="policy-retire",
            reason="Disable advanced policy",
        )
        return (
            retired,
            await repository.get_active_review_policy(),
            await repository.list_review_policy_events(active["id"]),
        )

    retired, current, events = asyncio.run(scenario())

    assert retired["status"] == ReviewPolicyStatus.RETIRED.value
    assert current is None
    assert events[-1]["action"] == ReviewPolicyEventAction.RETIRE.value
    assert events[-1]["reason"] == "Disable advanced policy"


def test_activation_retires_current_policy_and_rollback_restores_prior_snapshot() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def activate(version: str, astrbot_version: str, index: int) -> dict:
        draft = await service.create_draft(
            version=version,
            policy=policy_payload(astrbot_version),
            actor=CORE_ADMIN,
            request_id=f"request-create-{index}",
            idempotency_key=f"policy-create-{index}",
        )
        return await service.activate(
            draft["id"],
            actor=CORE_ADMIN,
            request_id=f"request-activate-{index}",
            idempotency_key=f"policy-activate-{index}",
            reason=f"Activate {version}",
        )

    async def scenario() -> tuple[dict, dict, dict]:
        first = await activate("policy-1", "4.26.5", 1)
        second = await activate("policy-2", "4.27.0", 2)
        first_after_replace = await repository.get_review_policy(first["id"])
        rolled_back = await service.rollback(
            first["id"],
            actor=CORE_ADMIN,
            request_id="request-rollback-1",
            idempotency_key="policy-rollback-1",
            reason="Runtime regression in policy-2",
        )
        return first_after_replace or {}, second, rolled_back

    first_retired, second, rolled_back = asyncio.run(scenario())

    assert first_retired["status"] == ReviewPolicyStatus.RETIRED.value
    assert rolled_back["status"] == ReviewPolicyStatus.ACTIVE.value
    assert repository.policies[second["id"]]["status"] == ReviewPolicyStatus.RETIRED.value
    active = [
        policy
        for policy in repository.policies.values()
        if policy["status"] == ReviewPolicyStatus.ACTIVE.value
    ]
    assert [policy["id"] for policy in active] == [rolled_back["id"]]
    assert ReviewPolicyEventAction.ROLLBACK.value in {
        event["action"] for event in repository.policy_events.values()
    }

    audit_json = json.dumps(repository.policy_events, sort_keys=True)
    assert "4.26.5" not in audit_json
    assert "pypi-only-v1" not in audit_json
    assert '"redacted": true' in audit_json


def test_invalid_policy_cannot_replace_the_current_active_policy() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def scenario() -> tuple[dict, dict]:
        good = await service.create_draft(
            version="policy-good",
            policy=policy_payload(),
            actor=CORE_ADMIN,
            request_id="request-good-create",
            idempotency_key="policy-good-create",
        )
        active = await service.activate(
            good["id"],
            actor=CORE_ADMIN,
            request_id="request-good-activate",
            idempotency_key="policy-good-activate",
            reason="Known good policy",
        )

        invalid_payload = {"schema_version": "1", "required_stages": ["static"]}
        invalid_json = json.dumps(
            invalid_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        invalid = await repository.create_review_policy(
            {
                "version": "policy-invalid",
                "schema_version": "1",
                "policy": invalid_payload,
                "policy_sha256": hashlib.sha256(invalid_json.encode()).hexdigest(),
                "base_policy_id": active["id"],
            }
        )
        with pytest.raises(ReviewPolicyServiceError) as exc_info:
            await service.activate(
                invalid["id"],
                actor=CORE_ADMIN,
                request_id="request-invalid-activate",
                idempotency_key="policy-invalid-activate",
                reason="Must not activate",
            )
        assert exc_info.value.code == ArtifactErrorCode.REVIEW_POLICY_INVALID.value
        current = await repository.get_active_review_policy()
        invalid_after = await repository.get_review_policy(invalid["id"])
        return current or {}, invalid_after or {}

    current, invalid = asyncio.run(scenario())

    assert current["version"] == "policy-good"
    assert invalid["status"] == ReviewPolicyStatus.DRAFT.value
    assert invalid["validation_summary"]["valid"] is False
    assert invalid["validation_summary"]["issues"]
    invalid_actions = {
        event["action"]
        for event in repository.policy_events.values()
        if event["policy_id"] == invalid["id"]
    }
    assert invalid_actions == {ReviewPolicyEventAction.VALIDATE.value}


def test_service_rejects_invalid_draft_before_storage() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def scenario() -> None:
        invalid = policy_payload()
        invalid["llm"] = {"enabled": True, "api_key": "must-not-be-stored"}
        with pytest.raises(ValidationError):
            await service.create_draft(
                version="policy-invalid",
                policy=invalid,
                actor=CORE_ADMIN,
                request_id="request-invalid",
                idempotency_key="policy-invalid",
            )

    asyncio.run(scenario())
    assert repository.policies == {}
    assert repository.policy_events == {}


def test_policy_version_and_audit_idempotency_conflicts_are_atomic() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def scenario() -> None:
        first = await service.create_draft(
            version="policy-1",
            policy=policy_payload(),
            actor=CORE_ADMIN,
            request_id="request-create-1",
            idempotency_key="policy-create-once",
        )
        repeated = await service.create_draft(
            version="policy-1",
            policy=policy_payload(),
            actor=CORE_ADMIN,
            request_id="request-create-1",
            idempotency_key="policy-create-once",
        )
        assert repeated["id"] == first["id"]

        with pytest.raises(ReviewPolicyServiceError) as version_conflict:
            await service.create_draft(
                version="policy-1",
                policy=policy_payload("4.27.0"),
                actor=CORE_ADMIN,
                request_id="request-create-changed",
                idempotency_key="policy-create-changed",
            )
        assert version_conflict.value.code == ArtifactErrorCode.REVIEW_POLICY_VERSION_CONFLICT

        with pytest.raises(ReviewPolicyServiceError) as key_conflict:
            await service.create_draft(
                version="policy-2",
                policy=policy_payload("4.27.0"),
                actor=CORE_ADMIN,
                request_id="request-create-2",
                idempotency_key="policy-create-once",
            )
        assert key_conflict.value.code == ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT

    asyncio.run(scenario())
    assert len(repository.policies) == 1
    assert len(repository.policy_events) == 1


def test_concurrent_activation_has_one_winner() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def scenario() -> list[object]:
        base = await service.create_draft(
            version="policy-base",
            policy=policy_payload(),
            actor=CORE_ADMIN,
            request_id="request-base-create",
            idempotency_key="policy-base-create",
        )
        base = await service.activate(
            base["id"],
            actor=CORE_ADMIN,
            request_id="request-base-activate",
            idempotency_key="policy-base-activate",
            reason="Base policy",
        )
        candidates = []
        for index, version in enumerate(("4.27.0", "4.28.0"), start=1):
            candidate = await service.create_draft(
                version=f"policy-candidate-{index}",
                policy=policy_payload(version),
                actor=CORE_ADMIN,
                request_id=f"request-candidate-create-{index}",
                idempotency_key=f"policy-candidate-create-{index}",
            )
            candidate = await service.validate_draft(
                candidate["id"],
                actor=CORE_ADMIN,
                request_id=f"request-candidate-validate-{index}",
                idempotency_key=f"policy-candidate-validate-{index}",
            )
            candidates.append(candidate)

        async def activate_candidate(candidate: dict, index: int) -> dict | None:
            return await repository.transition_review_policy(
                candidate["id"],
                action=ReviewPolicyEventAction.ACTIVATE.value,
                expected_policy_sha256=candidate["policy_sha256"],
                expected_active_policy_id=base["id"],
                validation_summary=None,
                event={
                    "action": ReviewPolicyEventAction.ACTIVATE.value,
                    "actor_user_id": CORE_ADMIN["id"],
                    "actor_nickname": CORE_ADMIN["username"],
                    "reason": "Concurrent activation",
                    "request_id": f"request-concurrent-{index}",
                    "base_version": base["version"],
                    "diff": {"redacted": True},
                    "idempotency_key": f"policy-concurrent-{index}",
                },
            )

        return await asyncio.gather(
            *(activate_candidate(candidate, index) for index, candidate in enumerate(candidates)),
            return_exceptions=True,
        )

    results = asyncio.run(scenario())

    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, ValueError)]
    assert len(errors) == 1
    assert str(errors[0]) == ArtifactErrorCode.REVIEW_POLICY_ACTIVATION_CONFLICT.value
    assert (
        sum(
            policy["status"] == ReviewPolicyStatus.ACTIVE.value
            for policy in repository.policies.values()
        )
        == 1
    )


def test_artifact_snapshot_is_fixed_and_inherited_by_jobs_runs_and_decisions() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def create_active(version: str, astrbot_version: str, index: int) -> dict:
        policy = await service.create_draft(
            version=version,
            policy=policy_payload(astrbot_version),
            actor=CORE_ADMIN,
            request_id=f"snapshot-create-{index}",
            idempotency_key=f"snapshot-create-{index}",
        )
        return await service.activate(
            policy["id"],
            actor=CORE_ADMIN,
            request_id=f"snapshot-activate-{index}",
            idempotency_key=f"snapshot-activate-{index}",
            reason=f"Activate snapshot policy {index}",
        )

    async def scenario() -> tuple[dict, dict, dict, dict, dict]:
        await create_active("snapshot-policy-1", "4.26.5", 1)
        first_artifact = await repository.create_artifact(artifact_payload("a"))
        first_artifact = await repository.snapshot_active_review_policy(first_artifact["id"])
        assert first_artifact
        run = await repository.create_review_run(
            {
                "artifact_id": first_artifact["id"],
                "type": "precheck",
                "status": "running",
                "idempotency_key": "snapshot-run-1",
            }
        )
        job = await repository.enqueue_job(
            {
                "artifact_id": first_artifact["id"],
                "type": "static_scan",
                "idempotency_key": "snapshot-job-1",
            }
        )

        second_policy = await create_active("snapshot-policy-2", "4.27.0", 2)
        repeated_snapshot = await repository.snapshot_active_review_policy(first_artifact["id"])
        second_artifact = await repository.create_artifact(artifact_payload("b"))
        second_artifact = await repository.snapshot_active_review_policy(second_artifact["id"])
        assert repeated_snapshot and second_artifact

        with pytest.raises(ValueError, match="artifact_policy_snapshot_conflict"):
            await repository.create_review_run(
                {
                    "artifact_id": first_artifact["id"],
                    "type": "static",
                    "policy_version_id": second_policy["id"],
                }
            )
        decided = await repository.decide_artifact(
            first_artifact["id"],
            action="auto_reject",
            target_status="rejected",
            reason="Snapshot decision",
            reviewer=None,
            idempotency_key="snapshot-decision-1",
        )
        assert decided
        decision = (await repository.list_review_decisions(first_artifact["id"]))[-1]
        return repeated_snapshot, second_artifact, run, job, decision

    first_artifact, second_artifact, run, job, decision = asyncio.run(scenario())

    assert first_artifact["policy_version_id"] == run["policy_version_id"]
    assert first_artifact["policy_version_id"] == job["policy_version_id"]
    assert first_artifact["policy_version_id"] == decision["policy_version_id"]
    assert decision["policy_version"] == "snapshot-policy-1"
    assert second_artifact["policy_version_id"] != first_artifact["policy_version_id"]


def test_core_admin_policy_migration_invalidates_coverage_and_preserves_old_runs() -> None:
    repository = InMemoryArtifactRepository()
    service = ReviewPolicyService(repository)

    async def create_active(version: str, astrbot_version: str, index: int) -> dict:
        draft = await service.create_draft(
            version=version,
            policy=policy_payload(astrbot_version),
            actor=CORE_ADMIN,
            request_id=f"migration-create-{index}",
            idempotency_key=f"migration-create-{index}",
        )
        return await service.activate(
            draft["id"],
            actor=CORE_ADMIN,
            request_id=f"migration-activate-{index}",
            idempotency_key=f"migration-activate-{index}",
            reason=f"Activate migration policy {index}",
        )

    async def scenario() -> tuple[dict, dict, dict, list[dict]]:
        await create_active("migration-policy-1", "4.26.5", 1)
        artifact = await repository.create_artifact(artifact_payload("c"))
        artifact = await repository.snapshot_active_review_policy(artifact["id"])
        assert artifact
        old_run = await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "precheck",
                "status": "succeeded",
                "idempotency_key": "migration-old-run",
            }
        )
        await repository.update_artifact_review_coverage(
            artifact["id"],
            {"precheck": "complete"},
            automated_review_completed=True,
        )
        second_policy = await create_active("migration-policy-2", "4.27.0", 2)

        with pytest.raises(ReviewPolicyPermissionError):
            await service.migrate_artifact_snapshot(
                artifact["id"],
                second_policy["id"],
                actor=NORMAL_ADMIN,
                request_id="migration-denied",
                idempotency_key="migration-denied",
                reason="Denied",
            )
        migrated = await service.migrate_artifact_snapshot(
            artifact["id"],
            second_policy["id"],
            actor=CORE_ADMIN,
            request_id="migration-approved",
            idempotency_key="migration-approved",
            reason="Adopt stricter policy",
        )
        repeated = await service.migrate_artifact_snapshot(
            artifact["id"],
            second_policy["id"],
            actor=CORE_ADMIN,
            request_id="migration-approved",
            idempotency_key="migration-approved",
            reason="Adopt stricter policy",
        )
        new_run = await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "static",
                "status": "running",
                "idempotency_key": "migration-new-run",
            }
        )
        records = await repository.list_review_decisions(artifact["id"])
        records.append(new_run)
        return migrated, repeated, old_run, records

    migrated, repeated, old_run, records = asyncio.run(scenario())
    decision = next(item for item in records if item.get("action") == "policy_migrate")
    new_run = next(item for item in records if item.get("type") == "static")

    assert repeated["id"] == migrated["id"]
    assert migrated["policy_version_id"] != old_run["policy_version_id"]
    assert new_run["policy_version_id"] == migrated["policy_version_id"]
    assert (
        old_run["policy_version_id"]
        == decision["metadata"]["policy_migration"]["from_policy_version_id"]
    )
    assert migrated["automated_review_completed_at"] is None
    assert migrated["review_coverage"]["policy_migration"]["invalidates_automated_review"] is True
    assert decision["policy_version_id"] == migrated["policy_version_id"]
