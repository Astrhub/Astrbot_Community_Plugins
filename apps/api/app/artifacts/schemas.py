from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .policy import ReviewPolicyV1


ArtifactReviewStatus = Literal[
    "quarantined",
    "prechecking",
    "scanning",
    "pending_review",
    "changes_requested",
    "approved",
    "rejected",
    "withdrawn",
    "processing_failed",
]
ArtifactPublicationStatus = Literal[
    "unpublished",
    "publishing",
    "published",
    "publish_failed",
    "revoking",
    "revoked",
    "revoke_failed",
]
ArtifactRiskLevel = Literal["none", "low", "medium", "high", "critical"]
ReviewRunType = Literal[
    "precheck",
    "static",
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
]
ReviewRunStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
]


class PublicResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicArtifact(PublicResponseModel):
    id: str
    plugin_id: str
    plugin_name: str = ""
    plugin_repo: str = ""
    version: str = ""
    normalized_version: str = ""
    repo_version: str = ""
    published_version: str = ""
    source_type: Literal["upload", "github"]
    source_repo: str = ""
    source_ref: str = ""
    source_commit_sha: str = ""
    archive_sha256: str = ""
    tree_sha256: str = ""
    size_bytes: int = Field(default=0, ge=0)
    review_status: ArtifactReviewStatus
    publication_status: ArtifactPublicationStatus
    risk_level: ArtifactRiskLevel
    rejection_code: str = ""
    download_url: str | None = None
    submitted_by: str | None = None
    owner_user_id: str | None = None
    suggested_category: str = ""
    category_confidence: float | None = None
    category_reason: str = ""
    policy_version_id: str | None = None
    base_artifact_id: str | None = None
    supersedes_artifact_id: str | None = None
    review_coverage: dict[str, Any] = Field(default_factory=dict)
    automated_review_completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    reviewed_at: datetime | None = None
    published_at: datetime | None = None
    revoked_at: datetime | None = None


class PublicReviewRun(PublicResponseModel):
    id: str
    artifact_id: str
    type: ReviewRunType
    status: ReviewRunStatus
    attempt: int = Field(default=1, ge=1)
    advisory: bool
    label: Literal["自动审查建议", "确定性检查"]
    summary: str = ""
    error_code: str = ""
    model: str = ""
    ruleset_version: str = ""
    tool_name: str = ""
    tool_version: str = ""
    policy_version_id: str | None = None
    input_sha256: str = ""
    output_sha256: str = ""
    coverage: dict[str, Any] = Field(default_factory=dict)
    prompt_version: str = ""
    result_schema_version: str = ""
    astrbot_version: str = ""
    python_version: str = ""
    platform: str = ""
    dependency_snapshot_sha256: str = ""
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class PublicReviewFinding(PublicResponseModel):
    id: str
    artifact_id: str
    run_id: str
    fingerprint: str
    rule_id: str = ""
    file_path: str = ""
    line_start: int | None = None
    line_end: int | None = None
    severity: Literal["info", "low", "medium", "high", "critical"]
    category: str = ""
    message: str
    suggestion: str = ""
    evidence_excerpt: str = ""
    confidence: float | None = None
    status: Literal["open", "accepted", "resolved", "false_positive"] = "open"
    source: str = ""
    deterministic: bool = True
    advisory: bool
    label: Literal["自动审查建议", "确定性检查"]
    affects_current_release: bool = False
    version: int = Field(default=1, ge=1)
    created_at: datetime
    status_updated_at: datetime | None = None


class PublicReviewDecision(PublicResponseModel):
    id: str
    artifact_id: str
    action: str
    from_status: str
    to_status: str
    reason: str = ""
    reviewer_nickname: str = ""
    policy_version: str = ""
    policy_version_id: str | None = None
    source: Literal["admin", "system", "policy"] = "admin"
    input_run_ids: list[str] = Field(default_factory=list)
    input_fingerprints: list[str] = Field(default_factory=list)
    coverage_sha256: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ArtifactDetailResponse(PublicResponseModel):
    artifact: PublicArtifact
    runs: list[PublicReviewRun]
    findings: list[PublicReviewFinding]
    decisions: list[PublicReviewDecision]


class ArtifactEnvelope(PublicResponseModel):
    artifact: PublicArtifact


class ReviewRunListResponse(PublicResponseModel):
    items: list[PublicReviewRun]


class ReviewFindingListResponse(PublicResponseModel):
    items: list[PublicReviewFinding]


class PublicReviewHistoryEvent(PublicResponseModel):
    id: str
    type: Literal[
        "artifact_submitted",
        "comment_event",
        "decision",
        "finding",
        "finding_event",
        "policy_event",
        "publication_publish_failed",
        "publication_published",
        "publication_revoke_failed",
        "publication_revoked",
        "run",
    ]
    occurred_at: datetime
    source: str
    actor_nickname: str = ""
    actor_role: str
    idempotency_key: str = ""
    policy_version_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ReviewHistoryResponse(PublicResponseModel):
    artifact_id: str
    items: list[PublicReviewHistoryEvent]
    has_more: bool
    next_cursor: str | None = None


class StableRiskEvidenceResponse(PublicResponseModel):
    kind: Literal["path_sha", "dependency", "fingerprint", "admin_confirmation"]
    deterministic: bool
    candidate_artifact_id: str
    stable_artifact_id: str
    finding_id: str
    fingerprint: str = ""
    path: str = ""
    file_sha256: str = ""
    package_name: str = ""
    package_version: str = ""
    advisory_id: str = ""
    tool_name: str = ""
    tool_version: str = ""
    ruleset_version: str = ""
    confirmed_by_nickname: str = ""
    reason: str = ""


class StableRiskResponse(PublicResponseModel):
    candidate_artifact_id: str
    finding_id: str
    affects_current_release: Literal[True]
    correlation: StableRiskEvidenceResponse
    stable_artifact: PublicArtifact


class PublicArtifactFile(PublicResponseModel):
    id: str
    artifact_id: str
    path: str
    language: str = ""
    mime_type: str = "application/octet-stream"
    sha256: str
    size_bytes: int = Field(ge=0)
    line_count: int | None = Field(default=None, ge=0)
    is_text: bool
    is_entrypoint: bool = False
    is_reachable: bool = False
    graph_status: Literal["not_analyzed", "complete", "incomplete", "not_applicable"]
    content_available: bool


class ArtifactFileListResponse(PublicResponseModel):
    artifact_id: str
    tree_sha256: str
    items: list[PublicArtifactFile]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ArtifactTextLine(PublicResponseModel):
    number: int = Field(ge=1)
    text: str


class ArtifactFileContentResponse(PublicResponseModel):
    artifact_id: str
    tree_sha256: str
    file: PublicArtifactFile
    encoding: Literal["utf-8"]
    start_line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    total_lines: int = Field(ge=0)
    truncated: bool
    lines: list[ArtifactTextLine]


class PublicArtifactDiffStats(PublicResponseModel):
    base_size_bytes: int | None = Field(default=None, ge=0)
    current_size_bytes: int | None = Field(default=None, ge=0)
    base_line_count: int | None = Field(default=None, ge=0)
    current_line_count: int | None = Field(default=None, ge=0)
    forced_review: bool = False
    binary: bool = False
    added_lines: int = Field(default=0, ge=0)
    deleted_lines: int = Field(default=0, ge=0)
    hunk_count: int = Field(default=0, ge=0)
    hunks_complete: bool = True
    hunks_omitted: int = Field(default=0, ge=0)
    hunks_omitted_reason: str = ""
    hunks_truncated: bool = False


class PublicArtifactDiff(PublicResponseModel):
    id: str
    artifact_id: str
    base_artifact_id: str | None = None
    base_file_id: str | None = None
    current_file_id: str | None = None
    path: str
    base_path: str = ""
    change_type: Literal["added", "deleted", "modified", "unchanged", "renamed"]
    base_sha256: str | None = None
    current_sha256: str | None = None
    base_tree_sha256: str | None = None
    current_tree_sha256: str
    stats: PublicArtifactDiffStats = Field(default_factory=PublicArtifactDiffStats)
    has_hunks: bool
    created_at: datetime | None = None


class ArtifactDiffListResponse(PublicResponseModel):
    artifact_id: str
    tree_sha256: str
    items: list[PublicArtifactDiff]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ArtifactDiffLine(PublicResponseModel):
    kind: Literal["context", "delete", "add"]
    prefix: Literal[" ", "-", "+"]
    text: str
    newline: Literal["none", "lf", "crlf", "cr"]
    old_line: int | None = Field(default=None, ge=1)
    new_line: int | None = Field(default=None, ge=1)


class ArtifactDiffHunk(PublicResponseModel):
    id: str
    header: str
    old_start: int = Field(ge=0)
    old_lines: int = Field(ge=0)
    new_start: int = Field(ge=0)
    new_lines: int = Field(ge=0)
    lines: list[ArtifactDiffLine]


class ArtifactDiffContentResponse(PublicResponseModel):
    artifact_id: str
    tree_sha256: str
    diff: PublicArtifactDiff
    hunks_available: bool
    unavailable_reason: str = ""
    schema_version: str = ""
    tool_version: str = ""
    context_lines: int = Field(default=0, ge=0, le=20)
    truncated: bool
    omitted_hunks: int = Field(default=0, ge=0)
    hunks: list[ArtifactDiffHunk]


class PublicReviewCommentEvent(PublicResponseModel):
    id: str
    thread_id: str
    type: Literal["create", "edit", "reply", "resolve", "reopen", "author_addressed"]
    body: str = ""
    actor_nickname: str = ""
    actor_role: Literal["author", "admin", "core_admin", "system"]
    expected_version: int = Field(ge=0)
    resulting_version: int = Field(ge=1)
    created_at: datetime


class PublicReviewComment(PublicResponseModel):
    id: str
    artifact_id: str
    source_thread_id: str | None = None
    file_id: str | None = None
    file_path: str
    file_sha256: str
    side: Literal["base", "current"]
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    body: str
    reviewer_nickname: str = ""
    reviewer_role: Literal["admin", "core_admin"]
    resolved: bool
    resolved_by_nickname: str = ""
    locked_at: datetime | None = None
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    event_count: int = Field(ge=0)
    events_truncated: bool
    events: list[PublicReviewCommentEvent]


class ReviewCommentListResponse(PublicResponseModel):
    artifact_id: str
    items: list[PublicReviewComment]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ReviewCommentEnvelope(PublicResponseModel):
    comment: PublicReviewComment


class ReviewCommentCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(min_length=1, max_length=200)
    side: Literal["base", "current"]
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=10_000)
    diff_id: str | None = Field(default=None, max_length=200)
    hunk_id: str | None = Field(default=None, max_length=200)
    source_thread_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("file_id")
    @classmethod
    def clean_file_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("diff_id", "hunk_id", "source_thread_id")
    @classmethod
    def clean_optional_ids(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("idempotency_key")
    @classmethod
    def clean_create_key(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_anchor(self) -> ReviewCommentCreatePayload:
        if self.line_end < self.line_start or bool(self.diff_id) != bool(self.hunk_id):
            raise ValueError("invalid comment anchor")
        return self


class ReviewCommentBodyMutationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=10_000)
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def clean_body_key(cls, value: str) -> str:
        return value.strip()


class ReviewCommentAddressedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    body: str = Field(default="", max_length=10_000)
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def clean_addressed_key(cls, value: str) -> str:
        return value.strip()


class ReviewCommentStateMutationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def clean_state_key(cls, value: str) -> str:
        return value.strip()


class GithubArtifactSubmission(BaseModel):
    source_ref: str = Field(default="", max_length=200)
    supersedes_artifact_id: str = Field(default="", max_length=200)

    @field_validator("source_ref", "supersedes_artifact_id")
    @classmethod
    def clean_source_ref(cls, value: str) -> str:
        return value.strip()


class ArtifactDecisionPayload(BaseModel):
    reason: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("reason", "idempotency_key")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()


class StableRiskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    confirm_affects_current_release: bool = False
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("reason", "idempotency_key")
    @classmethod
    def clean_stable_risk_text(cls, value: str) -> str:
        return " ".join(value.split()) if value else ""


class PluginRegistrationPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=120)
    desc: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=120)
    repo: str = Field(min_length=1, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=20)
    category: Literal[
        "ai_tools",
        "entertainment",
        "integrations",
        "productivity",
        "utilities",
        "other",
    ] = "other"

    @field_validator("name", "display_name", "desc", "author", "repo")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        tags: list[str] = []
        for item in value:
            tag = str(item or "").strip()[:40]
            if tag and tag not in tags:
                tags.append(tag)
        return tags


class ReviewPolicyValidationIssue(PublicResponseModel):
    path: str = Field(max_length=300)
    code: str = Field(max_length=100)
    message: str = Field(max_length=300)


class ReviewPolicyValidationSummary(PublicResponseModel):
    valid: bool = False
    schema_version: str = Field(default="", max_length=32)
    policy_sha256: str = Field(default="", max_length=64)
    readiness_checked: bool = False
    issues: list[ReviewPolicyValidationIssue] = Field(default_factory=list, max_length=100)


class PublicReviewPolicy(PublicResponseModel):
    id: str
    version: str
    schema_version: str
    status: Literal["draft", "active", "retired"]
    is_default: bool
    policy: ReviewPolicyV1
    policy_sha256: str
    base_policy_id: str | None = None
    created_by_nickname: str = ""
    validation_summary: ReviewPolicyValidationSummary
    validated_at: datetime | None = None
    activated_at: datetime | None = None
    retired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ReviewPolicyDiff(PublicResponseModel):
    redacted: Literal[True] = True
    before_sha256: str = ""
    after_sha256: str = ""
    added_paths: list[str] = Field(default_factory=list, max_length=200)
    removed_paths: list[str] = Field(default_factory=list, max_length=200)
    changed_paths: list[str] = Field(default_factory=list, max_length=200)
    path_count: int = Field(default=0, ge=0)
    truncated: bool = False


class ReviewPolicyEnvelope(PublicResponseModel):
    policy: PublicReviewPolicy
    diff: ReviewPolicyDiff


class ReviewPolicyListResponse(PublicResponseModel):
    items: list[PublicReviewPolicy]
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class ActiveReviewPolicyResponse(PublicResponseModel):
    policy: PublicReviewPolicy | None


class ReviewPolicyDraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    policy: dict[str, Any]
    reason: str = Field(default="", max_length=2000)
    base_policy_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("reason", "idempotency_key")
    @classmethod
    def clean_policy_draft_text(cls, value: str) -> str:
        return " ".join(value.split()) if value else ""

    @field_validator("base_policy_id")
    @classmethod
    def clean_base_policy_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ReviewPolicyValidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("reason", "idempotency_key")
    @classmethod
    def clean_policy_validation_text(cls, value: str) -> str:
        return " ".join(value.split()) if value else ""


class ReviewPolicyTransitionPayload(ReviewPolicyValidatePayload):
    reason: str = Field(min_length=1, max_length=2000)


class ReviewHealthComponent(PublicResponseModel):
    enabled: bool
    configured: bool
    ready: bool
    degraded: bool
    status: Literal["disabled", "ready", "degraded"]
    reasons: list[str] = Field(default_factory=list, max_length=20)


class ReviewHealthComponents(PublicResponseModel):
    runtime: ReviewHealthComponent
    llm: ReviewHealthComponent
    clamav: ReviewHealthComponent
    yara: ReviewHealthComponent
    dependency: ReviewHealthComponent
    policy: ReviewHealthComponent


class ReviewAggregateHealth(PublicResponseModel):
    enabled: bool
    configured: bool
    ready: bool
    degraded: bool
    auto_approve_enabled: bool
    policy_auto_approve_enabled: bool = False
    auto_approve_effective: bool = False
    components: ReviewHealthComponents


class ReviewWorkerHealth(PublicResponseModel):
    kind: Literal["artifact_worker", "runtime_runner"]
    status: Literal["ready", "degraded"]
    ready: bool
    degraded: bool
    live_instances: int = Field(ge=0)
    stale_instances: int = Field(ge=0)
    capacity: int = Field(ge=0)
    active_count: int = Field(ge=0)
    last_observed_at: datetime | None = None
    reasons: list[str] = Field(default_factory=list, max_length=20)


class ReviewToolHealth(PublicResponseModel):
    name: Literal["policy", "runtime", "llm", "clamav", "yara", "dependency"]
    enabled: bool
    configured: bool
    ready: bool
    degraded: bool
    status: Literal["disabled", "ready", "degraded"]
    reasons: list[str] = Field(default_factory=list, max_length=20)
    version: str = Field(default="", max_length=160)
    data_updated_at: datetime | None = None
    freshness: Literal["current", "stale", "unknown", "not_applicable"]
    observed_at: datetime | None = None


class ReviewOperationsHealth(PublicResponseModel):
    review: ReviewAggregateHealth
    workers: list[ReviewWorkerHealth] = Field(max_length=2)
    tools: list[ReviewToolHealth] = Field(max_length=6)


class ReviewQueueMetric(PublicResponseModel):
    job_type: Literal[
        "precheck",
        "static_scan",
        "publish",
        "revoke",
        "outbox",
        "cleanup_orphan",
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
    ]
    status: Literal["queued", "running"]
    count: int = Field(ge=0)


class ReviewStageMetric(PublicResponseModel):
    run_type: ReviewRunType
    sample_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    average_duration_ms: float = Field(ge=0, allow_inf_nan=False)
    p95_duration_ms: float = Field(ge=0, allow_inf_nan=False)


class ReviewManualWaitMetric(PublicResponseModel):
    waiting_count: int = Field(ge=0)
    average_wait_seconds: float = Field(ge=0, allow_inf_nan=False)
    max_wait_seconds: float = Field(ge=0, allow_inf_nan=False)


class ReviewRoutingMetric(PublicResponseModel):
    action: Literal[
        "auto_reject",
        "auto_approve",
        "approve",
        "reject",
        "request_changes",
        "retry_publish",
        "revoke",
        "emergency_override",
        "policy_migrate",
    ]
    source: Literal["admin", "system", "policy"]
    count: int = Field(ge=0)


class ReviewRevokeMetric(PublicResponseModel):
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    count: int = Field(ge=0)


class ReviewOperationsMetrics(PublicResponseModel):
    available: bool
    window_started_at: datetime
    collected_at: datetime
    queue: list[ReviewQueueMetric]
    stages: list[ReviewStageMetric]
    manual_wait: ReviewManualWaitMetric
    routing: list[ReviewRoutingMetric]
    revoke: list[ReviewRevokeMetric]


class ReviewOperationsResponse(PublicResponseModel):
    health: ReviewOperationsHealth
    metrics: ReviewOperationsMetrics
