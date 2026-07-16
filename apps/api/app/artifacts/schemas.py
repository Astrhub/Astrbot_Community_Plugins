from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class GithubArtifactSubmission(BaseModel):
    source_ref: str = Field(default="", max_length=200)

    @field_validator("source_ref")
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
