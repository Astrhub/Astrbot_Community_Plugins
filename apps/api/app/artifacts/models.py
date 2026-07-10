from __future__ import annotations

import uuid
from enum import StrEnum


class ReviewStatus(StrEnum):
    QUARANTINED = "quarantined"
    PRECHECKING = "prechecking"
    SCANNING = "scanning"
    PENDING_REVIEW = "pending_review"
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
    RUNTIME = "runtime"
    LLM_PACKAGE = "llm_package"
    LLM_FILE = "llm_file"
    LLM_SUMMARY = "llm_summary"


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


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DecisionAction(StrEnum):
    AUTO_REJECT = "auto_reject"
    APPROVE = "approve"
    REJECT = "reject"
    RETRY_PUBLISH = "retry_publish"
    REVOKE = "revoke"
    EMERGENCY_OVERRIDE = "emergency_override"


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


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
            ReviewStatus.REJECTED,
            ReviewStatus.PROCESSING_FAILED,
        }
    ),
    ReviewStatus.PENDING_REVIEW: frozenset(
        {ReviewStatus.APPROVED, ReviewStatus.REJECTED, ReviewStatus.WITHDRAWN}
    ),
    ReviewStatus.APPROVED: frozenset(),
    ReviewStatus.REJECTED: frozenset(),
    ReviewStatus.WITHDRAWN: frozenset(),
    ReviewStatus.PROCESSING_FAILED: frozenset(
        {ReviewStatus.PRECHECKING, ReviewStatus.REJECTED, ReviewStatus.WITHDRAWN}
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


class ArtifactStateError(ValueError):
    def __init__(self, code: str, current: str, target: str) -> None:
        super().__init__(f"{code}: cannot change {current} to {target}")
        self.code = code
        self.current = current
        self.target = target


def validate_review_transition(current: str, target: str) -> None:
    _validate_transition(
        ReviewStatus(current),
        ReviewStatus(target),
        REVIEW_TRANSITIONS,
        "invalid_review_transition",
    )


def validate_publication_transition(current: str, target: str) -> None:
    _validate_transition(
        PublicationStatus(current),
        PublicationStatus(target),
        PUBLICATION_TRANSITIONS,
        "invalid_publication_transition",
    )


def highest_risk(values: list[str]) -> RiskLevel:
    if not values:
        return RiskLevel.NONE
    severity = max(
        (FindingSeverity(value) for value in values),
        key=lambda item: SEVERITY_ORDER[item],
    )
    return RiskLevel.NONE if severity is FindingSeverity.INFO else RiskLevel(severity.value)


def new_domain_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _validate_transition(current: StrEnum, target: StrEnum, allowed: dict, code: str) -> None:
    if current == target:
        return
    if target not in allowed[current]:
        raise ArtifactStateError(code, current.value, target.value)
