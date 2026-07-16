from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .models import ArtifactErrorCode
from .repository import ArtifactRepository
from .storage import (
    ArtifactStorage,
    ArtifactStorageError,
    build_content_key,
    build_diff_key,
)

DIFF_SCHEMA_VERSION = "1"
DIFF_TOOL_NAME = "artifact-diff"
DIFF_TOOL_VERSION = "artifact-diff-v1"

_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_UNSAFE_PATH_CHARACTER = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2060\ufeff]")
_DEFAULT_FORCED_PATHS = frozenset({"main.py", "metadata.yaml", "metadata.yml"})
_COUNT_ORDER = ("added", "deleted", "modified", "renamed", "unchanged")


@dataclass(frozen=True, slots=True)
class DiffLimits:
    max_text_file_bytes: int = 512 * 1024
    max_total_text_bytes: int = 8 * 1024 * 1024
    max_text_lines_per_file: int = 5_000
    max_total_text_lines: int = 50_000
    max_hunk_bytes: int = 256 * 1024
    max_total_hunk_bytes: int = 2 * 1024 * 1024
    context_lines: int = 3
    max_hunks_per_file: int = 200

    def __post_init__(self) -> None:
        positive = (
            self.max_text_file_bytes,
            self.max_total_text_bytes,
            self.max_text_lines_per_file,
            self.max_total_text_lines,
            self.max_hunk_bytes,
            self.max_total_hunk_bytes,
            self.max_hunks_per_file,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("Diff byte and hunk limits must be positive")
        if self.max_total_text_bytes < self.max_text_file_bytes:
            raise ValueError("Total diff text limit cannot be smaller than the per-file limit")
        if self.max_total_text_lines < self.max_text_lines_per_file:
            raise ValueError("Total diff line limit cannot be smaller than the per-file limit")
        if self.max_total_hunk_bytes < self.max_hunk_bytes:
            raise ValueError("Total diff hunk limit cannot be smaller than the per-file limit")
        if self.context_lines < 0 or self.context_lines > 20:
            raise ValueError("Diff context_lines must be between 0 and 20")

    def as_dict(self) -> dict[str, int]:
        return {
            "context_lines": self.context_lines,
            "max_hunk_bytes": self.max_hunk_bytes,
            "max_hunks_per_file": self.max_hunks_per_file,
            "max_text_file_bytes": self.max_text_file_bytes,
            "max_text_lines_per_file": self.max_text_lines_per_file,
            "max_total_hunk_bytes": self.max_total_hunk_bytes,
            "max_total_text_bytes": self.max_total_text_bytes,
            "max_total_text_lines": self.max_total_text_lines,
        }


@dataclass(frozen=True, slots=True)
class ArtifactDiffResult:
    diffs: tuple[Mapping[str, Any], ...]
    coverage: Mapping[str, Any]
    input_sha256: str
    output_sha256: str
    degraded_code: str | None = None
    blocking_code: str | None = None


class DiffBuildError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class _ManifestFile:
    id: str
    artifact_id: str
    path: str
    language: str
    mime_type: str
    sha256: str
    size_bytes: int
    line_count: int | None
    is_text: bool
    content_key: str | None
    is_entrypoint: bool

    def input_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "is_entrypoint": self.is_entrypoint,
            "is_text": self.is_text,
            "line_count": self.line_count,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    change_type: str
    path: str
    base: _ManifestFile | None
    current: _ManifestFile | None

    @property
    def base_path(self) -> str:
        return self.base.path if self.base is not None else ""


@dataclass(frozen=True, slots=True)
class _TextLine:
    raw: str
    text: str
    newline: str


@dataclass(frozen=True, slots=True)
class _HunkBuild:
    payload: bytes | None
    added_lines: int
    deleted_lines: int
    hunk_count: int
    omitted_hunks: int
    truncated: bool


class ArtifactDiffService:
    def __init__(self, limits: DiffLimits | None = None) -> None:
        self.limits = limits or DiffLimits()

    async def build(
        self,
        *,
        artifact: Mapping[str, Any],
        repository: ArtifactRepository,
        storage: ArtifactStorage,
        forced_paths: Set[str] | Sequence[str] = (),
    ) -> ArtifactDiffResult:
        artifact_id = str(artifact.get("id") or "")
        current_tree_sha256 = str(artifact.get("tree_sha256") or "")
        current_rows = await repository.list_artifact_files(artifact_id)
        try:
            current_files = _validated_manifest(artifact, current_rows)
        except ValueError as exc:
            return await self._current_manifest_degraded(
                artifact,
                repository,
                current_rows,
                reason="diff_current_manifest_incomplete",
                detail=str(exc),
            )

        requested_base_id = str(artifact.get("base_artifact_id") or "") or None
        compared_base: Mapping[str, Any] | None = None
        base_files: tuple[_ManifestFile, ...] = ()
        base_reason: str | None = None
        base_detail = ""
        if requested_base_id is None:
            base_reason = "diff_base_missing"
            base_detail = "Artifact has no submission-time base artifact"
        else:
            compared_base = await repository.get_artifact(requested_base_id)
            if (
                compared_base is None
                or requested_base_id == artifact_id
                or str(compared_base.get("plugin_id") or "") != str(artifact.get("plugin_id") or "")
            ):
                compared_base = None
                base_reason = ArtifactErrorCode.DIFF_BASE_INVALID.value
                base_detail = (
                    "Submission-time base artifact is missing or belongs to another plugin"
                )
            else:
                base_rows = await repository.list_artifact_files(requested_base_id)
                try:
                    base_files = _validated_manifest(compared_base, base_rows)
                except ValueError as exc:
                    compared_base = None
                    base_files = ()
                    base_reason = "diff_base_manifest_incomplete"
                    base_detail = str(exc)

        forced = _forced_review_paths(current_files, base_files, forced_paths)
        candidates = _classify(base_files, current_files)
        compared_base_id = str(compared_base.get("id") or "") if compared_base else None
        base_tree_sha256 = str(compared_base.get("tree_sha256") or "") if compared_base else None
        input_sha256 = _input_sha256(
            artifact,
            current_files,
            requested_base_id=requested_base_id,
            compared_base_id=compared_base_id,
            base_tree_sha256=base_tree_sha256,
            base_files=base_files,
            forced_paths=forced,
            limits=self.limits,
            base_reason=base_reason,
        )
        records: list[dict[str, Any]] = []
        degraded_reasons: list[str] = []
        if base_reason:
            degraded_reasons.append(base_reason)
        total_text_bytes = 0
        total_text_lines = 0
        total_hunk_bytes = 0
        hunk_file_count = 0
        binary_file_count = 0

        limits_sha256 = _sha256_json(self.limits.as_dict())
        for candidate in candidates:
            diff_id = _diff_id(
                artifact_id=artifact_id,
                compared_base_id=compared_base_id,
                base_tree_sha256=base_tree_sha256,
                current_tree_sha256=current_tree_sha256,
                path=candidate.path,
                base_path=candidate.base_path,
                limits_sha256=limits_sha256,
            )
            stats = _base_stats(candidate, forced)
            hunks_key: str | None = None
            needs_hunk = candidate.change_type not in {"unchanged", "renamed"}
            text_eligible = all(
                item is None or item.is_text for item in (candidate.base, candidate.current)
            )
            if needs_hunk and not text_eligible:
                binary_file_count += 1
                stats.update(
                    {
                        "binary": True,
                        "hunks_complete": True,
                        "hunks_omitted_reason": "binary_file",
                    }
                )
            elif needs_hunk:
                content_bytes = sum(
                    item.size_bytes
                    for item in (candidate.base, candidate.current)
                    if item is not None
                )
                reason = _content_limit_reason(
                    candidate,
                    total_text_bytes=total_text_bytes,
                    total_text_lines=total_text_lines,
                    limits=self.limits,
                )
                if reason:
                    _append_unique(degraded_reasons, reason)
                    stats.update(
                        {
                            "hunks_complete": False,
                            "hunks_omitted_reason": reason,
                        }
                    )
                else:
                    base_text, base_error = await _read_text(candidate.base, storage, self.limits)
                    current_text, current_error = await _read_text(
                        candidate.current,
                        storage,
                        self.limits,
                    )
                    content_error = base_error or current_error
                    if content_error:
                        _append_unique(degraded_reasons, content_error)
                        stats.update(
                            {
                                "hunks_complete": False,
                                "hunks_omitted_reason": content_error,
                            }
                        )
                    else:
                        total_text_bytes += content_bytes
                        total_text_lines += sum(
                            int(item.line_count or 0)
                            for item in (candidate.base, candidate.current)
                            if item is not None
                        )
                        remaining_output = self.limits.max_total_hunk_bytes - total_hunk_bytes
                        output_limit = min(self.limits.max_hunk_bytes, remaining_output)
                        hunk_build = _build_hunk_document(
                            diff_id=diff_id,
                            artifact_id=artifact_id,
                            compared_base_id=compared_base_id,
                            base_tree_sha256=base_tree_sha256,
                            current_tree_sha256=current_tree_sha256,
                            candidate=candidate,
                            base_text=base_text,
                            current_text=current_text,
                            limits=self.limits,
                            output_limit=output_limit,
                        )
                        stats.update(
                            {
                                "added_lines": hunk_build.added_lines,
                                "deleted_lines": hunk_build.deleted_lines,
                                "hunk_count": hunk_build.hunk_count,
                                "hunks_complete": not hunk_build.truncated,
                                "hunks_omitted": hunk_build.omitted_hunks,
                                "hunks_truncated": hunk_build.truncated,
                            }
                        )
                        if hunk_build.truncated:
                            _append_unique(degraded_reasons, "diff_hunks_truncated")
                        if hunk_build.payload is not None:
                            hunks_key = build_diff_key(artifact_id, diff_id)
                            stored = await storage.put_text_content(
                                hunks_key,
                                hunk_build.payload,
                            )
                            stats.update(
                                {
                                    "hunks_sha256": stored.sha256,
                                    "hunks_size_bytes": stored.size_bytes,
                                }
                            )
                            total_hunk_bytes += stored.size_bytes
                            hunk_file_count += 1
            records.append(
                {
                    "id": diff_id,
                    "base_file_id": candidate.base.id if candidate.base else None,
                    "current_file_id": candidate.current.id if candidate.current else None,
                    "path": candidate.path,
                    "base_path": candidate.base_path,
                    "change_type": candidate.change_type,
                    "base_sha256": candidate.base.sha256 if candidate.base else None,
                    "current_sha256": candidate.current.sha256 if candidate.current else None,
                    "hunks_key": hunks_key,
                    "stats": stats,
                }
            )

        try:
            saved = await repository.replace_artifact_diffs(
                artifact_id,
                compared_base_id,
                current_tree_sha256=current_tree_sha256,
                base_tree_sha256=base_tree_sha256,
                diffs=records,
            )
        except ValueError as exc:
            code = str(exc)
            if code not in {
                ArtifactErrorCode.DIFF_BASE_INVALID.value,
                ArtifactErrorCode.DIFF_TREE_CHANGED.value,
            }:
                code = ArtifactErrorCode.DIFF_TREE_CHANGED.value
            raise DiffBuildError(
                code, "Artifact tree changed while persisting diff", retryable=True
            ) from exc

        output_sha256 = _output_sha256(records)
        counts = Counter(item["change_type"] for item in records)
        coverage: dict[str, Any] = {
            "outcome": "completed",
            "stage_name": "diff",
            "complete": not degraded_reasons,
            "full_review_required": bool(degraded_reasons),
            "requested_base_artifact_id": requested_base_id,
            "compared_base_artifact_id": compared_base_id,
            "base_tree_sha256": base_tree_sha256,
            "current_tree_sha256": current_tree_sha256,
            "counts": {name: counts.get(name, 0) for name in _COUNT_ORDER},
            "file_count": len(records),
            "forced_review_files": sum(
                bool(item["stats"].get("forced_review")) for item in records
            ),
            "binary_files": binary_file_count,
            "hunk_files": hunk_file_count,
            "text_bytes": total_text_bytes,
            "text_lines": total_text_lines,
            "hunk_bytes": total_hunk_bytes,
            "input_sha256": input_sha256,
            "output_sha256": output_sha256,
            "tool_version": DIFF_TOOL_VERSION,
        }
        if degraded_reasons:
            coverage["reason"] = degraded_reasons[0]
            coverage["degraded_reasons"] = degraded_reasons
        if base_detail:
            coverage["base_validation"] = {
                "status": "degraded",
                "reason": base_reason,
            }
        return ArtifactDiffResult(
            diffs=tuple(saved),
            coverage=coverage,
            input_sha256=input_sha256,
            output_sha256=output_sha256,
            degraded_code=degraded_reasons[0] if degraded_reasons else None,
            blocking_code=None,
        )

    async def _current_manifest_degraded(
        self,
        artifact: Mapping[str, Any],
        repository: ArtifactRepository,
        rows: Sequence[Mapping[str, Any]],
        *,
        reason: str,
        detail: str,
    ) -> ArtifactDiffResult:
        artifact_id = str(artifact.get("id") or "")
        current_tree_sha256 = str(artifact.get("tree_sha256") or "")
        latest = await repository.get_artifact(artifact_id)
        clear_tree_sha256 = str((latest or {}).get("tree_sha256") or current_tree_sha256)
        if _SHA256_PATTERN.fullmatch(clear_tree_sha256):
            try:
                await repository.replace_artifact_diffs(
                    artifact_id,
                    None,
                    current_tree_sha256=clear_tree_sha256,
                    base_tree_sha256=None,
                    diffs=(),
                )
            except ValueError:
                pass
        input_sha256 = _sha256_json(
            {
                "artifact_id": artifact_id,
                "current_tree_sha256": current_tree_sha256,
                "manifest": [
                    {
                        "id": str(item.get("id") or ""),
                        "path": str(item.get("path") or ""),
                        "sha256": str(item.get("sha256") or ""),
                    }
                    for item in rows
                ],
                "reason": reason,
                "tool_version": DIFF_TOOL_VERSION,
            }
        )
        output_sha256 = _output_sha256(())
        coverage = {
            "outcome": "degraded",
            "stage_name": "diff",
            "complete": False,
            "full_review_required": True,
            "reason": reason,
            "degraded_reasons": [reason],
            "requested_base_artifact_id": artifact.get("base_artifact_id"),
            "compared_base_artifact_id": None,
            "base_tree_sha256": None,
            "current_tree_sha256": current_tree_sha256,
            "counts": {name: 0 for name in _COUNT_ORDER},
            "file_count": 0,
            "input_sha256": input_sha256,
            "output_sha256": output_sha256,
            "tool_version": DIFF_TOOL_VERSION,
            "manifest_validation": {"status": "degraded", "reason": reason},
        }
        return ArtifactDiffResult(
            diffs=(),
            coverage=coverage,
            input_sha256=input_sha256,
            output_sha256=output_sha256,
            degraded_code=reason,
            blocking_code=reason,
        )


def manifest_tree_sha256(files: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: str(value.get("path") or "")):
        path = str(item.get("path") or "")
        sha256 = str(item.get("sha256") or "")
        if not path or not _SHA256_PATTERN.fullmatch(sha256):
            raise ValueError("Manifest path and SHA-256 are required")
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_hunk_payload(
    payload: bytes,
    *,
    diff: Mapping[str, Any],
    artifact_id: str,
    current_tree_sha256: str,
    base_tree_sha256: str | None,
) -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiffBuildError(
            "diff_hunk_invalid", "Diff hunk payload is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(document, dict):
        raise DiffBuildError("diff_hunk_invalid", "Diff hunk payload must be a JSON object")
    expected = {
        "schema_version": DIFF_SCHEMA_VERSION,
        "tool_version": DIFF_TOOL_VERSION,
        "diff_id": str(diff.get("id") or ""),
        "artifact_id": artifact_id,
        "base_artifact_id": diff.get("base_artifact_id"),
        "path": str(diff.get("path") or ""),
        "base_path": str(diff.get("base_path") or ""),
        "change_type": str(diff.get("change_type") or ""),
        "base_tree_sha256": base_tree_sha256,
        "current_tree_sha256": current_tree_sha256,
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise DiffBuildError(
            ArtifactErrorCode.DIFF_TREE_CHANGED.value,
            "Diff hunk identity or tree binding is stale",
        )
    for side, file_id_key, sha_key in (
        ("base", "base_file_id", "base_sha256"),
        ("current", "current_file_id", "current_sha256"),
    ):
        identity = document.get(side)
        expected_file_id = diff.get(file_id_key)
        expected_sha256 = diff.get(sha_key)
        if expected_file_id is None:
            if identity is not None:
                raise DiffBuildError("diff_hunk_invalid", "Diff hunk has an unexpected file side")
            continue
        if not isinstance(identity, dict) or (
            identity.get("file_id") != expected_file_id or identity.get("sha256") != expected_sha256
        ):
            raise DiffBuildError(
                ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                "Diff hunk file binding is stale",
            )
    hunks = document.get("hunks")
    if not isinstance(hunks, list):
        raise DiffBuildError("diff_hunk_invalid", "Diff hunk list is invalid")
    return document


def _validated_manifest(
    artifact: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> tuple[_ManifestFile, ...]:
    artifact_id = str(artifact.get("id") or "")
    tree_sha256 = str(artifact.get("tree_sha256") or "")
    if not artifact_id or not _SHA256_PATTERN.fullmatch(tree_sha256):
        raise ValueError("Artifact identity or tree SHA-256 is invalid")
    if not rows:
        raise ValueError("Artifact manifest is empty")
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    normalized: list[_ManifestFile] = []
    for row in rows:
        file_id = str(row.get("id") or "")
        path = str(row.get("path") or "")
        sha256 = str(row.get("sha256") or "")
        row_artifact_id = str(row.get("artifact_id") or artifact_id)
        if row_artifact_id != artifact_id:
            raise ValueError("Manifest file belongs to another artifact")
        _validate_manifest_path(path)
        if path in seen or path.casefold() in seen_casefold:
            raise ValueError("Manifest contains duplicate or case-conflicting paths")
        seen.add(path)
        seen_casefold.add(path.casefold())
        if not _SHA256_PATTERN.fullmatch(sha256):
            raise ValueError("Manifest file SHA-256 is invalid")
        size_value = row.get("size_bytes")
        if not isinstance(size_value, int) or isinstance(size_value, bool) or size_value < 0:
            raise ValueError("Manifest file size is invalid")
        size_bytes = size_value
        is_text_value = row.get("is_text")
        if not isinstance(is_text_value, bool):
            raise ValueError("Manifest text flag is invalid")
        is_text = is_text_value
        line_count = row.get("line_count")
        if is_text:
            if not isinstance(line_count, int) or isinstance(line_count, bool) or line_count < 0:
                raise ValueError("Text manifest line count is invalid")
        else:
            line_count = None
        try:
            expected_key = build_content_key(artifact_id, file_id)
        except ArtifactStorageError as exc:
            raise ValueError("Manifest file ID is invalid") from exc
        content_key = str(row.get("content_key") or "") or None
        if is_text and content_key != expected_key:
            raise ValueError("Text manifest content key is missing or not server-derived")
        if not is_text and content_key is not None:
            raise ValueError("Binary manifest unexpectedly contains a text content key")
        normalized.append(
            _ManifestFile(
                id=file_id,
                artifact_id=artifact_id,
                path=path,
                language=str(row.get("language") or ""),
                mime_type=str(row.get("mime_type") or "application/octet-stream"),
                sha256=sha256,
                size_bytes=size_bytes,
                line_count=line_count,
                is_text=is_text,
                content_key=content_key,
                is_entrypoint=bool(row.get("is_entrypoint")),
            )
        )
    if manifest_tree_sha256([item.input_record() for item in normalized]) != tree_sha256:
        raise ValueError("Artifact tree SHA-256 does not match the registered manifest")
    return tuple(sorted(normalized, key=lambda item: item.path))


def _validate_manifest_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or len(path) > 512
        or "\\" in path
        or _UNSAFE_PATH_CHARACTER.search(path)
        or unicodedata.normalize("NFC", path) != path
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or parsed.as_posix() != path
    ):
        raise ValueError("Manifest file path is unsafe")


def _forced_review_paths(
    current: Sequence[_ManifestFile],
    base: Sequence[_ManifestFile],
    policy_paths: Set[str] | Sequence[str],
) -> frozenset[str]:
    forced = set(_DEFAULT_FORCED_PATHS)
    forced.update(str(path) for path in policy_paths if str(path))
    for item in (*base, *current):
        lower = item.path.lower()
        if (
            item.is_entrypoint
            or lower == "requirements.txt"
            or (lower.startswith("requirements/") and lower.endswith(".txt"))
        ):
            forced.add(item.path)
    return frozenset(forced)


def _classify(
    base: Sequence[_ManifestFile],
    current: Sequence[_ManifestFile],
) -> tuple[_Candidate, ...]:
    base_by_path = {item.path: item for item in base}
    current_by_path = {item.path: item for item in current}
    candidates: list[_Candidate] = []
    shared_paths = sorted(base_by_path.keys() & current_by_path.keys())
    for path in shared_paths:
        base_file = base_by_path[path]
        current_file = current_by_path[path]
        candidates.append(
            _Candidate(
                "unchanged" if base_file.sha256 == current_file.sha256 else "modified",
                path,
                base_file,
                current_file,
            )
        )

    deleted = {path: base_by_path[path] for path in base_by_path.keys() - current_by_path.keys()}
    added = {path: current_by_path[path] for path in current_by_path.keys() - base_by_path.keys()}
    deleted_by_sha: dict[str, list[_ManifestFile]] = defaultdict(list)
    added_by_sha: dict[str, list[_ManifestFile]] = defaultdict(list)
    for item in deleted.values():
        deleted_by_sha[item.sha256].append(item)
    for item in added.values():
        added_by_sha[item.sha256].append(item)
    for sha256 in sorted(deleted_by_sha.keys() & added_by_sha.keys()):
        old = deleted_by_sha[sha256]
        new = added_by_sha[sha256]
        if len(old) != 1 or len(new) != 1:
            continue
        base_file = old[0]
        current_file = new[0]
        candidates.append(_Candidate("renamed", current_file.path, base_file, current_file))
        deleted.pop(base_file.path)
        added.pop(current_file.path)
    candidates.extend(
        _Candidate("deleted", path, item, None) for path, item in sorted(deleted.items())
    )
    candidates.extend(_Candidate("added", path, None, item) for path, item in sorted(added.items()))
    return tuple(sorted(candidates, key=lambda item: item.path))


def _base_stats(candidate: _Candidate, forced: Set[str]) -> dict[str, Any]:
    return {
        "base_size_bytes": candidate.base.size_bytes if candidate.base else None,
        "current_size_bytes": candidate.current.size_bytes if candidate.current else None,
        "base_line_count": candidate.base.line_count if candidate.base else None,
        "current_line_count": candidate.current.line_count if candidate.current else None,
        "forced_review": bool({candidate.path, candidate.base_path} & set(forced)),
        "binary": not all(
            item is None or item.is_text for item in (candidate.base, candidate.current)
        ),
        "added_lines": 0,
        "deleted_lines": 0,
        "hunk_count": 0,
        "hunks_complete": True,
        "hunks_omitted": 0,
        "hunks_truncated": False,
    }


def _content_limit_reason(
    candidate: _Candidate,
    *,
    total_text_bytes: int,
    total_text_lines: int,
    limits: DiffLimits,
) -> str | None:
    sizes = [item.size_bytes for item in (candidate.base, candidate.current) if item is not None]
    line_counts = [
        int(item.line_count or 0)
        for item in (candidate.base, candidate.current)
        if item is not None
    ]
    if any(size > limits.max_text_file_bytes for size in sizes):
        return "diff_text_file_too_large"
    if any(line_count > limits.max_text_lines_per_file for line_count in line_counts):
        return "diff_text_file_too_many_lines"
    if total_text_bytes + sum(sizes) > limits.max_total_text_bytes:
        return "diff_text_budget_exceeded"
    if total_text_lines + sum(line_counts) > limits.max_total_text_lines:
        return "diff_text_line_budget_exceeded"
    return None


async def _read_text(
    item: _ManifestFile | None,
    storage: ArtifactStorage,
    limits: DiffLimits,
) -> tuple[str, str | None]:
    if item is None:
        return "", None
    try:
        content = await storage.read_text_content(
            str(item.content_key),
            limits.max_text_file_bytes,
            item.sha256,
        )
    except ArtifactStorageError:
        return "", "diff_content_unavailable"
    try:
        return content.decode("utf-8"), None
    except UnicodeDecodeError:
        return "", "diff_content_invalid_utf8"


def _build_hunk_document(
    *,
    diff_id: str,
    artifact_id: str,
    compared_base_id: str | None,
    base_tree_sha256: str | None,
    current_tree_sha256: str,
    candidate: _Candidate,
    base_text: str,
    current_text: str,
    limits: DiffLimits,
    output_limit: int,
) -> _HunkBuild:
    base_lines = _split_text_lines(base_text)
    current_lines = _split_text_lines(current_text)
    matcher = difflib.SequenceMatcher(
        None,
        [line.raw for line in base_lines],
        [line.raw for line in current_lines],
        autojunk=True,
    )
    groups = list(matcher.get_grouped_opcodes(limits.context_lines))
    hunks = [
        _structured_hunk(group, base_lines, current_lines, index)
        for index, group in enumerate(groups, start=1)
    ]
    added_lines = sum(1 for hunk in hunks for line in hunk["lines"] if line["kind"] == "add")
    deleted_lines = sum(1 for hunk in hunks for line in hunk["lines"] if line["kind"] == "delete")
    included = hunks[: limits.max_hunks_per_file]
    truncated = len(included) < len(hunks)
    document = _hunk_envelope(
        diff_id=diff_id,
        artifact_id=artifact_id,
        compared_base_id=compared_base_id,
        base_tree_sha256=base_tree_sha256,
        current_tree_sha256=current_tree_sha256,
        candidate=candidate,
        limits=limits,
        hunks=included,
        truncated=truncated,
        omitted_hunks=len(hunks) - len(included),
    )
    payload = _canonical_json_bytes(document)
    while included and (output_limit <= 0 or len(payload) > output_limit):
        included.pop()
        truncated = True
        document = _hunk_envelope(
            diff_id=diff_id,
            artifact_id=artifact_id,
            compared_base_id=compared_base_id,
            base_tree_sha256=base_tree_sha256,
            current_tree_sha256=current_tree_sha256,
            candidate=candidate,
            limits=limits,
            hunks=included,
            truncated=True,
            omitted_hunks=len(hunks) - len(included),
        )
        payload = _canonical_json_bytes(document)
    if not included and hunks:
        payload = None
        truncated = True
    elif output_limit <= 0 or len(payload) > output_limit:
        payload = None
        truncated = bool(hunks)
    return _HunkBuild(
        payload=payload,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        hunk_count=len(included),
        omitted_hunks=len(hunks) - len(included),
        truncated=truncated,
    )


def _hunk_envelope(
    *,
    diff_id: str,
    artifact_id: str,
    compared_base_id: str | None,
    base_tree_sha256: str | None,
    current_tree_sha256: str,
    candidate: _Candidate,
    limits: DiffLimits,
    hunks: Sequence[Mapping[str, Any]],
    truncated: bool,
    omitted_hunks: int,
) -> dict[str, Any]:
    return {
        "schema_version": DIFF_SCHEMA_VERSION,
        "tool_version": DIFF_TOOL_VERSION,
        "diff_id": diff_id,
        "artifact_id": artifact_id,
        "base_artifact_id": compared_base_id,
        "path": candidate.path,
        "base_path": candidate.base_path,
        "change_type": candidate.change_type,
        "base_tree_sha256": base_tree_sha256,
        "current_tree_sha256": current_tree_sha256,
        "base": _hunk_file_identity(candidate.base),
        "current": _hunk_file_identity(candidate.current),
        "context_lines": limits.context_lines,
        "truncated": truncated,
        "omitted_hunks": omitted_hunks,
        "hunks": list(hunks),
    }


def _structured_hunk(
    group: Sequence[tuple[str, int, int, int, int]],
    base_lines: Sequence[_TextLine],
    current_lines: Sequence[_TextLine],
    index: int,
) -> dict[str, Any]:
    old_first, old_last = group[0][1], group[-1][2]
    new_first, new_last = group[0][3], group[-1][4]
    old_start, old_count = _unified_range(old_first, old_last)
    new_start, new_count = _unified_range(new_first, new_last)
    lines: list[dict[str, Any]] = []
    for tag, old_start_index, old_end, new_start_index, new_end in group:
        if tag == "equal":
            for offset, line in enumerate(base_lines[old_start_index:old_end]):
                lines.append(
                    _hunk_line(
                        "context",
                        line,
                        old_start_index + offset + 1,
                        new_start_index + offset + 1,
                    )
                )
        if tag in {"delete", "replace"}:
            for offset, line in enumerate(base_lines[old_start_index:old_end]):
                lines.append(_hunk_line("delete", line, old_start_index + offset + 1, None))
        if tag in {"insert", "replace"}:
            for offset, line in enumerate(current_lines[new_start_index:new_end]):
                lines.append(_hunk_line("add", line, None, new_start_index + offset + 1))
    return {
        "id": f"hunk-{index}",
        "header": (
            f"@@ -{_format_unified_range(old_start, old_count)} "
            f"+{_format_unified_range(new_start, new_count)} @@"
        ),
        "old_start": old_start,
        "old_lines": old_count,
        "new_start": new_start,
        "new_lines": new_count,
        "lines": lines,
    }


def _hunk_line(
    kind: str,
    line: _TextLine,
    old_line: int | None,
    new_line: int | None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "prefix": {"context": " ", "delete": "-", "add": "+"}[kind],
        "text": line.text,
        "newline": line.newline,
        "old_line": old_line,
        "new_line": new_line,
    }


def _split_text_lines(value: str) -> tuple[_TextLine, ...]:
    lines: list[_TextLine] = []
    for raw in value.splitlines(keepends=True):
        if raw.endswith("\r\n"):
            text, newline = raw[:-2], "crlf"
        elif raw.endswith("\n"):
            text, newline = raw[:-1], "lf"
        elif raw.endswith("\r"):
            text, newline = raw[:-1], "cr"
        else:
            text, newline = raw, "none"
        lines.append(_TextLine(raw=raw, text=text, newline=newline))
    return tuple(lines)


def _unified_range(start: int, stop: int) -> tuple[int, int]:
    count = stop - start
    line = start + 1
    if count == 0:
        line -= 1
    return line, count


def _format_unified_range(start: int, count: int) -> str:
    return str(start) if count == 1 else f"{start},{count}"


def _hunk_file_identity(item: _ManifestFile | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "file_id": item.id,
        "path": item.path,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "line_count": item.line_count,
    }


def _diff_id(
    *,
    artifact_id: str,
    compared_base_id: str | None,
    base_tree_sha256: str | None,
    current_tree_sha256: str,
    path: str,
    base_path: str,
    limits_sha256: str,
) -> str:
    digest = hashlib.sha256(
        "\x00".join(
            (
                DIFF_TOOL_VERSION,
                artifact_id,
                compared_base_id or "",
                base_tree_sha256 or "",
                current_tree_sha256,
                path,
                base_path,
                limits_sha256,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"diff_{digest[:32]}"


def _input_sha256(
    artifact: Mapping[str, Any],
    current_files: Sequence[_ManifestFile],
    *,
    requested_base_id: str | None,
    compared_base_id: str | None,
    base_tree_sha256: str | None,
    base_files: Sequence[_ManifestFile],
    forced_paths: Set[str],
    limits: DiffLimits,
    base_reason: str | None,
) -> str:
    return _sha256_json(
        {
            "schema_version": DIFF_SCHEMA_VERSION,
            "tool_version": DIFF_TOOL_VERSION,
            "artifact_id": artifact.get("id"),
            "archive_sha256": artifact.get("archive_sha256"),
            "plugin_id": artifact.get("plugin_id"),
            "current_tree_sha256": artifact.get("tree_sha256"),
            "requested_base_artifact_id": requested_base_id,
            "compared_base_artifact_id": compared_base_id,
            "base_tree_sha256": base_tree_sha256,
            "base_validation_reason": base_reason,
            "current_manifest": [item.input_record() for item in current_files],
            "base_manifest": [item.input_record() for item in base_files],
            "forced_paths": sorted(forced_paths),
            "limits": limits.as_dict(),
        }
    )


def diff_output_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    return _sha256_json(
        {
            "schema_version": DIFF_SCHEMA_VERSION,
            "tool_version": DIFF_TOOL_VERSION,
            "diffs": [_output_record(item) for item in records],
        }
    )


def _output_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    return diff_output_sha256(records)


def _output_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "base_file_id": item.get("base_file_id"),
        "current_file_id": item.get("current_file_id"),
        "path": item.get("path"),
        "base_path": item.get("base_path", ""),
        "change_type": item.get("change_type"),
        "base_sha256": item.get("base_sha256"),
        "current_sha256": item.get("current_sha256"),
        "hunks_key": item.get("hunks_key"),
        "stats": dict(item.get("stats") or {}),
    }


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
