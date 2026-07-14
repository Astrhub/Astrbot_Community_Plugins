from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from ..auth import is_core_admin
from .models import ArtifactErrorCode, ReviewPolicyEventAction, ReviewPolicyStatus
from .policy import ReviewPolicyV1, parse_review_policy, review_policy_sha256
from .repository import ArtifactRepository

_POLICY_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REQUEST_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_SENSITIVE_PATH_PARTS = ("api_key", "credential", "password", "secret", "token")
_MAX_DIFF_PATHS = 200


class ReviewPolicyServiceError(RuntimeError):
    def __init__(self, code: str | ArtifactErrorCode, message: str = "") -> None:
        normalized = code.value if isinstance(code, ArtifactErrorCode) else str(code)
        super().__init__(message or normalized)
        self.code = normalized


class ReviewPolicyPermissionError(PermissionError):
    code = "core_admin_required"

    def __init__(self) -> None:
        super().__init__(self.code)


class ReviewPolicyService:
    def __init__(self, repository: ArtifactRepository) -> None:
        self.repository = repository

    async def create_draft(
        self,
        *,
        version: str,
        policy: Mapping[str, Any],
        actor: Mapping[str, Any],
        request_id: str,
        idempotency_key: str,
        reason: str = "",
        base_policy_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_core_admin(actor)
        normalized_version = _policy_version(version)
        normalized_request_id = _request_identifier(request_id, "request_id")
        normalized_idempotency_key = _request_identifier(
            idempotency_key,
            "idempotency_key",
        )
        normalized_reason = _reason(reason, required=False)
        parsed = parse_review_policy(policy)
        policy_payload = parsed.model_dump(mode="json")
        policy_sha256 = review_policy_sha256(parsed)

        active = await self.repository.get_active_review_policy()
        base = None
        normalized_base_id = str(base_policy_id or "").strip() or None
        if normalized_base_id:
            base = await self.repository.get_review_policy(normalized_base_id)
            if not base:
                raise ReviewPolicyServiceError(ArtifactErrorCode.REVIEW_POLICY_INVALID)
        elif active:
            base = active
            normalized_base_id = str(active["id"])

        event = _event_payload(
            action=ReviewPolicyEventAction.CREATE,
            actor=actor,
            reason=normalized_reason,
            request_id=normalized_request_id,
            idempotency_key=normalized_idempotency_key,
            base_version=str(base["version"]) if base else "",
            diff=redacted_policy_diff(
                base.get("policy") if base else None,
                policy_payload,
            ),
        )
        try:
            return await self.repository.create_review_policy(
                {
                    "version": normalized_version,
                    "schema_version": parsed.schema_version,
                    "status": ReviewPolicyStatus.DRAFT.value,
                    "is_default": True,
                    "policy": policy_payload,
                    "policy_sha256": policy_sha256,
                    "base_policy_id": normalized_base_id,
                    "created_by_user_id": _actor_id(actor),
                    "created_by_nickname": _actor_nickname(actor),
                },
                event,
            )
        except ValueError as exc:
            raise _service_error(exc) from exc

    async def validate_draft(
        self,
        policy_id: str,
        *,
        actor: Mapping[str, Any],
        request_id: str,
        idempotency_key: str,
        reason: str = "",
    ) -> dict[str, Any]:
        self._require_core_admin(actor)
        record = await self._require_policy(policy_id)
        summary = review_policy_validation_summary(record)
        base = await self._base_policy(record)
        event = _event_payload(
            action=ReviewPolicyEventAction.VALIDATE,
            actor=actor,
            reason=_reason(reason, required=False),
            request_id=_request_identifier(request_id, "request_id"),
            idempotency_key=_request_identifier(idempotency_key, "idempotency_key"),
            base_version=str(base["version"]) if base else "",
            diff=redacted_policy_diff(
                base.get("policy") if base else None,
                record.get("policy") or {},
            ),
        )
        try:
            updated = await self.repository.transition_review_policy(
                policy_id,
                action=ReviewPolicyEventAction.VALIDATE.value,
                expected_policy_sha256=str(record["policy_sha256"]),
                expected_active_policy_id=None,
                validation_summary=summary,
                event=event,
            )
        except ValueError as exc:
            raise _service_error(exc) from exc
        if not updated:
            raise ReviewPolicyServiceError(ArtifactErrorCode.REVIEW_POLICY_INVALID)
        return updated

    async def activate(
        self,
        policy_id: str,
        *,
        actor: Mapping[str, Any],
        request_id: str,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        return await self._activate_or_rollback(
            policy_id,
            action=ReviewPolicyEventAction.ACTIVATE,
            actor=actor,
            request_id=request_id,
            idempotency_key=idempotency_key,
            reason=reason,
        )

    async def rollback(
        self,
        policy_id: str,
        *,
        actor: Mapping[str, Any],
        request_id: str,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        return await self._activate_or_rollback(
            policy_id,
            action=ReviewPolicyEventAction.ROLLBACK,
            actor=actor,
            request_id=request_id,
            idempotency_key=idempotency_key,
            reason=reason,
        )

    async def retire(
        self,
        policy_id: str,
        *,
        actor: Mapping[str, Any],
        request_id: str,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        self._require_core_admin(actor)
        record = await self._require_policy(policy_id)
        active = await self.repository.get_active_review_policy()
        event = _event_payload(
            action=ReviewPolicyEventAction.RETIRE,
            actor=actor,
            reason=_reason(reason, required=True),
            request_id=_request_identifier(request_id, "request_id"),
            idempotency_key=_request_identifier(idempotency_key, "idempotency_key"),
            base_version=str(record["version"]),
            diff=redacted_policy_diff(record.get("policy") or {}, None),
        )
        try:
            updated = await self.repository.transition_review_policy(
                policy_id,
                action=ReviewPolicyEventAction.RETIRE.value,
                expected_policy_sha256=str(record["policy_sha256"]),
                expected_active_policy_id=str(active["id"]) if active else None,
                validation_summary=None,
                event=event,
            )
        except ValueError as exc:
            raise _service_error(exc) from exc
        if not updated:
            raise ReviewPolicyServiceError(ArtifactErrorCode.REVIEW_POLICY_INVALID)
        return updated

    async def migrate_artifact_snapshot(
        self,
        artifact_id: str,
        target_policy_id: str,
        *,
        actor: Mapping[str, Any],
        request_id: str,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        self._require_core_admin(actor)
        normalized_artifact_id = _request_identifier(artifact_id, "artifact_id")
        normalized_target_id = _request_identifier(target_policy_id, "target_policy_id")
        target = await self._require_policy(normalized_target_id)
        if str(target.get("status") or "") not in {
            ReviewPolicyStatus.ACTIVE.value,
            ReviewPolicyStatus.RETIRED.value,
        } or not bool((target.get("validation_summary") or {}).get("valid")):
            raise ReviewPolicyServiceError(ArtifactErrorCode.REVIEW_POLICY_INVALID)
        try:
            migrated = await self.repository.migrate_artifact_policy(
                normalized_artifact_id,
                normalized_target_id,
                actor=actor,
                reason=_reason(reason, required=True),
                request_id=_request_identifier(request_id, "request_id"),
                idempotency_key=_request_identifier(idempotency_key, "idempotency_key"),
            )
        except ValueError as exc:
            raise _service_error(exc) from exc
        if not migrated:
            raise ReviewPolicyServiceError(ArtifactErrorCode.ARTIFACT_POLICY_MIGRATION_FORBIDDEN)
        return migrated

    async def _activate_or_rollback(
        self,
        policy_id: str,
        *,
        action: ReviewPolicyEventAction,
        actor: Mapping[str, Any],
        request_id: str,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        self._require_core_admin(actor)
        normalized_request_id = _request_identifier(request_id, "request_id")
        normalized_idempotency_key = _request_identifier(
            idempotency_key,
            "idempotency_key",
        )
        validated = await self.validate_draft(
            policy_id,
            actor=actor,
            request_id=normalized_request_id,
            idempotency_key=_derived_idempotency_key(
                normalized_idempotency_key,
                "validate",
            ),
            reason="Schema and cross-field validation before policy transition",
        )
        if not bool((validated.get("validation_summary") or {}).get("valid")):
            raise ReviewPolicyServiceError(ArtifactErrorCode.REVIEW_POLICY_INVALID)

        active = await self.repository.get_active_review_policy()
        event = _event_payload(
            action=action,
            actor=actor,
            reason=_reason(reason, required=True),
            request_id=normalized_request_id,
            idempotency_key=normalized_idempotency_key,
            base_version=str(active["version"]) if active else "",
            diff=redacted_policy_diff(
                active.get("policy") if active else None,
                validated.get("policy") or {},
            ),
        )
        try:
            updated = await self.repository.transition_review_policy(
                policy_id,
                action=action.value,
                expected_policy_sha256=str(validated["policy_sha256"]),
                expected_active_policy_id=str(active["id"]) if active else None,
                validation_summary=None,
                event=event,
            )
        except ValueError as exc:
            raise _service_error(exc) from exc
        if not updated:
            raise ReviewPolicyServiceError(ArtifactErrorCode.REVIEW_POLICY_INVALID)
        return updated

    async def _require_policy(self, policy_id: str) -> dict[str, Any]:
        normalized = str(policy_id or "").strip()
        if not normalized:
            raise ReviewPolicyServiceError(ArtifactErrorCode.REVIEW_POLICY_INVALID)
        policy = await self.repository.get_review_policy(normalized)
        if not policy:
            raise ReviewPolicyServiceError(ArtifactErrorCode.REVIEW_POLICY_INVALID)
        return policy

    async def _base_policy(self, policy: Mapping[str, Any]) -> dict[str, Any] | None:
        base_policy_id = str(policy.get("base_policy_id") or "")
        if not base_policy_id:
            return None
        return await self.repository.get_review_policy(base_policy_id)

    @staticmethod
    def _require_core_admin(actor: Mapping[str, Any]) -> None:
        if not is_core_admin(actor):
            raise ReviewPolicyPermissionError()


def review_policy_validation_summary(policy: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    parsed: ReviewPolicyV1 | None = None
    try:
        parsed = parse_review_policy(policy.get("policy") or {})
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False)[:100]:
            path = ".".join(str(part) for part in error.get("loc") or ()) or "$"
            issues.append(
                {
                    "path": _redact_path(path),
                    "code": str(error.get("type") or "validation_error")[:100],
                    "message": str(error.get("msg") or "Invalid policy")[:300],
                }
            )

    if parsed:
        if str(policy.get("schema_version") or "") != parsed.schema_version:
            issues.append(
                {
                    "path": "schema_version",
                    "code": "schema_version_mismatch",
                    "message": "Stored schema version does not match the policy payload",
                }
            )
        if review_policy_sha256(parsed) != str(policy.get("policy_sha256") or ""):
            issues.append(
                {
                    "path": "$",
                    "code": "policy_hash_mismatch",
                    "message": "Stored policy hash does not match the canonical payload",
                }
            )
    return {
        "valid": not issues,
        "schema_version": str(policy.get("schema_version") or ""),
        "policy_sha256": str(policy.get("policy_sha256") or ""),
        "issues": issues,
    }


def redacted_policy_diff(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> dict[str, Any]:
    before_paths = _leaf_paths(before or {})
    after_paths = _leaf_paths(after or {})
    added = sorted(after_paths.keys() - before_paths.keys())
    removed = sorted(before_paths.keys() - after_paths.keys())
    changed = sorted(
        path
        for path in before_paths.keys() & after_paths.keys()
        if before_paths[path] != after_paths[path]
    )
    total = len(added) + len(removed) + len(changed)
    remaining = _MAX_DIFF_PATHS
    added_output = added[:remaining]
    remaining -= len(added_output)
    removed_output = removed[:remaining]
    remaining -= len(removed_output)
    changed_output = changed[:remaining]
    return {
        "redacted": True,
        "before_sha256": _json_sha256(before) if before is not None else "",
        "after_sha256": _json_sha256(after) if after is not None else "",
        "added_paths": [_redact_path(path) for path in added_output],
        "removed_paths": [_redact_path(path) for path in removed_output],
        "changed_paths": [_redact_path(path) for path in changed_output],
        "path_count": total,
        "truncated": total > _MAX_DIFF_PATHS,
    }


def _leaf_paths(value: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(value, Mapping):
        result: dict[str, str] = {}
        for raw_key, child in sorted(value.items(), key=lambda item: str(item[0])):
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            result.update(_leaf_paths(child, path))
        return result
    path = prefix or "$"
    return {path: _json_sha256(value)}


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redact_path(path: str) -> str:
    lowered = path.lower()
    if any(part in lowered for part in _SENSITIVE_PATH_PARTS):
        return "<redacted>"
    return path[:300]


def _event_payload(
    *,
    action: ReviewPolicyEventAction,
    actor: Mapping[str, Any],
    reason: str,
    request_id: str,
    idempotency_key: str,
    base_version: str,
    diff: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "action": action.value,
        "actor_user_id": _actor_id(actor),
        "actor_nickname": _actor_nickname(actor),
        "reason": reason,
        "request_id": request_id,
        "base_version": str(base_version or "")[:64],
        "diff": dict(diff),
        "idempotency_key": idempotency_key,
    }


def _actor_id(actor: Mapping[str, Any]) -> str | None:
    value = str(actor.get("id") or "").strip()
    return value or None


def _actor_nickname(actor: Mapping[str, Any]) -> str:
    for key in ("nickname", "github_name", "github_login", "username", "id"):
        value = str(actor.get(key) or "").strip()
        if value:
            return value[:120]
    return "core_admin"


def _policy_version(value: str) -> str:
    normalized = str(value or "").strip()
    if not _POLICY_VERSION.fullmatch(normalized):
        raise ReviewPolicyServiceError(
            ArtifactErrorCode.REVIEW_POLICY_INVALID,
            "Policy version must be a safe immutable identifier",
        )
    return normalized


def _request_identifier(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not _REQUEST_IDENTIFIER.fullmatch(normalized):
        raise ReviewPolicyServiceError(
            ArtifactErrorCode.REVIEW_POLICY_INVALID,
            f"{label} must be a safe non-empty identifier",
        )
    return normalized


def _derived_idempotency_key(value: str, suffix: str) -> str:
    candidate = f"{value}:{suffix}"
    if len(candidate) <= 200:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:24]
    return f"{value[:160]}:{suffix}:{digest}"


def _reason(value: str, *, required: bool) -> str:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise ReviewPolicyServiceError(
            ArtifactErrorCode.REVIEW_POLICY_INVALID,
            "A reason is required for policy state changes",
        )
    if len(normalized) > 2000:
        raise ReviewPolicyServiceError(
            ArtifactErrorCode.REVIEW_POLICY_INVALID,
            "Policy reason is too long",
        )
    return normalized


def _service_error(exc: ValueError) -> ReviewPolicyServiceError:
    code = str(exc)
    known = {item.value for item in ArtifactErrorCode}
    return ReviewPolicyServiceError(
        code if code in known else ArtifactErrorCode.REVIEW_POLICY_INVALID
    )
