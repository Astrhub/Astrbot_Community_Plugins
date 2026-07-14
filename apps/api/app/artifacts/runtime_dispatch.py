from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError

from ..runtime_runner.queue import (
    RuntimeDispatchWorkItem,
    RuntimeRunnerQueue,
    RuntimeRunnerQueueError,
    RuntimeRunnerRepository,
)
from .models import ArtifactErrorCode, RuntimeDispatchStatus
from .repository import ArtifactRepository
from .runner_contract import (
    MAX_RUNTIME_RESULT_BYTES,
    RuntimeDispatchRequest,
    RuntimeDispatchResult,
    contract_sha256,
    runtime_result_error_code,
    runtime_result_object_key,
    runtime_result_passed,
    validate_runtime_result_identity,
)
from .storage import ArtifactStorageError

__all__ = [
    "CollectionState",
    "RuntimeCollectionResult",
    "RuntimeDispatchController",
    "RuntimeDispatchServiceError",
    "RuntimeDispatchWorkItem",
    "RuntimeRunnerQueue",
    "RuntimeRunnerQueueError",
    "RuntimeRunnerRepository",
]


class RuntimeDispatchServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CollectionState(StrEnum):
    WAITING = "waiting"
    COLLECTED = "collected"
    ALREADY_COLLECTED = "already_collected"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class RuntimeCollectionResult:
    state: CollectionState
    dispatch_id: str
    run_id: str = ""
    run_status: str = ""
    result: RuntimeDispatchResult | None = None
    error_code: str = ""


class RuntimeResultStorage(Protocol):
    async def read_text_content(
        self,
        key: str,
        max_bytes: int,
        expected_sha256: str = "",
    ) -> bytes: ...


class RuntimeDispatchController:
    def __init__(
        self,
        repository: ArtifactRepository,
        result_storage: RuntimeResultStorage,
    ) -> None:
        self.repository = repository
        self.result_storage = result_storage

    async def create(
        self,
        request: RuntimeDispatchRequest | Mapping[str, Any],
        *,
        run_id: str,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        parsed = (
            request
            if isinstance(request, RuntimeDispatchRequest)
            else RuntimeDispatchRequest.model_validate(dict(request))
        )
        if max_attempts < 1 or max_attempts > 20:
            raise ValueError("invalid_runtime_max_attempts")
        artifact = await self.repository.get_artifact(parsed.artifact_id)
        if artifact is None:
            raise RuntimeDispatchServiceError("artifact_not_found", "Artifact does not exist")
        runs = await self.repository.list_review_runs(parsed.artifact_id)
        run = next((item for item in runs if str(item["id"]) == run_id), None)
        if run is None or run["type"] != "runtime":
            raise RuntimeDispatchServiceError(
                ArtifactErrorCode.RUNTIME_RESULT_INVALID.value,
                "Runtime dispatch must reference a runtime review run",
            )
        if str(run.get("status") or "") in {"succeeded", "failed", "timed_out", "cancelled"}:
            raise RuntimeDispatchServiceError(
                ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value,
                "Runtime dispatch cannot target a terminal review run",
            )
        _validate_request_identity(parsed, artifact, run)
        request_payload = parsed.model_dump(mode="json")
        request_sha256 = contract_sha256(parsed)
        try:
            dispatch = await self.repository.create_runtime_dispatch(
                {
                    "id": parsed.dispatch_id,
                    "artifact_id": parsed.artifact_id,
                    "run_id": run_id,
                    "request": request_payload,
                    "request_sha256": request_sha256,
                    "max_attempts": max_attempts,
                }
            )
        except ValueError as exc:
            if str(exc) != ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value:
                raise
            raise RuntimeDispatchServiceError(
                ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value,
                "Runtime dispatch conflicts with the existing run request",
            ) from exc
        if (
            str(dispatch["id"]) != parsed.dispatch_id
            or str(dispatch["request_sha256"]) != request_sha256
        ):
            raise RuntimeDispatchServiceError(
                ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value,
                "Runtime dispatch identity conflicts with an existing request",
            )
        return dispatch

    async def collect(self, dispatch_id: str) -> RuntimeCollectionResult:
        dispatch = await self.repository.get_runtime_dispatch(dispatch_id)
        if dispatch is None:
            return RuntimeCollectionResult(CollectionState.NOT_FOUND, dispatch_id)
        run_id = str(dispatch["run_id"])
        if dispatch.get("collected_at"):
            return RuntimeCollectionResult(
                CollectionState.ALREADY_COLLECTED,
                dispatch_id,
                run_id=run_id,
            )
        status = RuntimeDispatchStatus(str(dispatch["status"]))
        if status in {RuntimeDispatchStatus.QUEUED, RuntimeDispatchStatus.RUNNING}:
            return RuntimeCollectionResult(CollectionState.WAITING, dispatch_id, run_id=run_id)
        if status == RuntimeDispatchStatus.CANCELLED:
            return RuntimeCollectionResult(CollectionState.CANCELLED, dispatch_id, run_id=run_id)

        parsed_result: RuntimeDispatchResult | None = None
        validation_error = ""
        if dispatch.get("result_key") and dispatch.get("result_sha256"):
            try:
                request = _validated_dispatch_request(dispatch)
                expected_result_key = runtime_result_object_key(
                    request,
                    int(dispatch.get("attempts") or 0),
                    str(dispatch["result_sha256"]),
                )
                if str(dispatch["result_key"]) != expected_result_key:
                    raise ValueError("runtime result key differs from its request")
                # The embedded hash signs canonical JSON without the hash field, not raw object bytes.
                content = await self.result_storage.read_text_content(
                    str(dispatch["result_key"]),
                    min(request.limits.max_result_bytes, MAX_RUNTIME_RESULT_BYTES),
                )
                parsed_result = RuntimeDispatchResult.model_validate_json(content)
                validate_runtime_result_identity(request, parsed_result)
                if parsed_result.result_sha256 != str(dispatch["result_sha256"]):
                    raise ValueError("runtime result digest differs from dispatch")
            except ArtifactStorageError as exc:
                if exc.code == "content_object_missing":
                    raise RuntimeDispatchServiceError(
                        "runtime_result_unavailable",
                        "Runtime result object is not available yet",
                        retryable=True,
                    ) from exc
                validation_error = ArtifactErrorCode.RUNTIME_RESULT_INVALID.value
            except (ValidationError, ValueError):
                validation_error = ArtifactErrorCode.RUNTIME_RESULT_INVALID.value
        elif status == RuntimeDispatchStatus.SUCCEEDED:
            validation_error = ArtifactErrorCode.RUNTIME_RESULT_INVALID.value

        run_payload = _collection_run_payload(dispatch, parsed_result, validation_error)
        collected = await self.repository.collect_runtime_dispatch(dispatch_id, run_payload)
        if collected is None:
            return RuntimeCollectionResult(
                CollectionState.ALREADY_COLLECTED,
                dispatch_id,
                run_id=run_id,
            )
        return RuntimeCollectionResult(
            CollectionState.COLLECTED,
            dispatch_id,
            run_id=run_id,
            run_status=str(run_payload["status"]),
            result=parsed_result,
            error_code=str(run_payload.get("error_code") or ""),
        )

    async def reconcile_expired(self, *, limit: int = 32) -> tuple[RuntimeCollectionResult, ...]:
        if limit < 1 or limit > 256:
            raise ValueError("invalid_runtime_reconcile_limit")
        expired = await self.repository.expire_runtime_dispatches(limit)
        collected: list[RuntimeCollectionResult] = []
        for dispatch in expired:
            collected.append(await self.collect(str(dispatch["id"])))
        return tuple(collected)


def _validated_dispatch_request(dispatch: Mapping[str, Any]) -> RuntimeDispatchRequest:
    request = RuntimeDispatchRequest.model_validate(dispatch.get("request") or {})
    if (
        request.dispatch_id != str(dispatch["id"])
        or request.artifact_id != str(dispatch["artifact_id"])
        or request.canonical_sha256() != str(dispatch["request_sha256"])
    ):
        raise ValueError("runtime dispatch request identity mismatch")
    return request


def _validate_request_identity(
    request: RuntimeDispatchRequest,
    artifact: Mapping[str, Any],
    run: Mapping[str, Any],
) -> None:
    if (
        request.artifact_sha256 != str(artifact.get("archive_sha256") or "")
        or request.artifact_size_bytes != int(artifact.get("size_bytes") or 0)
        or request.quarantine_key != str(artifact.get("quarantine_key") or "")
        or request.policy_version_id != str(artifact.get("policy_version_id") or "")
        or request.policy_version_id != str(run.get("policy_version_id") or "")
    ):
        raise RuntimeDispatchServiceError(
            ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value,
            "Runtime request does not match the artifact or policy snapshot",
        )
    plugin_name = str(artifact.get("plugin_name") or artifact.get("plugin_id") or "")
    if request.expected_plugin.name != plugin_name:
        raise RuntimeDispatchServiceError(
            ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value,
            "Runtime request plugin identity does not match the artifact",
        )
    expected_values = {
        "version": artifact.get("version"),
        "source_repo": str(artifact.get("source_repo") or "").rstrip("/"),
        "source_commit_sha": artifact.get("source_commit_sha"),
    }
    actual_values = {
        "version": request.expected_plugin.version,
        "source_repo": request.expected_plugin.source_repo.rstrip("/"),
        "source_commit_sha": request.expected_plugin.source_commit_sha,
    }
    for field, expected in expected_values.items():
        if expected and str(expected) != str(actual_values[field]):
            raise RuntimeDispatchServiceError(
                ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value,
                f"Runtime request {field} does not match the artifact snapshot",
            )
    run_target = {
        "astrbot_version": str(run.get("astrbot_version") or ""),
        "python_version": str(run.get("python_version") or ""),
        "image_digest": str(run.get("container_image_digest") or ""),
    }
    request_target = {
        "astrbot_version": request.target.astrbot_version,
        "python_version": request.target.python_version,
        "image_digest": request.target.image_digest,
    }
    if any(
        run_target[field] and run_target[field] != request_target[field] for field in run_target
    ):
        raise RuntimeDispatchServiceError(
            ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value,
            "Runtime request target does not match the review run",
        )


def _collection_run_payload(
    dispatch: Mapping[str, Any],
    result: RuntimeDispatchResult | None,
    validation_error: str,
) -> dict[str, Any]:
    dispatch_status = RuntimeDispatchStatus(str(dispatch["status"]))
    passed = (
        not validation_error
        and dispatch_status == RuntimeDispatchStatus.SUCCEEDED
        and result is not None
        and runtime_result_passed(result)
    )
    if passed:
        status = "succeeded"
        error_code = ""
        summary = "Runtime install and AstrBot smoke test passed"
        outcome = "completed"
    else:
        status = "timed_out" if dispatch_status == RuntimeDispatchStatus.TIMED_OUT else "failed"
        error_code = (
            validation_error
            or str(dispatch.get("error_code") or "")
            or (runtime_result_error_code(result) if result else "runtime_validation_failed")
        )
        summary = "Runtime validation did not pass"
        outcome = "failed"
    raw_result = {
        "dispatch_id": dispatch["id"],
        "dispatch_status": dispatch_status.value,
        "result_valid": result is not None and not validation_error,
    }
    coverage: dict[str, Any] = {
        "outcome": outcome,
        "stage_name": "runtime",
        "dispatch_id": dispatch["id"],
        "attempts": int(dispatch.get("attempts") or 0),
    }
    if result is not None:
        statuses = {
            "install": result.install.status.value,
            "smoke": result.smoke.status.value,
            "network": result.network_attestation.status.value,
            "cleanup": result.cleanup.status.value,
        }
        raw_result["statuses"] = statuses
        raw_result["target"] = result.target.model_dump(mode="json")
        coverage["statuses"] = statuses
    return {
        "status": status,
        "summary": summary,
        "raw_result": raw_result,
        "raw_result_key": dispatch.get("result_key"),
        "error_code": error_code,
        "output_sha256": str(dispatch.get("result_sha256") or ""),
        "coverage": coverage,
        "container_image_digest": str(dispatch.get("image_digest") or ""),
        "dependency_snapshot_sha256": (
            result.install.core_after_sha256 if result is not None else ""
        ),
        "worker_id": str(dispatch.get("runner_id") or ""),
    }
