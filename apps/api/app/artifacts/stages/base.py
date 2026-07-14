from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, TypeVar, runtime_checkable

from ..repository import ArtifactRepository
from ..storage import ArtifactStorage

T = TypeVar("T")


class StageOutcomeKind(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class StageOutcome:
    kind: StageOutcomeKind
    summary: str
    error_code: str | None = None
    coverage: Mapping[str, Any] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        summary = " ".join(str(self.summary or "").split())
        if not summary:
            raise ValueError("Stage outcome summary is required")
        error_code = str(self.error_code or "").strip() or None
        if self.kind == StageOutcomeKind.COMPLETED and error_code is not None:
            raise ValueError("Completed stage outcomes cannot have an error code")
        if self.kind != StageOutcomeKind.COMPLETED and error_code is None:
            raise ValueError("Non-completed stage outcomes require an error code")
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "coverage", _frozen_copy(self.coverage))
        object.__setattr__(self, "details", _frozen_copy(self.details))

    @property
    def completes_job(self) -> bool:
        return self.kind in {
            StageOutcomeKind.COMPLETED,
            StageOutcomeKind.BLOCKED,
            StageOutcomeKind.DEGRADED,
        }

    @property
    def retryable(self) -> bool:
        return self.kind == StageOutcomeKind.RETRYABLE_FAILURE

    @classmethod
    def completed(
        cls,
        summary: str,
        *,
        coverage: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> StageOutcome:
        return cls(
            StageOutcomeKind.COMPLETED,
            summary,
            coverage=coverage or {},
            details=details or {},
        )

    @classmethod
    def blocked(
        cls,
        error_code: str,
        summary: str,
        *,
        coverage: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> StageOutcome:
        return cls(
            StageOutcomeKind.BLOCKED,
            summary,
            error_code=error_code,
            coverage=coverage or {},
            details=details or {},
        )

    @classmethod
    def degraded(
        cls,
        error_code: str,
        summary: str,
        *,
        coverage: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> StageOutcome:
        return cls(
            StageOutcomeKind.DEGRADED,
            summary,
            error_code=error_code,
            coverage=coverage or {},
            details=details or {},
        )

    @classmethod
    def retryable_failure(
        cls,
        error_code: str,
        summary: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> StageOutcome:
        return cls(
            StageOutcomeKind.RETRYABLE_FAILURE,
            summary,
            error_code=error_code,
            details=details or {},
        )

    @classmethod
    def terminal_failure(
        cls,
        error_code: str,
        summary: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> StageOutcome:
        return cls(
            StageOutcomeKind.TERMINAL_FAILURE,
            summary,
            error_code=error_code,
            details=details or {},
        )


@dataclass(frozen=True, slots=True)
class StageContext:
    job: Mapping[str, Any]
    artifact: Mapping[str, Any]
    policy: Mapping[str, Any] | None
    repository: ArtifactRepository
    storage: ArtifactStorage
    tools: Mapping[str, object]
    logger: logging.Logger
    log_context: Mapping[str, str]

    @classmethod
    def create(
        cls,
        *,
        job: Mapping[str, Any],
        artifact: Mapping[str, Any],
        policy: Mapping[str, Any] | None,
        repository: ArtifactRepository,
        storage: ArtifactStorage,
        tools: Mapping[str, object],
        logger: logging.Logger,
    ) -> StageContext:
        job_snapshot = _frozen_copy(job)
        artifact_snapshot = _frozen_copy(artifact)
        return cls(
            job=job_snapshot,
            artifact=artifact_snapshot,
            policy=_frozen_copy(policy) if policy is not None else None,
            repository=repository,
            storage=storage,
            tools=MappingProxyType(dict(tools)),
            logger=logger,
            log_context=MappingProxyType(
                {
                    "artifact_id": str(artifact_snapshot.get("id") or ""),
                    "job_id": str(job_snapshot.get("id") or ""),
                    "job_type": str(job_snapshot.get("type") or ""),
                }
            ),
        )

    def with_snapshots(
        self,
        *,
        artifact: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> StageContext:
        artifact_snapshot = _frozen_copy(artifact or self.artifact)
        policy_snapshot = _frozen_copy(policy) if policy is not None else self.policy
        return replace(
            self,
            artifact=artifact_snapshot,
            policy=policy_snapshot,
            log_context=MappingProxyType(
                {
                    **dict(self.log_context),
                    "artifact_id": str(artifact_snapshot.get("id") or ""),
                }
            ),
        )

    def require_tool(self, name: str, expected_type: type[T]) -> T:
        tool = self.tools.get(name)
        if tool is None or not isinstance(tool, expected_type):
            raise RuntimeError(f"Review stage tool is unavailable: {name}")
        return tool

    @property
    def attempt(self) -> int:
        return int(self.job.get("attempts") or 1)

    async def emit_status(
        self,
        event_type: str,
        suffix: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        await self.repository.enqueue_outbox(
            {
                "event_type": event_type,
                "aggregate_type": "artifact",
                "aggregate_id": self.artifact["id"],
                "recipient_user_id": self.artifact.get("submitted_by"),
                "payload": {
                    "artifact_id": self.artifact["id"],
                    "plugin_id": self.artifact["plugin_id"],
                    **dict(extra or {}),
                },
                "dedupe_key": f"artifact:{self.artifact['id']}:{suffix}",
            }
        )


@runtime_checkable
class ReviewStage(Protocol):
    job_type: str

    async def execute(self, context: StageContext) -> StageOutcome: ...


def _frozen_copy(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _frozen_value(item) for key, item in value.items()})


def _frozen_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _frozen_copy(value)
    if isinstance(value, (list, tuple)):
        return tuple(_frozen_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_frozen_value(item) for item in value)
    return value
