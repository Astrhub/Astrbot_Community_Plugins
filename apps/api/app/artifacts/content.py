from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .diff import (
    ArtifactManifestFile,
    DiffBuildError,
    validate_artifact_manifest,
    validate_hunk_payload,
)
from .models import ArtifactErrorCode
from .repository import ArtifactRepository
from .storage import (
    ArtifactStorage,
    ArtifactStorageError,
    build_diff_key,
)

_HUNK_ID_PATTERN = re.compile(r"^hunk-[1-9][0-9]*$")
_PUBLIC_DIFF_STATS = frozenset(
    {
        "added_lines",
        "base_line_count",
        "base_size_bytes",
        "binary",
        "current_line_count",
        "current_size_bytes",
        "deleted_lines",
        "forced_review",
        "hunk_count",
        "hunks_complete",
        "hunks_omitted",
        "hunks_omitted_reason",
        "hunks_truncated",
    }
)


class ArtifactContentError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ArtifactContentLimits:
    max_file_bytes: int = 512 * 1024
    max_diff_bytes: int = 256 * 1024
    max_response_bytes: int = 256 * 1024
    max_line_limit: int = 500
    max_list_limit: int = 500
    max_hunks: int = 200

    def __post_init__(self) -> None:
        values = (
            self.max_file_bytes,
            self.max_diff_bytes,
            self.max_response_bytes,
            self.max_line_limit,
            self.max_list_limit,
            self.max_hunks,
        )
        if any(value <= 0 for value in values):
            raise ValueError("Artifact content limits must be positive")


class ArtifactContentService:
    def __init__(
        self,
        repository: ArtifactRepository,
        storage: ArtifactStorage,
        limits: ArtifactContentLimits | None = None,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.limits = limits or ArtifactContentLimits()

    async def list_files(
        self,
        artifact: Mapping[str, Any],
        *,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        current = await self._current_artifact(artifact)
        rows = await self.repository.list_artifact_files(str(current["id"]))
        files = self._validated_manifest(current, rows)
        row_by_id = {str(row.get("id") or ""): row for row in rows}
        items = [_public_file(row_by_id[item.id], item) for item in files[offset : offset + limit]]
        response = {
            "artifact_id": str(current["id"]),
            "tree_sha256": str(current.get("tree_sha256") or ""),
            "items": items,
            "total": len(files),
            "limit": limit,
            "offset": offset,
        }
        self._ensure_response_size(response)
        return response

    async def read_file(
        self,
        artifact: Mapping[str, Any],
        file_id: str,
        *,
        start_line: int,
        line_limit: int,
    ) -> dict[str, Any]:
        self._line_page(start_line, line_limit)
        current, selected_row, item = await self.resolve_file(artifact, file_id)
        if not item.is_text or not item.content_key:
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_FILE_NOT_TEXT.value,
                "Artifact file is not UTF-8 text",
                status_code=415,
            )
        if item.size_bytes > self.limits.max_file_bytes:
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_FILE_TOO_LARGE.value,
                "Artifact file exceeds the browsing limit",
                status_code=413,
            )
        try:
            stored = await self.storage.read_text_content_range(
                item.content_key,
                start_byte=0,
                max_bytes=self.limits.max_file_bytes,
                max_object_bytes=self.limits.max_file_bytes,
                expected_size_bytes=item.size_bytes,
                expected_sha256=item.sha256,
            )
        except ArtifactStorageError as exc:
            raise _file_storage_error(exc) from exc
        try:
            text = stored.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_FILE_INVALID_UTF8.value,
                "Artifact file is not valid UTF-8",
                status_code=415,
            ) from exc
        lines = text.splitlines()
        if len(lines) != int(item.line_count or 0):
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_FILE_SHA_CHANGED.value,
                "Artifact file line metadata changed",
                status_code=409,
            )
        if lines and start_line > len(lines):
            raise self._range_error()
        if not lines and start_line != 1:
            raise self._range_error()
        first_index = start_line - 1
        selected = lines[first_index : first_index + line_limit]
        response = self._bounded_file_response(
            artifact=current,
            row=selected_row,
            item=item,
            start_line=start_line,
            lines=selected,
            total_lines=len(lines),
        )
        await self._verify_file_unchanged(current, item)
        return response

    async def resolve_file(
        self,
        artifact: Mapping[str, Any],
        file_id: str,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], ArtifactManifestFile]:
        current = await self._current_artifact(artifact)
        artifact_id = str(current["id"])
        selected_row = await self.repository.get_artifact_file(artifact_id, file_id)
        if selected_row is None:
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_FILE_NOT_FOUND.value,
                "Artifact file does not exist",
                status_code=404,
            )
        rows = await self.repository.list_artifact_files(artifact_id)
        files = self._validated_manifest(current, rows)
        item = next((value for value in files if value.id == file_id), None)
        if item is None or not _same_file_identity(selected_row, item):
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_FILE_SHA_CHANGED.value,
                "Artifact file metadata changed",
                status_code=409,
            )
        return current, selected_row, item

    async def list_diffs(
        self,
        artifact: Mapping[str, Any],
        *,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        current = await self._current_artifact(artifact)
        artifact_id = str(current["id"])
        rows = await self.repository.list_artifact_diffs(artifact_id)
        base_ids = {str(item.get("base_artifact_id") or "") for item in rows}
        base_ids.discard("")
        base_cache: dict[str, Mapping[str, Any] | None] = {}
        file_cache: dict[tuple[str, str], Mapping[str, Any]] = {
            (artifact_id, str(item.get("id") or "")): item
            for item in await self.repository.list_artifact_files(artifact_id)
        }
        for base_id in sorted(base_ids):
            base = await self.repository.get_artifact(base_id)
            base_cache[base_id] = base
            if base is not None and str(base.get("plugin_id") or "") == str(
                current.get("plugin_id") or ""
            ):
                file_cache.update(
                    {
                        (base_id, str(item.get("id") or "")): item
                        for item in await self.repository.list_artifact_files(base_id)
                    }
                )
        for item in rows:
            await self._validate_diff_binding(
                current,
                item,
                base_cache=base_cache,
                file_cache=file_cache,
            )
        response = {
            "artifact_id": str(current["id"]),
            "tree_sha256": str(current.get("tree_sha256") or ""),
            "items": [_public_diff(item) for item in rows[offset : offset + limit]],
            "total": len(rows),
            "limit": limit,
            "offset": offset,
        }
        self._ensure_response_size(response)
        return response

    async def read_diff(
        self,
        artifact: Mapping[str, Any],
        diff_id: str,
        *,
        hunk_id: str | None = None,
    ) -> dict[str, Any]:
        if hunk_id is not None and not _HUNK_ID_PATTERN.fullmatch(hunk_id):
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_DIFF_HUNK_INVALID.value,
                "Diff hunk identifier is invalid",
                status_code=400,
            )
        current = await self._current_artifact(artifact)
        artifact_id = str(current["id"])
        diff = await self.repository.get_artifact_diff(artifact_id, diff_id)
        if diff is None:
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_DIFF_NOT_FOUND.value,
                "Artifact diff does not exist",
                status_code=404,
            )
        await self._validate_diff_binding(current, diff)
        public_diff = _public_diff(diff)
        stats = diff.get("stats") if isinstance(diff.get("stats"), Mapping) else {}
        hunks_key = str(diff.get("hunks_key") or "")
        if not hunks_key:
            if stats.get("hunks_sha256") is not None or stats.get("hunks_size_bytes") is not None:
                raise ArtifactContentError(
                    ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                    "Diff hunk object binding changed",
                    status_code=409,
                )
            if hunk_id is not None:
                raise ArtifactContentError(
                    ArtifactErrorCode.ARTIFACT_DIFF_HUNK_NOT_FOUND.value,
                    "Diff hunk does not exist",
                    status_code=404,
                )
            response = _empty_diff_document(current, public_diff, stats)
            self._ensure_response_size(response)
            return response
        try:
            expected_key = build_diff_key(artifact_id, diff_id)
        except ArtifactStorageError as exc:
            raise ArtifactContentError(
                ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                "Diff identity is invalid",
                status_code=409,
            ) from exc
        if hunks_key != expected_key:
            raise ArtifactContentError(
                ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                "Diff object binding changed",
                status_code=409,
            )
        expected_sha256 = str(stats.get("hunks_sha256") or "")
        expected_size = stats.get("hunks_size_bytes")
        if (
            len(expected_sha256) != 64
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_DIFF_HUNK_UNAVAILABLE.value,
                "Diff hunk metadata is unavailable",
                status_code=409,
            )
        if expected_size > self.limits.max_diff_bytes:
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_DIFF_TOO_LARGE.value,
                "Diff hunk exceeds the browsing limit",
                status_code=413,
            )
        try:
            stored = await self.storage.read_text_content_range(
                hunks_key,
                start_byte=0,
                max_bytes=self.limits.max_diff_bytes,
                max_object_bytes=self.limits.max_diff_bytes,
                expected_size_bytes=expected_size,
                expected_sha256=expected_sha256,
            )
        except ArtifactStorageError as exc:
            raise _diff_storage_error(exc) from exc
        try:
            document = validate_hunk_payload(
                stored.content,
                diff=diff,
                artifact_id=artifact_id,
                current_tree_sha256=str(current.get("tree_sha256") or ""),
                base_tree_sha256=str(diff.get("base_tree_sha256") or "") or None,
            )
        except DiffBuildError as exc:
            code = (
                ArtifactErrorCode.DIFF_TREE_CHANGED.value
                if exc.code == ArtifactErrorCode.DIFF_TREE_CHANGED.value
                else ArtifactErrorCode.ARTIFACT_DIFF_HUNK_INVALID.value
            )
            raise ArtifactContentError(
                code, "Diff hunk validation failed", status_code=409
            ) from exc
        hunks = _validated_hunks(document.get("hunks"), maximum=self.limits.max_hunks)
        if hunk_id is not None:
            selected = [item for item in hunks if item["id"] == hunk_id]
            if not selected:
                raise ArtifactContentError(
                    ArtifactErrorCode.ARTIFACT_DIFF_HUNK_NOT_FOUND.value,
                    "Diff hunk does not exist",
                    status_code=404,
                )
            hunks = selected
        response = {
            "artifact_id": artifact_id,
            "tree_sha256": str(current.get("tree_sha256") or ""),
            "diff": public_diff,
            "hunks_available": True,
            "unavailable_reason": "",
            "schema_version": str(document.get("schema_version") or ""),
            "tool_version": str(document.get("tool_version") or ""),
            "context_lines": _non_negative_int(document.get("context_lines"), maximum=20),
            "truncated": _strict_bool(document.get("truncated")),
            "omitted_hunks": _non_negative_int(document.get("omitted_hunks")),
            "hunks": hunks,
        }
        if _json_size(response) > self.limits.max_response_bytes:
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_DIFF_TOO_LARGE.value,
                "Diff response exceeds the browsing limit",
                status_code=413,
            )
        await self._verify_diff_unchanged(current, diff)
        return response

    def _validated_manifest(
        self,
        artifact: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
    ) -> tuple[ArtifactManifestFile, ...]:
        if not rows and not str(artifact.get("tree_sha256") or ""):
            return ()
        try:
            return validate_artifact_manifest(artifact, rows)
        except (ArtifactStorageError, ValueError) as exc:
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_FILE_SHA_CHANGED.value,
                "Artifact file manifest changed",
                status_code=409,
            ) from exc

    async def _current_artifact(self, artifact: Mapping[str, Any]) -> Mapping[str, Any]:
        artifact_id = str(artifact.get("id") or "")
        current = await self.repository.get_artifact(artifact_id)
        if current is None:
            raise ArtifactContentError(
                "artifact_not_found",
                "Artifact does not exist",
                status_code=404,
            )
        return current

    async def _verify_file_unchanged(
        self,
        artifact: Mapping[str, Any],
        item: ArtifactManifestFile,
    ) -> None:
        latest = await self.repository.get_artifact(str(artifact["id"]))
        row = await self.repository.get_artifact_file(str(artifact["id"]), item.id)
        if (
            latest is None
            or row is None
            or str(latest.get("tree_sha256") or "") != str(artifact.get("tree_sha256") or "")
            or not _same_file_identity(row, item)
        ):
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_FILE_SHA_CHANGED.value,
                "Artifact file changed during read",
                status_code=409,
            )

    async def _validate_diff_binding(
        self,
        artifact: Mapping[str, Any],
        diff: Mapping[str, Any],
        *,
        base_cache: dict[str, Mapping[str, Any] | None] | None = None,
        file_cache: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    ) -> None:
        artifact_id = str(artifact.get("id") or "")
        if str(diff.get("artifact_id") or "") != artifact_id or str(
            diff.get("current_tree_sha256") or ""
        ) != str(artifact.get("tree_sha256") or ""):
            raise ArtifactContentError(
                ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                "Artifact diff tree binding changed",
                status_code=409,
            )
        base_id = str(diff.get("base_artifact_id") or "") or None
        base_tree = str(diff.get("base_tree_sha256") or "") or None
        if base_id is None:
            if base_tree is not None:
                raise ArtifactContentError(
                    ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                    "Artifact diff base binding changed",
                    status_code=409,
                )
        else:
            if base_cache is not None and base_id in base_cache:
                base = base_cache[base_id]
            else:
                base = await self.repository.get_artifact(base_id)
                if base_cache is not None:
                    base_cache[base_id] = base
            if (
                base is None
                or str(base.get("plugin_id") or "") != str(artifact.get("plugin_id") or "")
                or str(base.get("tree_sha256") or "") != base_tree
            ):
                raise ArtifactContentError(
                    ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                    "Artifact diff base tree changed",
                    status_code=409,
                )
        await self._validate_diff_files(
            artifact,
            diff,
            base_id=base_id,
            file_cache=file_cache,
        )

    async def _validate_diff_files(
        self,
        artifact: Mapping[str, Any],
        diff: Mapping[str, Any],
        *,
        base_id: str | None,
        file_cache: Mapping[tuple[str, str], Mapping[str, Any]] | None,
    ) -> None:
        artifact_id = str(artifact["id"])
        current_file_id = str(diff.get("current_file_id") or "") or None
        base_file_id = str(diff.get("base_file_id") or "") or None
        current_file = await self._bound_file(
            artifact_id,
            current_file_id,
            file_cache=file_cache,
        )
        base_file = await self._bound_file(
            base_id,
            base_file_id,
            file_cache=file_cache,
        )
        change_type = str(diff.get("change_type") or "")
        valid_sides = (
            (change_type == "added" and base_file_id is None and current_file_id is not None)
            or (change_type == "deleted" and base_file_id is not None and current_file_id is None)
            or (
                change_type in {"modified", "unchanged", "renamed"}
                and base_file_id is not None
                and current_file_id is not None
            )
        )
        if (
            not valid_sides
            or (current_file_id is not None and current_file is None)
            or (base_file_id is not None and base_file is None)
            or (current_file_id is None and diff.get("current_sha256") is not None)
            or (base_file_id is None and diff.get("base_sha256") is not None)
            or (
                current_file is not None
                and (
                    str(current_file.get("sha256") or "") != str(diff.get("current_sha256") or "")
                    or str(current_file.get("path") or "") != str(diff.get("path") or "")
                )
            )
            or (
                base_file is not None
                and (
                    str(base_file.get("sha256") or "") != str(diff.get("base_sha256") or "")
                    or str(base_file.get("path") or "") != str(diff.get("base_path") or "")
                )
            )
        ):
            raise ArtifactContentError(
                ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                "Artifact diff file binding changed",
                status_code=409,
            )

    async def _bound_file(
        self,
        artifact_id: str | None,
        file_id: str | None,
        *,
        file_cache: Mapping[tuple[str, str], Mapping[str, Any]] | None,
    ) -> Mapping[str, Any] | None:
        if artifact_id is None or file_id is None:
            return None
        if file_cache is not None:
            return file_cache.get((artifact_id, file_id))
        return await self.repository.get_artifact_file(artifact_id, file_id)

    async def _verify_diff_unchanged(
        self,
        artifact: Mapping[str, Any],
        diff: Mapping[str, Any],
    ) -> None:
        latest = await self.repository.get_artifact(str(artifact["id"]))
        stored = await self.repository.get_artifact_diff(
            str(artifact["id"]), str(diff.get("id") or "")
        )
        if (
            latest is None
            or stored is None
            or str(latest.get("tree_sha256") or "") != str(artifact.get("tree_sha256") or "")
            or _diff_identity(stored) != _diff_identity(diff)
        ):
            raise ArtifactContentError(
                ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                "Artifact diff changed during read",
                status_code=409,
            )
        await self._validate_diff_binding(latest, stored)

    def _page(self, limit: int, offset: int) -> tuple[int, int]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > self.limits.max_list_limit
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_PAGE_INVALID.value,
                "Artifact page is invalid",
                status_code=400,
            )
        return limit, offset

    def _line_page(self, start_line: int, line_limit: int) -> None:
        if (
            not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or start_line < 1
            or not isinstance(line_limit, int)
            or isinstance(line_limit, bool)
            or line_limit < 1
            or line_limit > self.limits.max_line_limit
        ):
            raise self._range_error()

    def _ensure_response_size(self, response: Mapping[str, Any]) -> None:
        if _json_size(response) > self.limits.max_response_bytes:
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_RESPONSE_TOO_LARGE.value,
                "Artifact response exceeds the browsing limit",
                status_code=413,
            )

    @staticmethod
    def _range_error() -> ArtifactContentError:
        return ArtifactContentError(
            ArtifactErrorCode.ARTIFACT_CONTENT_RANGE_INVALID.value,
            "Artifact content line range is invalid",
            status_code=416,
        )

    def _bounded_file_response(
        self,
        *,
        artifact: Mapping[str, Any],
        row: Mapping[str, Any],
        item: ArtifactManifestFile,
        start_line: int,
        lines: Sequence[str],
        total_lines: int,
    ) -> dict[str, Any]:
        selected = list(lines)
        requested_line_count = len(selected)

        def response_for(count: int) -> dict[str, Any]:
            included = selected[:count]
            end_line = start_line + len(included) - 1 if included else None
            return {
                "artifact_id": str(artifact["id"]),
                "tree_sha256": str(artifact.get("tree_sha256") or ""),
                "file": _public_file(row, item),
                "encoding": "utf-8",
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": total_lines,
                "truncated": bool(end_line is not None and end_line < total_lines),
                "lines": [
                    {"number": start_line + index, "text": value}
                    for index, value in enumerate(included)
                ],
            }

        low = 0
        high = requested_line_count
        best: dict[str, Any] | None = None
        while low <= high:
            count = (low + high) // 2
            candidate = response_for(count)
            if _json_size(candidate) <= self.limits.max_response_bytes:
                best = candidate
                low = count + 1
            else:
                high = count - 1
        if best is None or (requested_line_count and not best["lines"]):
            raise ArtifactContentError(
                ArtifactErrorCode.ARTIFACT_FILE_TOO_LARGE.value,
                "Artifact content line exceeds the browsing limit",
                status_code=413,
            )
        return best


def _public_file(row: Mapping[str, Any], item: ArtifactManifestFile) -> dict[str, Any]:
    return {
        "id": item.id,
        "artifact_id": item.artifact_id,
        "path": item.path,
        "language": item.language,
        "mime_type": item.mime_type,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "line_count": item.line_count,
        "is_text": item.is_text,
        "is_entrypoint": bool(row.get("is_entrypoint")),
        "is_reachable": bool(row.get("is_reachable")),
        "graph_status": str(row.get("graph_status") or "not_analyzed"),
        "content_available": bool(item.is_text and item.content_key),
    }


def _public_diff(diff: Mapping[str, Any]) -> dict[str, Any]:
    stats = diff.get("stats") if isinstance(diff.get("stats"), Mapping) else {}
    return {
        "id": str(diff.get("id") or ""),
        "artifact_id": str(diff.get("artifact_id") or ""),
        "base_artifact_id": str(diff.get("base_artifact_id") or "") or None,
        "base_file_id": str(diff.get("base_file_id") or "") or None,
        "current_file_id": str(diff.get("current_file_id") or "") or None,
        "path": str(diff.get("path") or ""),
        "base_path": str(diff.get("base_path") or ""),
        "change_type": str(diff.get("change_type") or ""),
        "base_sha256": str(diff.get("base_sha256") or "") or None,
        "current_sha256": str(diff.get("current_sha256") or "") or None,
        "base_tree_sha256": str(diff.get("base_tree_sha256") or "") or None,
        "current_tree_sha256": str(diff.get("current_tree_sha256") or ""),
        "stats": {key: stats[key] for key in sorted(_PUBLIC_DIFF_STATS) if key in stats},
        "has_hunks": bool(diff.get("hunks_key")),
        "created_at": diff.get("created_at"),
    }


def _empty_diff_document(
    artifact: Mapping[str, Any],
    public_diff: Mapping[str, Any],
    stats: Mapping[str, Any],
) -> dict[str, Any]:
    reason = str(stats.get("hunks_omitted_reason") or "")
    if not reason:
        reason = "binary_file" if bool(stats.get("binary")) else "diff_hunk_unavailable"
    return {
        "artifact_id": str(artifact["id"]),
        "tree_sha256": str(artifact.get("tree_sha256") or ""),
        "diff": dict(public_diff),
        "hunks_available": False,
        "unavailable_reason": reason,
        "schema_version": "",
        "tool_version": "",
        "context_lines": 0,
        "truncated": bool(stats.get("hunks_truncated")),
        "omitted_hunks": int(stats.get("hunks_omitted") or 0),
        "hunks": [],
    }


def _same_file_identity(row: Mapping[str, Any], item: ArtifactManifestFile) -> bool:
    return (
        str(row.get("artifact_id") or "") == item.artifact_id
        and str(row.get("id") or "") == item.id
        and str(row.get("path") or "") == item.path
        and str(row.get("sha256") or "") == item.sha256
        and row.get("size_bytes") == item.size_bytes
        and row.get("line_count") == item.line_count
        and row.get("is_text") is item.is_text
        and (str(row.get("content_key") or "") or None) == item.content_key
    )


def _diff_identity(diff: Mapping[str, Any]) -> tuple[Any, ...]:
    stats = diff.get("stats") if isinstance(diff.get("stats"), Mapping) else {}
    return (
        diff.get("id"),
        diff.get("artifact_id"),
        diff.get("base_artifact_id"),
        diff.get("base_file_id"),
        diff.get("current_file_id"),
        diff.get("path"),
        diff.get("base_path"),
        diff.get("change_type"),
        diff.get("base_sha256"),
        diff.get("current_sha256"),
        diff.get("base_tree_sha256"),
        diff.get("current_tree_sha256"),
        diff.get("hunks_key"),
        stats.get("hunks_sha256"),
        stats.get("hunks_size_bytes"),
    )


def _validated_hunks(value: Any, *, maximum: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ArtifactContentError(
            ArtifactErrorCode.ARTIFACT_DIFF_HUNK_INVALID.value,
            "Diff hunk list is invalid",
            status_code=409,
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hunk in value:
        if not isinstance(hunk, Mapping):
            raise _hunk_invalid()
        hunk_id = str(hunk.get("id") or "")
        if not _HUNK_ID_PATTERN.fullmatch(hunk_id) or hunk_id in seen:
            raise _hunk_invalid()
        seen.add(hunk_id)
        lines = hunk.get("lines")
        if not isinstance(lines, list):
            raise _hunk_invalid()
        public_lines = [_validated_hunk_line(item) for item in lines]
        header = hunk.get("header")
        if not isinstance(header, str) or len(header) > 512:
            raise _hunk_invalid()
        result.append(
            {
                "id": hunk_id,
                "header": header,
                "old_start": _non_negative_int(hunk.get("old_start")),
                "old_lines": _non_negative_int(hunk.get("old_lines")),
                "new_start": _non_negative_int(hunk.get("new_start")),
                "new_lines": _non_negative_int(hunk.get("new_lines")),
                "lines": public_lines,
            }
        )
    return result


def _validated_hunk_line(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _hunk_invalid()
    kind = value.get("kind")
    prefix = value.get("prefix")
    text = value.get("text")
    newline = value.get("newline")
    old_line = _optional_positive_int(value.get("old_line"))
    new_line = _optional_positive_int(value.get("new_line"))
    expected_prefix = {"context": " ", "delete": "-", "add": "+"}.get(kind)
    valid_coordinates = (
        (kind == "context" and old_line is not None and new_line is not None)
        or (kind == "delete" and old_line is not None and new_line is None)
        or (kind == "add" and old_line is None and new_line is not None)
    )
    if (
        expected_prefix is None
        or prefix != expected_prefix
        or not isinstance(text, str)
        or newline not in {"none", "lf", "crlf", "cr"}
        or not valid_coordinates
    ):
        raise _hunk_invalid()
    return {
        "kind": kind,
        "prefix": prefix,
        "text": text,
        "newline": newline,
        "old_line": old_line,
        "new_line": new_line,
    }


def _non_negative_int(value: Any, *, maximum: int | None = None) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or (maximum is not None and value > maximum)
    ):
        raise _hunk_invalid()
    return value


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _hunk_invalid()
    return value


def _strict_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _hunk_invalid()
    return value


def _hunk_invalid() -> ArtifactContentError:
    return ArtifactContentError(
        ArtifactErrorCode.ARTIFACT_DIFF_HUNK_INVALID.value,
        "Diff hunk payload is invalid",
        status_code=409,
    )


def _file_storage_error(exc: ArtifactStorageError) -> ArtifactContentError:
    if exc.code == "content_object_too_large":
        return ArtifactContentError(
            ArtifactErrorCode.ARTIFACT_FILE_TOO_LARGE.value,
            "Artifact file exceeds the browsing limit",
            status_code=413,
        )
    if exc.code in {"sha256_mismatch", "content_size_mismatch"}:
        return ArtifactContentError(
            ArtifactErrorCode.ARTIFACT_FILE_SHA_CHANGED.value,
            "Artifact file content changed",
            status_code=409,
        )
    return ArtifactContentError(
        ArtifactErrorCode.ARTIFACT_FILE_CONTENT_UNAVAILABLE.value,
        "Artifact file content is unavailable",
        status_code=409,
    )


def _diff_storage_error(exc: ArtifactStorageError) -> ArtifactContentError:
    if exc.code == "content_object_too_large":
        return ArtifactContentError(
            ArtifactErrorCode.ARTIFACT_DIFF_TOO_LARGE.value,
            "Diff hunk exceeds the browsing limit",
            status_code=413,
        )
    if exc.code in {"sha256_mismatch", "content_size_mismatch"}:
        return ArtifactContentError(
            ArtifactErrorCode.DIFF_TREE_CHANGED.value,
            "Diff hunk content changed",
            status_code=409,
        )
    return ArtifactContentError(
        ArtifactErrorCode.ARTIFACT_DIFF_HUNK_UNAVAILABLE.value,
        "Diff hunk content is unavailable",
        status_code=409,
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
