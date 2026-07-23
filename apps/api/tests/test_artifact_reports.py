from __future__ import annotations

import json
from datetime import UTC, datetime

from app.artifacts.schemas import (
    ArtifactDetailResponse,
    PublicReviewDecision,
    PublicReviewFinding,
    PublicReviewRun,
)
from app.artifacts.service import (
    public_artifact,
    public_review_decision,
    public_review_finding,
    public_review_run,
)


def test_public_report_projections_are_allowlists_and_strip_nested_private_data() -> None:
    now = datetime.now(UTC)
    artifact = public_artifact(
        {
            "id": "artifact-report-v1",
            "plugin_id": "astrbot_plugin_report",
            "plugin_name": "Report",
            "version": "v1.0.0",
            "normalized_version": "1.0.0",
            "source_type": "github",
            "archive_sha256": "a" * 64,
            "size_bytes": 128,
            "review_status": "pending_review",
            "publication_status": "unpublished",
            "risk_level": "medium",
            "download_url": "https://cdn.example.test/dirty.zip",
            "review_coverage": {
                "routing": {
                    "route": "manual_review",
                    "reason_codes": ["finding_requires_manual_review"],
                    "provider_response": {"private": "must-not-leak"},
                    "hunks_key": "private/hunks.json",
                    "internal_endpoint": "https://private.example.test",
                    "nested": {
                        "stdout": "source code output",
                        "safe_count": 2,
                    },
                }
            },
            "quarantine_key": "private/source.zip",
            "published_key": "private/published.zip",
            "path_suffix": "abcdef1234",
            "submitted_by_snapshot": {"email": "private@example.test"},
            "created_at": now,
            "updated_at": now,
        }
    )
    run = public_review_run(
        {
            "id": "run-report-v1",
            "artifact_id": artifact["id"],
            "type": "llm_summary",
            "status": "succeeded",
            "model": "review-model",
            "summary": "Manual review recommended",
            "coverage": {
                "outcome": "completed",
                "prompt": "private prompt",
                "result_key": "private/result.json",
                "coverage_count": 3,
            },
            "raw_result": {"provider_response": "private"},
            "raw_result_key": "private/raw.json",
            "worker_id": "private-worker",
            "created_at": now,
        }
    )
    finding = public_review_finding(
        {
            "id": "finding-report-v1",
            "artifact_id": artifact["id"],
            "run_id": run["id"],
            "fingerprint": "llm-report-risk",
            "severity": "high",
            "message": "Review this behavior",
            "source": "llm",
            "deterministic": False,
            "status": "open",
            "metadata": {"api_key": "private"},
            "created_at": now,
        }
    )
    decision = public_review_decision(
        {
            "id": "decision-report-v1",
            "artifact_id": artifact["id"],
            "action": "request_changes",
            "from_status": "pending_review",
            "to_status": "changes_requested",
            "reason": "Please revise",
            "source": "admin",
            "input_run_ids": [run["id"]],
            "input_fingerprints": [finding["fingerprint"]],
            "metadata": {
                "routing": {"route": "manual_review"},
                "authorization": "private bearer",
                "nested": {"logs": ["private log"]},
            },
            "idempotency_key": "private-idempotency-key",
            "created_at": now,
        }
    )

    report = ArtifactDetailResponse.model_validate(
        {
            "artifact": artifact,
            "runs": [run],
            "findings": [finding],
            "decisions": [decision],
        }
    )
    serialized = report.model_dump_json()

    assert report.runs[0].advisory is True
    assert report.runs[0].label == "自动审查建议"
    assert report.findings[0].advisory is True
    assert report.artifact.download_url is None
    assert report.artifact.review_coverage["routing"]["nested"] == {"safe_count": 2}
    assert report.decisions[0].metadata == {"routing": {"route": "manual_review"}, "nested": {}}
    for private_value in (
        "must-not-leak",
        "private prompt",
        "private/result.json",
        "private/source.zip",
        "private/published.zip",
        "private bearer",
        "private-idempotency-key",
        "private/hunks.json",
        "https://private.example.test",
    ):
        assert private_value not in serialized
    assert json.loads(serialized)["runs"][0]["coverage"]["coverage_count"] == 3


def test_public_report_models_reject_unprojected_extra_fields() -> None:
    now = datetime.now(UTC)
    base_run = {
        "id": "run-extra",
        "artifact_id": "artifact-extra",
        "type": "static",
        "status": "succeeded",
        "advisory": False,
        "label": "确定性检查",
        "coverage": {},
        "created_at": now,
    }
    base_finding = {
        "id": "finding-extra",
        "artifact_id": "artifact-extra",
        "run_id": "run-extra",
        "fingerprint": "extra",
        "severity": "low",
        "message": "message",
        "advisory": False,
        "label": "确定性检查",
        "created_at": now,
    }
    base_decision = {
        "id": "decision-extra",
        "artifact_id": "artifact-extra",
        "action": "reject",
        "from_status": "pending_review",
        "to_status": "rejected",
        "source": "admin",
        "metadata": {},
        "created_at": now,
    }

    for model, payload in (
        (PublicReviewRun, {**base_run, "raw_result": {}}),
        (PublicReviewFinding, {**base_finding, "metadata": {}}),
        (PublicReviewDecision, {**base_decision, "idempotency_key": "private"}),
    ):
        try:
            model.model_validate(payload)
        except ValueError as exc:
            assert "extra_forbidden" in str(exc)
        else:
            raise AssertionError("unprojected report field was accepted")
