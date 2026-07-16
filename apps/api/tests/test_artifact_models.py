from __future__ import annotations

import pytest

from app.artifacts.models import (
    ArtifactErrorCode,
    ArtifactStateError,
    DecisionAction,
    FindingSeverity,
    JobType,
    PublicationStatus,
    ReviewRunType,
    ReviewStatus,
    RiskLevel,
    TERMINAL_REVIEW_STATUSES,
    risk_rank,
    review_target_for_decision,
    validate_publication_transition,
    validate_review_transition,
)


def test_advanced_review_enum_contract_keeps_critical_as_risk_only() -> None:
    assert ReviewStatus.CHANGES_REQUESTED.value == "changes_requested"
    assert "critical" not in {item.value for item in ReviewStatus}
    assert RiskLevel.CRITICAL.value == "critical"
    assert {
        "diff_graph",
        "clamav_scan",
        "yara_scan",
        "runtime_dispatch",
        "runtime_collect",
        "dependency_scan",
        "category",
        "llm_package",
        "llm_file",
        "llm_summary",
        "route_review",
    } <= {item.value for item in JobType}
    assert {
        "diff",
        "import_graph",
        "runtime",
        "category",
        "clamav",
        "yara",
        "dependency",
        "llm_package",
        "llm_file",
        "llm_summary",
        "routing",
    } <= {item.value for item in ReviewRunType}
    assert {"auto_approve", "request_changes", "policy_migrate"} <= {
        item.value for item in DecisionAction
    }
    assert [risk_rank(item) for item in RiskLevel] == [0, 2, 3, 4, 5]
    assert risk_rank(FindingSeverity.CRITICAL) == risk_rank(RiskLevel.CRITICAL)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ReviewStatus.SCANNING, ReviewStatus.APPROVED),
        (ReviewStatus.PENDING_REVIEW, ReviewStatus.CHANGES_REQUESTED),
        (ReviewStatus.PROCESSING_FAILED, ReviewStatus.SCANNING),
    ],
)
def test_advanced_review_transitions_are_explicit(
    current: ReviewStatus,
    target: ReviewStatus,
) -> None:
    validate_review_transition(current.value, target.value)


def test_changes_requested_is_terminal_for_the_candidate() -> None:
    assert ReviewStatus.CHANGES_REQUESTED in TERMINAL_REVIEW_STATUSES

    with pytest.raises(ArtifactStateError) as exc_info:
        validate_review_transition(
            ReviewStatus.CHANGES_REQUESTED.value,
            ReviewStatus.WITHDRAWN.value,
        )

    assert exc_info.value.code == ArtifactErrorCode.INVALID_REVIEW_TRANSITION.value


def test_decision_actions_map_to_review_targets() -> None:
    assert review_target_for_decision(DecisionAction.AUTO_REJECT) is ReviewStatus.REJECTED
    assert review_target_for_decision("auto_approve") is ReviewStatus.APPROVED
    assert review_target_for_decision("request_changes") is ReviewStatus.CHANGES_REQUESTED
    assert review_target_for_decision(DecisionAction.REVOKE) is None


def test_publication_transition_contract_remains_compatible() -> None:
    validate_publication_transition(
        PublicationStatus.PUBLISHED.value,
        PublicationStatus.REVOKING.value,
    )

    with pytest.raises(ArtifactStateError) as exc_info:
        validate_publication_transition(
            PublicationStatus.UNPUBLISHED.value,
            PublicationStatus.PUBLISHED.value,
        )

    assert exc_info.value.code == ArtifactErrorCode.INVALID_PUBLICATION_TRANSITION.value


def test_advanced_review_error_codes_are_stable() -> None:
    required = {
        "runtime_runner_unavailable",
        "runtime_dispatch_timeout",
        "runtime_result_invalid",
        "runtime_network_unverified",
        "astrbot_version_incompatible",
        "dependency_install_failed",
        "astrbot_core_dependency_conflict",
        "plugin_import_failed",
        "plugin_initialize_failed",
        "handler_registration_failed",
        "llm_tool_registration_failed",
        "llm_output_invalid",
        "llm_budget_exceeded",
        "diff_base_invalid",
        "diff_tree_changed",
        "import_graph_incomplete",
        "artifact_file_not_text",
        "artifact_file_too_large",
        "artifact_file_sha_changed",
        "comment_line_invalid",
        "comment_version_conflict",
        "review_policy_invalid",
        "review_policy_activation_conflict",
        "review_policy_unavailable",
        "artifact_policy_migration_forbidden",
        "malware_scan_unknown",
        "vulnerability_data_stale",
        "stable_release_correlation_required",
    }

    assert required <= {item.value for item in ArtifactErrorCode}
