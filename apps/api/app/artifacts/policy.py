from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal, Mapping, Self

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

POLICY_SCHEMA_VERSION = "1"

_CONFIG_REFERENCE = re.compile(r"^(?:config|env|secret):[A-Za-z][A-Za-z0-9._/-]{0,127}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROFILE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ReviewPolicyStage(StrEnum):
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


class ToolFailureAction(StrEnum):
    MANUAL_REVIEW = "manual_review"
    FAIL_CLOSED = "fail_closed"


class PolicySeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PluginCategory(StrEnum):
    AI_TOOLS = "ai_tools"
    ENTERTAINMENT = "entertainment"
    INTEGRATIONS = "integrations"
    PRODUCTIVITY = "productivity"
    UTILITIES = "utilities"
    OTHER = "other"


_STAGE_ORDER = {stage: index for index, stage in enumerate(ReviewPolicyStage)}
_CATEGORY_ORDER = {category: index for index, category in enumerate(PluginCategory)}
_SEVERITY_ORDER = {severity: index for index, severity in enumerate(PolicySeverity)}


class FrozenPolicyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class RuntimeTarget(FrozenPolicyModel):
    astrbot: str = Field(min_length=1, max_length=64)
    python: str = Field(min_length=1, max_length=16)

    @field_validator("astrbot")
    @classmethod
    def validate_astrbot_version(cls, value: str) -> str:
        normalized = _exact_version(value, "AstrBot")
        if len(Version(normalized).release) < 3:
            raise ValueError("AstrBot version must include major, minor, and patch")
        return normalized

    @field_validator("python")
    @classmethod
    def validate_python_version(cls, value: str) -> str:
        raw = value.strip()
        if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", raw):
            raise ValueError("Python version must be an exact major.minor or major.minor.patch")
        normalized = _exact_version(raw, "Python")
        parsed = Version(normalized)
        if parsed.is_prerelease or parsed.is_postrelease or parsed.is_devrelease:
            raise ValueError("Python runtime version must be a final release")
        return normalized


class ResourceLimits(FrozenPolicyModel):
    cpu: float = Field(gt=0, le=16, allow_inf_nan=False)
    memory_mb: int = Field(ge=128, le=32768)
    pids: int = Field(ge=16, le=4096)
    timeout_seconds: int = Field(ge=10, le=3600)
    disk_mb: int = Field(default=2048, ge=128, le=32768)
    tmpfs_mb: int = Field(default=512, ge=16, le=8192)
    max_log_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)

    @model_validator(mode="after")
    def validate_memory_limits(self) -> Self:
        if self.tmpfs_mb > self.memory_mb:
            raise ValueError("tmpfs_mb cannot exceed memory_mb")
        return self


class NetworkProfiles(FrozenPolicyModel):
    install: str = Field(min_length=1, max_length=64)
    smoke: str = Field(default="none", min_length=1, max_length=64)
    on_unverified: ToolFailureAction = ToolFailureAction.FAIL_CLOSED

    @field_validator("install", "smoke")
    @classmethod
    def validate_profile_reference(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _PROFILE_IDENTIFIER.fullmatch(normalized):
            raise ValueError("network profile must be a versioned identifier")
        return normalized


class LlmPolicy(FrozenPolicyModel):
    enabled: bool = False
    provider_config_ref: str = "config:llm-default"
    model: str = Field(default="", max_length=128)
    prompt_version: str = Field(default="v1", min_length=1, max_length=64)
    max_tokens: int = Field(default=0, ge=0, le=1_000_000)
    max_files: int = Field(default=20, ge=1, le=500)
    max_file_bytes: int = Field(default=262_144, ge=1024, le=2_097_152)
    timeout_seconds: int = Field(default=90, ge=5, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("provider_config_ref")
    @classmethod
    def validate_provider_reference(cls, value: str) -> str:
        return _config_reference(value)

    @field_validator("model", "prompt_version")
    @classmethod
    def validate_public_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not _SAFE_IDENTIFIER.fullmatch(normalized):
            raise ValueError("value must be a public identifier, not a credential or URL")
        return normalized

    @model_validator(mode="after")
    def validate_enabled_budget(self) -> Self:
        if self.enabled:
            if not self.model:
                raise ValueError("enabled LLM review requires a model identifier")
            if self.max_tokens <= 0:
                raise ValueError("enabled LLM review requires a positive token budget")
        elif self.model or self.max_tokens:
            raise ValueError("disabled LLM review cannot carry an active model or token budget")
        return self


class MalwarePolicy(FrozenPolicyModel):
    clamav: bool = False
    clamav_config_ref: str = "config:clamav-default"
    yara_ruleset: str | None = Field(default=None, max_length=128)
    max_database_age_hours: int = Field(default=24, ge=1, le=720)
    on_unknown: ToolFailureAction = ToolFailureAction.FAIL_CLOSED

    @field_validator("clamav_config_ref")
    @classmethod
    def validate_clamav_reference(cls, value: str) -> str:
        return _config_reference(value)

    @field_validator("yara_ruleset")
    @classmethod
    def validate_yara_ruleset(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not _SAFE_IDENTIFIER.fullmatch(normalized):
            raise ValueError("YARA ruleset must be a versioned public identifier")
        return normalized


class DependencyPolicy(FrozenPolicyModel):
    enabled: bool = True
    advisory_config_ref: str = "config:dependency-default"
    max_severity: PolicySeverity = PolicySeverity.HIGH
    max_data_age_hours: int = Field(default=24, ge=1, le=720)
    on_unavailable: ToolFailureAction = ToolFailureAction.MANUAL_REVIEW
    allow_direct_urls: bool = False
    allow_vcs: bool = False

    @field_validator("advisory_config_ref")
    @classmethod
    def validate_advisory_reference(cls, value: str) -> str:
        return _config_reference(value)

    @model_validator(mode="after")
    def validate_severity_threshold(self) -> Self:
        if self.max_severity is PolicySeverity.INFO:
            raise ValueError("dependency max_severity must be low or higher")
        return self


class CategoryPolicy(FrozenPolicyModel):
    enabled: bool = False
    provider_config_ref: str = "config:category-default"
    model: str = Field(default="", max_length=128)
    minimum_confidence: float = Field(default=0.8, ge=0, le=1, allow_inf_nan=False)
    allowed_categories: tuple[PluginCategory, ...] = tuple(PluginCategory)
    default_category: PluginCategory = PluginCategory.OTHER
    max_input_chars: int = Field(default=32_000, ge=1024, le=200_000)

    @field_validator("provider_config_ref")
    @classmethod
    def validate_provider_reference(cls, value: str) -> str:
        return _config_reference(value)

    @field_validator("model")
    @classmethod
    def validate_model_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not _SAFE_IDENTIFIER.fullmatch(normalized):
            raise ValueError("category model must be a public identifier")
        return normalized

    @field_validator("allowed_categories")
    @classmethod
    def normalize_allowed_categories(
        cls,
        value: tuple[PluginCategory, ...],
    ) -> tuple[PluginCategory, ...]:
        if not value:
            raise ValueError("allowed_categories cannot be empty")
        if len(set(value)) != len(value):
            raise ValueError("allowed_categories cannot contain duplicates")
        return tuple(sorted(value, key=_CATEGORY_ORDER.__getitem__))

    @model_validator(mode="after")
    def validate_category_configuration(self) -> Self:
        if self.default_category not in self.allowed_categories:
            raise ValueError("default_category must be included in allowed_categories")
        if self.enabled and not self.model:
            raise ValueError("enabled category suggestion requires a model identifier")
        if not self.enabled and self.model:
            raise ValueError("disabled category suggestion cannot carry an active model")
        return self


class RoutingPolicy(FrozenPolicyModel):
    auto_approve: bool = False
    manual_review_at: PolicySeverity = PolicySeverity.LOW
    deterministic_reject_at: PolicySeverity = PolicySeverity.CRITICAL
    degraded_action: ToolFailureAction = ToolFailureAction.MANUAL_REVIEW
    require_complete_coverage: bool = True

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        if _SEVERITY_ORDER[self.manual_review_at] > _SEVERITY_ORDER[PolicySeverity.MEDIUM]:
            raise ValueError("manual_review_at cannot be higher than medium")
        if _SEVERITY_ORDER[self.deterministic_reject_at] < _SEVERITY_ORDER[PolicySeverity.HIGH]:
            raise ValueError("deterministic_reject_at cannot be lower than high")
        if _SEVERITY_ORDER[self.manual_review_at] >= _SEVERITY_ORDER[self.deterministic_reject_at]:
            raise ValueError("manual review threshold must be lower than reject threshold")
        return self


class ReviewPolicyV1(FrozenPolicyModel):
    schema_version: Literal[POLICY_SCHEMA_VERSION]
    required_stages: tuple[ReviewPolicyStage, ...]
    runtime_targets: tuple[RuntimeTarget, ...] = ()
    limits: ResourceLimits
    network_profiles: NetworkProfiles
    llm: LlmPolicy
    malware: MalwarePolicy
    dependency: DependencyPolicy
    category: CategoryPolicy = Field(default_factory=CategoryPolicy)
    routing: RoutingPolicy

    @field_validator("required_stages")
    @classmethod
    def normalize_required_stages(
        cls,
        value: tuple[ReviewPolicyStage, ...],
    ) -> tuple[ReviewPolicyStage, ...]:
        if not value:
            raise ValueError("required_stages cannot be empty")
        if len(set(value)) != len(value):
            raise ValueError("required_stages cannot contain duplicates")
        return tuple(sorted(value, key=_STAGE_ORDER.__getitem__))

    @field_validator("runtime_targets")
    @classmethod
    def normalize_runtime_targets(
        cls,
        value: tuple[RuntimeTarget, ...],
    ) -> tuple[RuntimeTarget, ...]:
        keys = [(Version(target.astrbot), Version(target.python)) for target in value]
        if len(set(keys)) != len(keys):
            raise ValueError("runtime_targets cannot contain duplicate version pairs")
        return tuple(
            sorted(value, key=lambda target: (Version(target.astrbot), Version(target.python)))
        )

    @model_validator(mode="after")
    def validate_stage_configuration(self) -> Self:
        required = set(self.required_stages)
        if ReviewPolicyStage.STATIC not in required:
            raise ValueError("static must be a required stage")
        if ReviewPolicyStage.RUNTIME in required and not self.runtime_targets:
            raise ValueError("required runtime stage needs at least one exact runtime target")
        if ReviewPolicyStage.CATEGORY in required and not self.category.enabled:
            raise ValueError("required category stage must be enabled")
        if ReviewPolicyStage.CLAMAV in required and not self.malware.clamav:
            raise ValueError("required ClamAV stage must be enabled")
        if ReviewPolicyStage.YARA in required and not self.malware.yara_ruleset:
            raise ValueError("required YARA stage needs a ruleset")
        if ReviewPolicyStage.DEPENDENCY in required and not self.dependency.enabled:
            raise ValueError("required dependency stage must be enabled")

        llm_stages = {
            ReviewPolicyStage.LLM_PACKAGE,
            ReviewPolicyStage.LLM_FILE,
            ReviewPolicyStage.LLM_SUMMARY,
        }
        if required & llm_stages and not self.llm.enabled:
            raise ValueError("required LLM stages need LLM review enabled")
        if ReviewPolicyStage.LLM_FILE in required and ReviewPolicyStage.LLM_PACKAGE not in required:
            raise ValueError("required llm_file stage needs llm_package")
        if (
            ReviewPolicyStage.LLM_SUMMARY in required
            and not {
                ReviewPolicyStage.LLM_PACKAGE,
                ReviewPolicyStage.LLM_FILE,
            }
            <= required
        ):
            raise ValueError("required llm_summary stage needs llm_package and llm_file")
        if ReviewPolicyStage.IMPORT_GRAPH in required and ReviewPolicyStage.DIFF not in required:
            raise ValueError("required import_graph stage needs diff")

        if self.routing.auto_approve:
            self._validate_auto_approve(required)
        return self

    def _validate_auto_approve(self, required: set[ReviewPolicyStage]) -> None:
        if ReviewPolicyStage.RUNTIME not in required:
            raise ValueError("auto approve requires runtime as a required stage")

        enabled_gates: set[ReviewPolicyStage] = set()
        if self.dependency.enabled:
            enabled_gates.add(ReviewPolicyStage.DEPENDENCY)
        if self.malware.clamav:
            enabled_gates.add(ReviewPolicyStage.CLAMAV)
        if self.malware.yara_ruleset:
            enabled_gates.add(ReviewPolicyStage.YARA)
        if self.llm.enabled:
            enabled_gates.update(
                {
                    ReviewPolicyStage.LLM_PACKAGE,
                    ReviewPolicyStage.LLM_FILE,
                    ReviewPolicyStage.LLM_SUMMARY,
                }
            )
        if not enabled_gates <= required:
            missing = sorted((stage.value for stage in enabled_gates - required))
            raise ValueError(f"auto approve requires enabled review gates: {', '.join(missing)}")
        if not self.routing.require_complete_coverage:
            raise ValueError("auto approve requires complete review coverage")


def parse_review_policy(payload: Mapping[str, Any] | ReviewPolicyV1) -> ReviewPolicyV1:
    if isinstance(payload, ReviewPolicyV1):
        return payload
    return ReviewPolicyV1.model_validate(dict(payload))


def canonical_policy_json(payload: Mapping[str, Any] | ReviewPolicyV1) -> str:
    policy = parse_review_policy(payload)
    return json.dumps(
        policy.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def review_policy_sha256(payload: Mapping[str, Any] | ReviewPolicyV1) -> str:
    return hashlib.sha256(canonical_policy_json(payload).encode("utf-8")).hexdigest()


def review_policy_json_schema() -> dict[str, Any]:
    return ReviewPolicyV1.model_json_schema()


def _config_reference(value: str) -> str:
    normalized = value.strip()
    if not _CONFIG_REFERENCE.fullmatch(normalized):
        raise ValueError("secret-backed values must use a config, env, or secret reference")
    return normalized


def _exact_version(value: str, label: str) -> str:
    raw = value.strip()
    if not raw or raw.lower() == "latest":
        raise ValueError(f"{label} version must be exact and cannot use latest")
    try:
        parsed = Version(raw)
    except InvalidVersion as exc:
        raise ValueError(f"{label} version must be an exact PEP 440 version") from exc
    if parsed.local is not None:
        raise ValueError(f"{label} version cannot use a local version label")
    return str(parsed)
