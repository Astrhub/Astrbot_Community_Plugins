from __future__ import annotations

import uuid
from enum import StrEnum


class ReviewStatus(StrEnum):
    QUARANTINED = "quarantined"
    PRECHECKING = "prechecking"
    SCANNING = "scanning"
    PENDING_REVIEW = "pending_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    PROCESSING_FAILED = "processing_failed"


class PublicationStatus(StrEnum):
    UNPUBLISHED = "unpublished"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    REVOKING = "revoking"
    REVOKED = "revoked"
    REVOKE_FAILED = "revoke_failed"


class RiskLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewRunType(StrEnum):
    PRECHECK = "precheck"
    STATIC = "static"
    DIFF = "diff"
    IMPORT_GRAPH = "import_graph"
    RUNTIME = "runtime"
    CATEGORY = "category"
    CLAMAV = "clamav"
    YARA = "yara"
    DEPENDENCY = "dependency"
    LLM_PACKAGE = "llm_package"
    LLM_FILE = "llm_file"
    LLM_SUMMARY = "llm_summary"
    ROUTING = "routing"


class ReviewRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    PRECHECK = "precheck"
    STATIC_SCAN = "static_scan"
    PUBLISH = "publish"
    REVOKE = "revoke"
    OUTBOX = "outbox"
    CLEANUP_ORPHAN = "cleanup_orphan"
    DIFF_GRAPH = "diff_graph"
    CLAMAV_SCAN = "clamav_scan"
    YARA_SCAN = "yara_scan"
    RUNTIME_DISPATCH = "runtime_dispatch"
    RUNTIME_COLLECT = "runtime_collect"
    DEPENDENCY_SCAN = "dependency_scan"
    CATEGORY = "category"
    LLM_PACKAGE = "llm_package"
    LLM_FILE = "llm_file"
    LLM_SUMMARY = "llm_summary"
    ROUTE_REVIEW = "route_review"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DecisionAction(StrEnum):
    AUTO_REJECT = "auto_reject"
    AUTO_APPROVE = "auto_approve"
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    RETRY_PUBLISH = "retry_publish"
    REVOKE = "revoke"
    EMERGENCY_OVERRIDE = "emergency_override"
    POLICY_MIGRATE = "policy_migrate"


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


class FindingSource(StrEnum):
    PRECHECK = "precheck"
    STATIC = "static"
    RUNTIME = "runtime"
    LLM = "llm"
    CLAMAV = "clamav"
    YARA = "yara"
    DEPENDENCY = "dependency"
    REVIEWER = "reviewer"
    SYSTEM = "system"


class DecisionSource(StrEnum):
    ADMIN = "admin"
    SYSTEM = "system"
    POLICY = "policy"


class ReviewPolicyStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class ReviewPolicyEventAction(StrEnum):
    CREATE = "create"
    VALIDATE = "validate"
    ACTIVATE = "activate"
    RETIRE = "retire"
    ROLLBACK = "rollback"


class RuntimeDispatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ArtifactGraphStatus(StrEnum):
    NOT_ANALYZED = "not_analyzed"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    NOT_APPLICABLE = "not_applicable"


class ArtifactFileChangeType(StrEnum):
    ADDED = "added"
    DELETED = "deleted"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"
    RENAMED = "renamed"


class ArtifactDependencyEdgeType(StrEnum):
    IMPORT = "import"
    FROM = "from"
    DYNAMIC = "dynamic"
    UNKNOWN = "unknown"


class ReviewCommentSide(StrEnum):
    BASE = "base"
    CURRENT = "current"


class ReviewCommentEventType(StrEnum):
    CREATE = "create"
    EDIT = "edit"
    REPLY = "reply"
    RESOLVE = "resolve"
    REOPEN = "reopen"
    AUTHOR_ADDRESSED = "author_addressed"


class ArtifactErrorCode(StrEnum):
    INVALID_REVIEW_TRANSITION = "invalid_review_transition"
    INVALID_PUBLICATION_TRANSITION = "invalid_publication_transition"
    IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"
    ARTIFACT_ALREADY_DECIDED = "artifact_already_decided"
    ARTIFACT_NOT_PENDING_REVIEW = "artifact_not_pending_review"
    SELF_APPROVAL_FORBIDDEN = "self_approval_forbidden"
    REPO_VERSION_CHANGED = "repo_version_changed"
    ARTIFACT_VERSION_CHANGED = "artifact_version_changed"
    REQUIRED_REVIEW_RUNS_MISSING = "required_review_runs_missing"
    ARTIFACT_NOT_CURRENT_RELEASE = "artifact_not_current_release"
    PUBLISHED_VERSION_CONFLICT = "published_version_conflict"
    DECISION_TARGET_MISMATCH = "decision_target_mismatch"
    RUNTIME_RUNNER_UNAVAILABLE = "runtime_runner_unavailable"
    RUNTIME_REQUEST_INVALID = "runtime_request_invalid"
    RUNTIME_DISPATCH_CONFLICT = "runtime_dispatch_conflict"
    RUNTIME_DISPATCH_TIMEOUT = "runtime_dispatch_timeout"
    RUNTIME_RESULT_INVALID = "runtime_result_invalid"
    RUNTIME_NETWORK_UNVERIFIED = "runtime_network_unverified"
    ASTRBOT_VERSION_INCOMPATIBLE = "astrbot_version_incompatible"
    DEPENDENCY_INSTALL_FAILED = "dependency_install_failed"
    ASTRBOT_CORE_DEPENDENCY_CONFLICT = "astrbot_core_dependency_conflict"
    PLUGIN_IMPORT_FAILED = "plugin_import_failed"
    PLUGIN_INITIALIZE_FAILED = "plugin_initialize_failed"
    HANDLER_REGISTRATION_FAILED = "handler_registration_failed"
    LLM_TOOL_REGISTRATION_FAILED = "llm_tool_registration_failed"
    LLM_OUTPUT_INVALID = "llm_output_invalid"
    LLM_BUDGET_EXCEEDED = "llm_budget_exceeded"
    DIFF_BASE_INVALID = "diff_base_invalid"
    DIFF_TREE_CHANGED = "diff_tree_changed"
    IMPORT_GRAPH_INCOMPLETE = "import_graph_incomplete"
    ARTIFACT_FILE_NOT_FOUND = "artifact_file_not_found"
    ARTIFACT_FILE_NOT_TEXT = "artifact_file_not_text"
    ARTIFACT_FILE_TOO_LARGE = "artifact_file_too_large"
    ARTIFACT_FILE_SHA_CHANGED = "artifact_file_sha_changed"
    ARTIFACT_FILE_INVALID_UTF8 = "artifact_file_invalid_utf8"
    ARTIFACT_FILE_CONTENT_UNAVAILABLE = "artifact_file_content_unavailable"
    ARTIFACT_CONTENT_RANGE_INVALID = "artifact_content_range_invalid"
    ARTIFACT_PAGE_INVALID = "artifact_page_invalid"
    ARTIFACT_DIFF_NOT_FOUND = "artifact_diff_not_found"
    ARTIFACT_DIFF_HUNK_INVALID = "artifact_diff_hunk_invalid"
    ARTIFACT_DIFF_HUNK_NOT_FOUND = "artifact_diff_hunk_not_found"
    ARTIFACT_DIFF_HUNK_UNAVAILABLE = "artifact_diff_hunk_unavailable"
    ARTIFACT_DIFF_TOO_LARGE = "artifact_diff_too_large"
    ARTIFACT_RESPONSE_TOO_LARGE = "artifact_response_too_large"
    COMMENT_LINE_INVALID = "comment_line_invalid"
    COMMENT_BODY_INVALID = "comment_body_invalid"
    COMMENT_ACTION_FORBIDDEN = "comment_action_forbidden"
    COMMENT_NOT_FOUND = "comment_not_found"
    COMMENT_SOURCE_INVALID = "comment_source_invalid"
    COMMENT_VERSION_CONFLICT = "comment_version_conflict"
    COMMENT_THREAD_LOCKED = "comment_thread_locked"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    SUPERSEDED_ARTIFACT_NOT_FOUND = "superseded_artifact_not_found"
    SUPERSEDED_ARTIFACT_INVALID = "superseded_artifact_invalid"
    SUPERSEDED_ARTIFACT_FORBIDDEN = "superseded_artifact_forbidden"
    RESUBMISSION_CONTENT_UNCHANGED = "resubmission_content_unchanged"
    HISTORY_CURSOR_INVALID = "history_cursor_invalid"
    HISTORY_PROJECTION_INVALID = "history_projection_invalid"
    FINDING_VERSION_CONFLICT = "finding_version_conflict"
    REVIEW_POLICY_INVALID = "review_policy_invalid"
    REVIEW_POLICY_VERSION_CONFLICT = "review_policy_version_conflict"
    REVIEW_POLICY_ACTIVATION_CONFLICT = "review_policy_activation_conflict"
    REVIEW_POLICY_UNAVAILABLE = "review_policy_unavailable"
    ARTIFACT_POLICY_SNAPSHOT_CONFLICT = "artifact_policy_snapshot_conflict"
    ARTIFACT_POLICY_MIGRATION_FORBIDDEN = "artifact_policy_migration_forbidden"
    MALWARE_SCAN_UNKNOWN = "malware_scan_unknown"
    VULNERABILITY_DATA_STALE = "vulnerability_data_stale"
    STABLE_RELEASE_CORRELATION_REQUIRED = "stable_release_correlation_required"


REVIEW_TRANSITIONS: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.QUARANTINED: frozenset(
        {ReviewStatus.PRECHECKING, ReviewStatus.REJECTED, ReviewStatus.WITHDRAWN}
    ),
    ReviewStatus.PRECHECKING: frozenset(
        {ReviewStatus.SCANNING, ReviewStatus.REJECTED, ReviewStatus.PROCESSING_FAILED}
    ),
    ReviewStatus.SCANNING: frozenset(
        {
            ReviewStatus.PENDING_REVIEW,
            ReviewStatus.APPROVED,
            ReviewStatus.REJECTED,
            ReviewStatus.PROCESSING_FAILED,
        }
    ),
    ReviewStatus.PENDING_REVIEW: frozenset(
        {
            ReviewStatus.CHANGES_REQUESTED,
            ReviewStatus.APPROVED,
            ReviewStatus.REJECTED,
            ReviewStatus.WITHDRAWN,
        }
    ),
    ReviewStatus.CHANGES_REQUESTED: frozenset(),
    ReviewStatus.APPROVED: frozenset(),
    ReviewStatus.REJECTED: frozenset(),
    ReviewStatus.WITHDRAWN: frozenset(),
    ReviewStatus.PROCESSING_FAILED: frozenset(
        {
            ReviewStatus.PRECHECKING,
            ReviewStatus.SCANNING,
            ReviewStatus.REJECTED,
            ReviewStatus.WITHDRAWN,
        }
    ),
}

PUBLICATION_TRANSITIONS: dict[PublicationStatus, frozenset[PublicationStatus]] = {
    PublicationStatus.UNPUBLISHED: frozenset({PublicationStatus.PUBLISHING}),
    PublicationStatus.PUBLISHING: frozenset(
        {PublicationStatus.PUBLISHED, PublicationStatus.PUBLISH_FAILED}
    ),
    PublicationStatus.PUBLISHED: frozenset({PublicationStatus.REVOKING}),
    PublicationStatus.PUBLISH_FAILED: frozenset({PublicationStatus.PUBLISHING}),
    PublicationStatus.REVOKING: frozenset(
        {PublicationStatus.REVOKED, PublicationStatus.REVOKE_FAILED}
    ),
    PublicationStatus.REVOKED: frozenset(),
    PublicationStatus.REVOKE_FAILED: frozenset({PublicationStatus.REVOKING}),
}

SEVERITY_ORDER = {
    FindingSeverity.INFO: 0,
    FindingSeverity.LOW: 1,
    FindingSeverity.MEDIUM: 2,
    FindingSeverity.HIGH: 3,
    FindingSeverity.CRITICAL: 4,
}

TERMINAL_REVIEW_STATUSES = frozenset(
    {
        ReviewStatus.CHANGES_REQUESTED,
        ReviewStatus.APPROVED,
        ReviewStatus.REJECTED,
        ReviewStatus.WITHDRAWN,
    }
)

DECISION_REVIEW_TARGETS = {
    DecisionAction.AUTO_REJECT: ReviewStatus.REJECTED,
    DecisionAction.AUTO_APPROVE: ReviewStatus.APPROVED,
    DecisionAction.APPROVE: ReviewStatus.APPROVED,
    DecisionAction.REJECT: ReviewStatus.REJECTED,
    DecisionAction.REQUEST_CHANGES: ReviewStatus.CHANGES_REQUESTED,
}


class ArtifactStateError(ValueError):
    def __init__(self, code: str | ArtifactErrorCode, current: str, target: str) -> None:
        normalized_code = ArtifactErrorCode(code).value
        super().__init__(f"{normalized_code}: cannot change {current} to {target}")
        self.code = normalized_code
        self.current = current
        self.target = target


def validate_review_transition(current: str, target: str) -> None:
    _validate_transition(
        ReviewStatus(current),
        ReviewStatus(target),
        REVIEW_TRANSITIONS,
        ArtifactErrorCode.INVALID_REVIEW_TRANSITION,
    )


def validate_publication_transition(current: str, target: str) -> None:
    _validate_transition(
        PublicationStatus(current),
        PublicationStatus(target),
        PUBLICATION_TRANSITIONS,
        ArtifactErrorCode.INVALID_PUBLICATION_TRANSITION,
    )


def highest_risk(values: list[str]) -> RiskLevel:
    if not values:
        return RiskLevel.NONE
    severity = max(
        (FindingSeverity(value) for value in values),
        key=lambda item: SEVERITY_ORDER[item],
    )
    return RiskLevel.NONE if severity is FindingSeverity.INFO else RiskLevel(severity.value)


def risk_rank(value: str | RiskLevel | FindingSeverity) -> int:
    normalized = str(value)
    if normalized == RiskLevel.NONE.value:
        return 0
    return SEVERITY_ORDER[FindingSeverity(normalized)] + 1


def review_target_for_decision(action: str | DecisionAction) -> ReviewStatus | None:
    return DECISION_REVIEW_TARGETS.get(DecisionAction(action))


def new_domain_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _validate_transition(
    current: StrEnum,
    target: StrEnum,
    allowed: dict,
    code: ArtifactErrorCode,
) -> None:
    if current == target:
        return
    if target not in allowed[current]:
        raise ArtifactStateError(code, current.value, target.value)
