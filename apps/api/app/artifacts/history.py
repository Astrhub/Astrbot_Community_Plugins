from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .models import ArtifactErrorCode
from .repository import ArtifactRepository

_HISTORY_TYPES = frozenset(
    {
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
    }
)
_PRIVATE_PAYLOAD_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "content_key",
        "credential",
        "credentials",
        "endpoint",
        "env",
        "environment",
        "log",
        "logs",
        "password",
        "prompt",
        "provider_response",
        "published_key",
        "quarantine_key",
        "raw_result",
        "raw_result_key",
        "request",
        "request_fingerprint",
        "response",
        "result_key",
        "secret",
        "stderr",
        "stdout",
        "token",
        "url",
        "worker_id",
    }
)
_PRIVATE_SUFFIXES = (
    "_api_key",
    "_credential",
    "_endpoint",
    "_key",
    "_password",
    "_secret",
    "_token",
    "_url",
)


class ReviewHistoryError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ReviewHistoryLimits:
    max_page_size: int = 50
    max_response_bytes: int = 512 * 1024
    max_payload_depth: int = 8
    max_collection_items: int = 200

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.max_page_size,
                self.max_response_bytes,
                self.max_payload_depth,
                self.max_collection_items,
            )
        ):
            raise ValueError("Review history limits must be positive")


class ReviewHistoryService:
    def __init__(
        self,
        repository: ArtifactRepository,
        limits: ReviewHistoryLimits | None = None,
    ) -> None:
        self.repository = repository
        self.limits = limits or ReviewHistoryLimits()

    async def list(
        self,
        artifact: Mapping[str, Any],
        *,
        limit: int,
        cursor: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > self.limits.max_page_size
        ):
            raise ReviewHistoryError(
                ArtifactErrorCode.ARTIFACT_PAGE_INVALID.value,
                "Review history page is invalid",
                status_code=400,
            )
        current = await self.repository.get_artifact(str(artifact.get("id") or ""))
        if current is None:
            raise ReviewHistoryError(
                "artifact_not_found",
                "Artifact does not exist",
                status_code=404,
            )
        after = _decode_cursor(cursor) if cursor else None
        rows = await self.repository.list_review_history_records(
            str(current["id"]),
            limit=limit + 1,
            after=after,
        )
        items = [self._event(row) for row in rows]
        keys = [_event_key(item) for item in items]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ReviewHistoryError(
                ArtifactErrorCode.HISTORY_PROJECTION_INVALID.value,
                "Review history projection is invalid",
                status_code=409,
            )
        has_more = len(items) > limit
        selected = items[:limit]
        next_cursor = _encode_cursor(_event_key(selected[-1])) if has_more and selected else None
        response = {
            "artifact_id": str(current["id"]),
            "items": selected,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }
        if _json_size(response) > self.limits.max_response_bytes:
            raise ReviewHistoryError(
                ArtifactErrorCode.ARTIFACT_RESPONSE_TOO_LARGE.value,
                "Review history response exceeds the browsing limit",
                status_code=413,
            )
        return response

    def _event(self, row: Mapping[str, Any]) -> dict[str, Any]:
        event_type = str(row.get("type") or "")
        record_id = str(row.get("id") or "")
        if event_type not in _HISTORY_TYPES or not record_id or len(record_id) > 300:
            raise ReviewHistoryError(
                ArtifactErrorCode.HISTORY_PROJECTION_INVALID.value,
                "Review history event identity is invalid",
                status_code=409,
            )
        occurred_at = _parse_time(row.get("occurred_at"))
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            raise ReviewHistoryError(
                ArtifactErrorCode.HISTORY_PROJECTION_INVALID.value,
                "Review history event payload is invalid",
                status_code=409,
            )
        return {
            "id": record_id,
            "type": event_type,
            "occurred_at": _iso_time(occurred_at),
            "source": str(row.get("source") or "system")[:40],
            "actor_nickname": str(row.get("actor_nickname") or "")[:200],
            "actor_role": str(row.get("actor_role") or "system")[:40],
            "idempotency_key": str(row.get("idempotency_key") or "")[:500],
            "policy_version_id": str(row.get("policy_version_id") or "") or None,
            "payload": self._public_value(payload),
        }

    def _public_value(self, value: Any, *, depth: int = 0) -> Any:
        if depth >= self.limits.max_payload_depth:
            return None
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in list(value.items())[: self.limits.max_collection_items]:
                normalized = str(key).strip().lower()
                if normalized in _PRIVATE_PAYLOAD_KEYS or normalized.endswith(_PRIVATE_SUFFIXES):
                    continue
                result[str(key)] = self._public_value(item, depth=depth + 1)
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                self._public_value(item, depth=depth + 1)
                for item in list(value)[: self.limits.max_collection_items]
            ]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)


def _encode_cursor(key: tuple[datetime, str, str]) -> str:
    payload = {
        "t": _iso_time(key[0]),
        "k": key[1],
        "i": key[2],
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str, str]:
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise _cursor_error()
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise _cursor_error() from exc
    if not isinstance(payload, dict) or set(payload) != {"t", "k", "i"}:
        raise _cursor_error()
    event_type = payload.get("k")
    record_id = payload.get("i")
    if (
        event_type not in _HISTORY_TYPES
        or not isinstance(record_id, str)
        or not record_id
        or len(record_id) > 300
    ):
        raise _cursor_error()
    try:
        occurred_at = _parse_time(payload.get("t"))
    except ReviewHistoryError as exc:
        raise _cursor_error() from exc
    return occurred_at, str(event_type), record_id


def _event_key(event: Mapping[str, Any]) -> tuple[datetime, str, str]:
    return (
        _parse_time(event.get("occurred_at")),
        str(event.get("type") or ""),
        str(event.get("id") or ""),
    )


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReviewHistoryError(
                ArtifactErrorCode.HISTORY_PROJECTION_INVALID.value,
                "Review history timestamp is invalid",
                status_code=409,
            ) from exc
    else:
        raise ReviewHistoryError(
            ArtifactErrorCode.HISTORY_PROJECTION_INVALID.value,
            "Review history timestamp is invalid",
            status_code=409,
        )
    if parsed.tzinfo is None:
        raise ReviewHistoryError(
            ArtifactErrorCode.HISTORY_PROJECTION_INVALID.value,
            "Review history timestamp must include a timezone",
            status_code=409,
        )
    return parsed.astimezone(UTC)


def _iso_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _cursor_error() -> ReviewHistoryError:
    return ReviewHistoryError(
        ArtifactErrorCode.HISTORY_CURSOR_INVALID.value,
        "Review history cursor is invalid",
        status_code=400,
    )


def _json_size(value: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )
