from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..auth import is_admin
from .content import ArtifactContentError, ArtifactContentService
from .diff import ArtifactManifestFile
from .models import (
    ArtifactErrorCode,
    ReviewCommentEventType,
    ReviewCommentSide,
)
from .repository import ArtifactRepository


class ReviewCommentError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ReviewCommentLimits:
    max_body_chars: int = 10_000
    max_page_size: int = 20
    max_events_per_thread: int = 10
    max_response_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if (
            self.max_body_chars < 1
            or self.max_page_size < 1
            or self.max_events_per_thread < 1
            or self.max_response_bytes < 1
        ):
            raise ValueError("Review comment limits must be positive")


class ReviewCommentService:
    def __init__(
        self,
        repository: ArtifactRepository,
        content: ArtifactContentService,
        limits: ReviewCommentLimits | None = None,
    ) -> None:
        self.repository = repository
        self.content = content
        self.limits = limits or ReviewCommentLimits()

    async def list(
        self,
        artifact: Mapping[str, Any],
        *,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        current = await self._current_artifact(artifact)
        artifact_id = str(current["id"])
        rows = await self.repository.list_review_comments(
            artifact_id,
            limit=limit,
            offset=offset,
            event_limit=self.limits.max_events_per_thread,
        )
        response = {
            "artifact_id": artifact_id,
            "items": [public_review_comment(row) for row in rows],
            "total": await self.repository.count_review_comments(artifact_id),
            "limit": limit,
            "offset": offset,
        }
        self._ensure_response_size(response)
        return response

    async def create(
        self,
        *,
        artifact: Mapping[str, Any],
        actor: Mapping[str, Any],
        file_id: str,
        side: str,
        line_start: int,
        line_end: int,
        body: str,
        diff_id: str | None,
        hunk_id: str | None,
        source_thread_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not is_admin(actor):
            raise self._forbidden()
        current = await self._current_artifact(artifact)
        try:
            comment_side = ReviewCommentSide(side)
        except ValueError as exc:
            raise self._line_invalid() from exc
        normalized_body = self._body(body, required=True)
        key = self._idempotency_key(idempotency_key)
        side_artifact = await self._side_artifact(current, comment_side)
        try:
            _, _, manifest_file = await self.content.resolve_file(side_artifact, file_id)
        except ArtifactContentError as exc:
            raise _comment_content_error(exc) from exc
        self._validate_file_range(manifest_file, line_start, line_end)
        await self._validate_hunk_range(
            current,
            manifest_file,
            comment_side,
            line_start,
            line_end,
            diff_id=diff_id,
            hunk_id=hunk_id,
        )
        source = await self._source_thread(current, source_thread_id)
        payload = {
            "artifact_id": str(current["id"]),
            "source_thread_id": source["id"] if source else None,
            "file_id": manifest_file.id,
            "file_path": manifest_file.path,
            "file_sha256": manifest_file.sha256,
            "side": comment_side.value,
            "line_start": line_start,
            "line_end": line_end,
            "body": normalized_body,
            "reviewer_user_id": actor.get("id"),
            "reviewer_nickname": _actor_name(actor),
            "reviewer_role": _admin_role(actor),
            "idempotency_key": _comment_idempotency_key("thread", key),
            "event_idempotency_key": _comment_idempotency_key("event:create", key),
            "metadata": {
                "diff_id": diff_id,
                "hunk_id": hunk_id,
            },
        }
        try:
            saved = await self.repository.create_review_comment(payload)
        except ValueError as exc:
            raise _comment_repository_error(exc) from exc
        return await self._thread(str(current["id"]), str(saved["id"]))

    async def mutate(
        self,
        *,
        artifact: Mapping[str, Any],
        thread_id: str,
        actor: Mapping[str, Any],
        event_type: str,
        expected_version: int,
        body: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        current = await self._current_artifact(artifact)
        artifact_id = str(current["id"])
        thread = await self.repository.get_review_comment(
            artifact_id,
            thread_id,
            event_limit=self.limits.max_events_per_thread,
        )
        if thread is None:
            raise ReviewCommentError(
                ArtifactErrorCode.COMMENT_NOT_FOUND.value,
                "Review comment thread does not exist",
                status_code=404,
            )
        try:
            action = ReviewCommentEventType(event_type)
        except ValueError as exc:
            raise self._forbidden() from exc
        if action is ReviewCommentEventType.CREATE:
            raise self._forbidden()
        actor_role = self._authorize(current, thread, actor, action)
        normalized_body = self._event_body(action, body)
        key = self._idempotency_key(idempotency_key)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ReviewCommentError(
                ArtifactErrorCode.COMMENT_VERSION_CONFLICT.value,
                "Review comment version is invalid",
                status_code=409,
            )
        try:
            saved = await self.repository.append_review_comment_event(
                thread_id,
                {
                    "type": action.value,
                    "body": normalized_body,
                    "actor_user_id": actor.get("id"),
                    "actor_nickname": _actor_name(actor),
                    "actor_role": actor_role,
                    "expected_version": expected_version,
                    "metadata": {},
                    "idempotency_key": _comment_idempotency_key(f"event:{action.value}", key),
                },
            )
        except ValueError as exc:
            raise _comment_repository_error(exc) from exc
        if saved is None:
            raise ReviewCommentError(
                ArtifactErrorCode.COMMENT_NOT_FOUND.value,
                "Review comment thread does not exist",
                status_code=404,
            )
        return await self._thread(artifact_id, thread_id)

    async def _thread(self, artifact_id: str, thread_id: str) -> dict[str, Any]:
        thread = await self.repository.get_review_comment(
            artifact_id,
            thread_id,
            event_limit=self.limits.max_events_per_thread,
        )
        if thread is None:
            raise ReviewCommentError(
                ArtifactErrorCode.COMMENT_NOT_FOUND.value,
                "Review comment thread does not exist",
                status_code=404,
            )
        response = public_review_comment(thread)
        self._ensure_response_size(response)
        return response

    async def _current_artifact(self, artifact: Mapping[str, Any]) -> Mapping[str, Any]:
        current = await self.repository.get_artifact(str(artifact.get("id") or ""))
        if current is None:
            raise ReviewCommentError(
                "artifact_not_found",
                "Artifact does not exist",
                status_code=404,
            )
        return current

    async def _side_artifact(
        self,
        current: Mapping[str, Any],
        side: ReviewCommentSide,
    ) -> Mapping[str, Any]:
        if side is ReviewCommentSide.CURRENT:
            return current
        base_id = str(current.get("base_artifact_id") or "")
        if not base_id:
            raise self._line_invalid()
        base = await self.repository.get_artifact(base_id)
        if base is None or str(base.get("plugin_id") or "") != str(current.get("plugin_id") or ""):
            raise self._line_invalid()
        return base

    async def _validate_hunk_range(
        self,
        current: Mapping[str, Any],
        manifest_file: ArtifactManifestFile,
        side: ReviewCommentSide,
        line_start: int,
        line_end: int,
        *,
        diff_id: str | None,
        hunk_id: str | None,
    ) -> None:
        if bool(diff_id) != bool(hunk_id):
            raise self._line_invalid()
        if not diff_id or not hunk_id:
            return
        try:
            document = await self.content.read_diff(current, diff_id, hunk_id=hunk_id)
        except ArtifactContentError as exc:
            raise _comment_content_error(exc) from exc
        diff = document["diff"]
        expected_file_id = (
            diff.get("base_file_id")
            if side is ReviewCommentSide.BASE
            else diff.get("current_file_id")
        )
        if str(expected_file_id or "") != manifest_file.id or len(document["hunks"]) != 1:
            raise self._line_invalid()
        coordinate = "old_line" if side is ReviewCommentSide.BASE else "new_line"
        available = {
            int(line[coordinate])
            for line in document["hunks"][0]["lines"]
            if line.get(coordinate) is not None
        }
        if any(line not in available for line in range(line_start, line_end + 1)):
            raise self._line_invalid()

    async def _source_thread(
        self,
        current: Mapping[str, Any],
        source_thread_id: str | None,
    ) -> Mapping[str, Any] | None:
        source_id = str(source_thread_id or "").strip()
        if not source_id:
            return None
        predecessor_id = str(current.get("supersedes_artifact_id") or "")
        if not predecessor_id:
            raise self._source_invalid()
        predecessor = await self.repository.get_artifact(predecessor_id)
        source = await self.repository.get_review_comment(predecessor_id, source_id)
        if (
            predecessor is None
            or source is None
            or str(predecessor.get("plugin_id") or "") != str(current.get("plugin_id") or "")
            or source.get("locked_at") is None
        ):
            raise self._source_invalid()
        return source

    def _authorize(
        self,
        artifact: Mapping[str, Any],
        thread: Mapping[str, Any],
        actor: Mapping[str, Any],
        action: ReviewCommentEventType,
    ) -> str:
        admin = is_admin(actor)
        author = _is_author(actor, artifact)
        if action is ReviewCommentEventType.EDIT:
            if not admin or str(thread.get("reviewer_user_id") or "") != str(actor.get("id") or ""):
                raise self._forbidden()
            return _admin_role(actor)
        if action in {ReviewCommentEventType.RESOLVE, ReviewCommentEventType.REOPEN}:
            if not admin:
                raise self._forbidden()
            return _admin_role(actor)
        if action is ReviewCommentEventType.REPLY:
            if admin:
                return _admin_role(actor)
            if author:
                return "author"
            raise self._forbidden()
        if action is ReviewCommentEventType.AUTHOR_ADDRESSED:
            if not author:
                raise self._forbidden()
            return "author"
        raise self._forbidden()

    def _event_body(self, action: ReviewCommentEventType, body: str) -> str:
        if action in {ReviewCommentEventType.EDIT, ReviewCommentEventType.REPLY}:
            return self._body(body, required=True)
        if action is ReviewCommentEventType.AUTHOR_ADDRESSED:
            return self._body(body, required=False)
        return ""

    def _body(self, value: str, *, required: bool) -> str:
        if not isinstance(value, str):
            raise self._body_invalid()
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if any(
            (ord(char) < 32 and char not in {"\n", "\t"}) or ord(char) == 127 for char in normalized
        ):
            raise self._body_invalid()
        if (required and not normalized) or len(normalized) > self.limits.max_body_chars:
            raise self._body_invalid()
        return normalized

    def _idempotency_key(self, value: str) -> str:
        key = str(value or "").strip()
        if not key:
            raise ReviewCommentError(
                ArtifactErrorCode.IDEMPOTENCY_KEY_REQUIRED.value,
                "Idempotency key is required",
                status_code=400,
            )
        if len(key) > 200 or any(ord(char) < 33 or ord(char) == 127 for char in key):
            raise ReviewCommentError(
                ArtifactErrorCode.IDEMPOTENCY_KEY_REQUIRED.value,
                "Idempotency key is invalid",
                status_code=400,
            )
        return key

    def _page(self, limit: int, offset: int) -> tuple[int, int]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > self.limits.max_page_size
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise ReviewCommentError(
                ArtifactErrorCode.ARTIFACT_PAGE_INVALID.value,
                "Review comment page is invalid",
                status_code=400,
            )
        return limit, offset

    def _ensure_response_size(self, response: Mapping[str, Any]) -> None:
        size = len(
            json.dumps(
                response,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        if size > self.limits.max_response_bytes:
            raise ReviewCommentError(
                ArtifactErrorCode.ARTIFACT_RESPONSE_TOO_LARGE.value,
                "Review comment response exceeds the browsing limit",
                status_code=413,
            )

    @staticmethod
    def _validate_file_range(
        manifest_file: ArtifactManifestFile,
        line_start: int,
        line_end: int,
    ) -> None:
        line_count = manifest_file.line_count
        if (
            not manifest_file.is_text
            or not isinstance(line_count, int)
            or isinstance(line_count, bool)
            or line_count < 1
            or not isinstance(line_start, int)
            or isinstance(line_start, bool)
            or not isinstance(line_end, int)
            or isinstance(line_end, bool)
            or line_start < 1
            or line_end < line_start
            or line_end > line_count
        ):
            raise ReviewCommentService._line_invalid()

    @staticmethod
    def _body_invalid() -> ReviewCommentError:
        return ReviewCommentError(
            ArtifactErrorCode.COMMENT_BODY_INVALID.value,
            "Review comment body is invalid",
            status_code=400,
        )

    @staticmethod
    def _line_invalid() -> ReviewCommentError:
        return ReviewCommentError(
            ArtifactErrorCode.COMMENT_LINE_INVALID.value,
            "Review comment line anchor is invalid",
            status_code=400,
        )

    @staticmethod
    def _source_invalid() -> ReviewCommentError:
        return ReviewCommentError(
            ArtifactErrorCode.COMMENT_SOURCE_INVALID.value,
            "Review comment source thread is invalid",
            status_code=400,
        )

    @staticmethod
    def _forbidden() -> ReviewCommentError:
        return ReviewCommentError(
            ArtifactErrorCode.COMMENT_ACTION_FORBIDDEN.value,
            "Review comment action is forbidden",
            status_code=403,
        )

    @staticmethod
    def _thread_locked() -> ReviewCommentError:
        return ReviewCommentError(
            ArtifactErrorCode.COMMENT_THREAD_LOCKED.value,
            "Review comment thread is locked",
            status_code=409,
        )


def public_review_comment(thread: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(thread.get("id") or ""),
        "artifact_id": str(thread.get("artifact_id") or ""),
        "source_thread_id": str(thread.get("source_thread_id") or "") or None,
        "file_id": str(thread.get("file_id") or "") or None,
        "file_path": str(thread.get("file_path") or ""),
        "file_sha256": str(thread.get("file_sha256") or ""),
        "side": str(thread.get("side") or ""),
        "line_start": int(thread.get("line_start") or 0),
        "line_end": int(thread.get("line_end") or 0),
        "body": str(thread.get("body") or ""),
        "reviewer_nickname": str(thread.get("reviewer_nickname") or ""),
        "reviewer_role": str(thread.get("reviewer_role") or ""),
        "resolved": bool(thread.get("resolved")),
        "resolved_by_nickname": str(thread.get("resolved_by_nickname") or ""),
        "locked_at": thread.get("locked_at"),
        "version": int(thread.get("version") or 1),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "resolved_at": thread.get("resolved_at"),
        "event_count": int(thread.get("event_count") or len(thread.get("events") or [])),
        "events_truncated": bool(thread.get("events_truncated")),
        "events": [public_review_comment_event(event) for event in thread.get("events") or []],
    }


def public_review_comment_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(event.get("id") or ""),
        "thread_id": str(event.get("thread_id") or ""),
        "type": str(event.get("type") or ""),
        "body": str(event.get("body") or ""),
        "actor_nickname": str(event.get("actor_nickname") or ""),
        "actor_role": str(event.get("actor_role") or ""),
        "expected_version": int(event.get("expected_version") or 0),
        "resulting_version": int(event.get("resulting_version") or 1),
        "created_at": event.get("created_at"),
    }


def _is_author(actor: Mapping[str, Any], artifact: Mapping[str, Any]) -> bool:
    actor_id = str(actor.get("id") or "")
    return bool(actor_id) and actor_id in {
        str(artifact.get("submitted_by") or ""),
        str(artifact.get("owner_user_id") or ""),
    }


def _admin_role(actor: Mapping[str, Any]) -> str:
    return "core_admin" if str(actor.get("role") or "") == "core_admin" else "admin"


def _actor_name(actor: Mapping[str, Any]) -> str:
    return str(
        actor.get("nickname")
        or actor.get("name")
        or actor.get("github_name")
        or actor.get("internal_username")
        or actor.get("github_login")
        or ""
    )[:200]


def _comment_idempotency_key(scope: str, client_key: str) -> str:
    return f"comment:{scope}:{client_key}"


def _comment_content_error(exc: ArtifactContentError) -> ReviewCommentError:
    if exc.code in {
        ArtifactErrorCode.ARTIFACT_FILE_NOT_FOUND.value,
        ArtifactErrorCode.ARTIFACT_FILE_NOT_TEXT.value,
        ArtifactErrorCode.ARTIFACT_CONTENT_RANGE_INVALID.value,
    }:
        return ReviewCommentService._line_invalid()
    return ReviewCommentError(exc.code, str(exc), status_code=exc.status_code)


def _comment_repository_error(exc: ValueError) -> ReviewCommentError:
    code = str(exc)
    if code == ArtifactErrorCode.COMMENT_LINE_INVALID.value:
        return ReviewCommentService._line_invalid()
    if code in {
        ArtifactErrorCode.COMMENT_VERSION_CONFLICT.value,
        ArtifactErrorCode.COMMENT_THREAD_LOCKED.value,
        ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value,
    }:
        return ReviewCommentError(code, "Review comment state changed", status_code=409)
    return ReviewCommentError(code, "Review comment could not be saved", status_code=409)
