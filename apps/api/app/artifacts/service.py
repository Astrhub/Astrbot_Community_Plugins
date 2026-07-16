from __future__ import annotations

import secrets
from collections.abc import AsyncIterable, Mapping
from decimal import Decimal
from typing import Any

from .archive import PrecheckError, normalize_github_repo, normalize_version
from .github_source import GithubSourceClient
from .models import ArtifactStateError, PublicationStatus, ReviewStatus, new_domain_id
from .repository import ArtifactRepository
from .storage import ArtifactStorage, build_quarantine_key


class ArtifactServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ArtifactService:
    def __init__(
        self,
        *,
        repository: ArtifactRepository,
        storage: ArtifactStorage,
        github: GithubSourceClient,
        max_upload_bytes: int,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.github = github
        self.max_upload_bytes = max_upload_bytes

    async def close(self) -> None:
        await self.github.close()

    async def submit_upload(
        self,
        *,
        plugin: Mapping[str, Any],
        user: Mapping[str, Any],
        stream: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        artifact_id = new_domain_id("artifact")
        quarantine_key = build_quarantine_key(artifact_id)
        stored = await self.storage.put_quarantine(
            stream,
            quarantine_key,
            self.max_upload_bytes,
        )
        return await self._create_artifact(
            artifact_id=artifact_id,
            plugin=plugin,
            user=user,
            quarantine_key=quarantine_key,
            archive_sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            source_type="upload",
            source_ref="",
            source_commit_sha="",
        )

    async def submit_github(
        self,
        *,
        plugin: Mapping[str, Any],
        user: Mapping[str, Any],
        source_ref: str,
    ) -> dict[str, Any]:
        source = await self.github.resolve(str(plugin.get("repo") or ""), source_ref)
        artifact_id = new_domain_id("artifact")
        quarantine_key = build_quarantine_key(artifact_id)
        stored = await self.storage.put_quarantine(
            self.github.stream_archive(source),
            quarantine_key,
            self.max_upload_bytes,
        )
        return await self._create_artifact(
            artifact_id=artifact_id,
            plugin=plugin,
            user=user,
            quarantine_key=quarantine_key,
            archive_sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            source_type="github",
            source_ref=source.requested_ref,
            source_commit_sha=source.commit_sha,
        )

    async def artifact_detail(self, artifact_id: str) -> dict[str, Any] | None:
        artifact = await self.repository.get_artifact(artifact_id)
        if not artifact:
            return None
        return {
            "artifact": public_artifact(artifact),
            "runs": [
                public_review_run(run)
                for run in await self.repository.list_review_runs(artifact_id)
            ],
            "findings": [
                public_review_finding(finding)
                for finding in await self.repository.list_findings(artifact_id)
            ],
            "decisions": [
                public_review_decision(decision)
                for decision in await self.repository.list_review_decisions(artifact_id)
            ],
        }

    async def approve(
        self,
        *,
        artifact_id: str,
        reviewer: Mapping[str, Any],
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        artifact = await self.repository.get_artifact(artifact_id)
        if not artifact:
            raise ArtifactServiceError("artifact_not_found", "Artifact 不存在", status_code=404)
        repo_version = str(artifact.get("repo_version") or "").strip()
        if not repo_version:
            raise ArtifactServiceError(
                "repo_version_missing",
                "仓库版本尚未同步，不能发布 CDN 包",
                status_code=409,
            )
        try:
            normalized_repo_version = normalize_version(repo_version)
        except PrecheckError as exc:
            raise ArtifactServiceError(exc.code, str(exc), status_code=409) from exc
        if normalized_repo_version != str(artifact.get("normalized_version") or ""):
            raise ArtifactServiceError(
                "repo_version_changed",
                "Artifact 版本与当前仓库版本不一致，请重新提交或等待仓库同步",
                status_code=409,
            )
        try:
            approved = await self.repository.approve_artifact(
                artifact_id,
                reviewer=reviewer,
                reason=reason,
                expected_repo_version=repo_version,
                expected_normalized_version=normalized_repo_version,
                idempotency_key=idempotency_key or f"approve:{artifact_id}:{secrets.token_hex(8)}",
            )
        except ArtifactStateError:
            raise
        except ValueError as exc:
            raise ArtifactServiceError(
                str(exc), "Artifact 状态或仓库版本已变化", status_code=409
            ) from exc
        if not approved:
            raise ArtifactServiceError("artifact_not_found", "Artifact 不存在", status_code=404)
        return public_artifact(approved)

    async def reject(
        self,
        *,
        artifact_id: str,
        reviewer: Mapping[str, Any],
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not reason:
            raise ArtifactServiceError("reason_required", "拒绝时必须填写原因")
        rejected = await self.repository.decide_artifact(
            artifact_id,
            action="reject",
            target_status=ReviewStatus.REJECTED.value,
            reason=reason,
            reviewer=reviewer,
            idempotency_key=idempotency_key or f"reject:{artifact_id}:{secrets.token_hex(8)}",
        )
        if not rejected:
            raise ArtifactServiceError("artifact_not_found", "Artifact 不存在", status_code=404)
        await self.enqueue_status_event(
            artifact=rejected,
            event_type="artifact_rejected",
            suffix="rejected",
            extra={"reason": reason},
        )
        return public_artifact(rejected)

    async def request_changes(
        self,
        *,
        artifact_id: str,
        reviewer: Mapping[str, Any],
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ArtifactServiceError("reason_required", "要求修改时必须填写原因")
        try:
            changed = await self.repository.decide_artifact(
                artifact_id,
                action="request_changes",
                target_status=ReviewStatus.CHANGES_REQUESTED.value,
                reason=reason,
                reviewer=reviewer,
                idempotency_key=(
                    idempotency_key
                    or f"request-changes:{artifact_id}:{secrets.token_hex(8)}"
                ),
            )
        except ArtifactStateError:
            raise
        except ValueError as exc:
            raise ArtifactServiceError(
                str(exc), "Artifact 状态已变化", status_code=409
            ) from exc
        if not changed:
            raise ArtifactServiceError("artifact_not_found", "Artifact 不存在", status_code=404)
        await self.enqueue_status_event(
            artifact=changed,
            event_type="artifact_changes_requested",
            suffix="changes-requested",
            extra={"reason": reason},
        )
        return public_artifact(changed)

    async def retry_publish(
        self, artifact_id: str, *, reviewer: Mapping[str, Any]
    ) -> dict[str, Any]:
        artifact = await self.repository.get_artifact(artifact_id)
        if not artifact:
            raise ArtifactServiceError("artifact_not_found", "Artifact 不存在", status_code=404)
        if artifact.get("review_status") != ReviewStatus.APPROVED.value:
            raise ArtifactServiceError(
                "artifact_not_approved", "仅能重试已批准版本", status_code=409
            )
        if artifact.get("publication_status") != PublicationStatus.PUBLISH_FAILED.value:
            raise ArtifactServiceError(
                "publish_not_failed", "当前版本不处于发布失败状态", status_code=409
            )
        decision_key = f"retry-publish:{artifact_id}:{secrets.token_hex(8)}"
        await self.repository.record_decision(
            artifact_id,
            action="retry_publish",
            from_status=PublicationStatus.PUBLISH_FAILED.value,
            to_status=PublicationStatus.PUBLISHING.value,
            reason="管理员重试 CDN 发布",
            reviewer=reviewer,
            idempotency_key=decision_key,
        )
        job = await self.repository.enqueue_job(
            {
                "artifact_id": artifact_id,
                "type": "publish",
                "payload": {"expected_repo_version": artifact.get("repo_version") or ""},
                "max_attempts": 5,
                "idempotency_key": f"publish-retry:{artifact_id}:{secrets.token_hex(8)}",
            }
        )
        return {"artifact": public_artifact(artifact), "job_id": job["id"]}

    async def request_revoke(
        self,
        *,
        artifact_id: str,
        reason: str,
        idempotency_key: str,
        reviewer: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ArtifactServiceError("reason_required", "撤回 CDN 版本时必须填写原因")
        artifact = await self.repository.get_artifact(artifact_id)
        if not artifact:
            raise ArtifactServiceError("artifact_not_found", "Artifact 不存在", status_code=404)
        decision_key = idempotency_key or f"revoke:{artifact_id}:{secrets.token_hex(8)}"
        try:
            revoking = await self.repository.request_revoke_artifact(
                artifact_id,
                reason=reason,
                reviewer=reviewer,
                idempotency_key=decision_key,
            )
        except ArtifactStateError:
            raise
        except ValueError as exc:
            raise ArtifactServiceError(str(exc), "下架请求状态已变化", status_code=409) from exc
        if not revoking:
            raise ArtifactServiceError("artifact_not_found", "Artifact 不存在", status_code=404)
        return {"artifact": public_artifact(revoking)}

    async def _create_artifact(
        self,
        *,
        artifact_id: str,
        plugin: Mapping[str, Any],
        user: Mapping[str, Any],
        quarantine_key: str,
        archive_sha256: str,
        size_bytes: int,
        source_type: str,
        source_ref: str,
        source_commit_sha: str,
    ) -> dict[str, Any]:
        existing = await self.repository.get_artifact_by_sha(str(plugin["id"]), archive_sha256)
        if existing:
            await self.storage.delete_quarantine(quarantine_key)
            return public_artifact(existing)
        try:
            artifact = await self.repository.create_artifact(
                {
                    "id": artifact_id,
                    "plugin_id": plugin["id"],
                    "source_type": source_type,
                    "source_repo": normalize_github_repo(str(plugin.get("repo") or "")),
                    "source_ref": source_ref,
                    "source_commit_sha": source_commit_sha,
                    "archive_sha256": archive_sha256,
                    "size_bytes": size_bytes,
                    "quarantine_key": quarantine_key,
                    "submitted_by": user.get("id"),
                    "submitted_by_snapshot": {
                        "github_login": user.get("github_login") or "",
                        "nickname": user.get("nickname")
                        or user.get("name")
                        or user.get("internal_username")
                        or "",
                    },
                    "base_artifact_id": plugin.get("current_artifact_id"),
                }
            )
            if str(artifact["id"]) != artifact_id:
                await self.storage.delete_quarantine(quarantine_key)
                return public_artifact(artifact)
            await self.repository.enqueue_job(
                {
                    "artifact_id": artifact["id"],
                    "type": "precheck",
                    "max_attempts": 3,
                    "idempotency_key": f"precheck:{artifact['id']}",
                }
            )
        except Exception:
            await self.storage.delete_quarantine(quarantine_key)
            raise
        await self.enqueue_status_event(
            artifact=artifact,
            event_type="artifact_submitted",
            suffix="submitted",
        )
        return public_artifact(artifact)

    async def enqueue_status_event(
        self,
        *,
        artifact: Mapping[str, Any],
        event_type: str,
        suffix: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        await self.repository.enqueue_outbox(
            {
                "event_type": event_type,
                "aggregate_type": "artifact",
                "aggregate_id": artifact["id"],
                "recipient_user_id": artifact.get("submitted_by"),
                "payload": {
                    "artifact_id": artifact["id"],
                    "plugin_id": artifact["plugin_id"],
                    **dict(extra or {}),
                },
                "dedupe_key": f"artifact:{artifact['id']}:{suffix}",
            }
        )


_PUBLIC_ARTIFACT_FIELDS = {
    "id",
    "plugin_id",
    "plugin_name",
    "plugin_repo",
    "version",
    "normalized_version",
    "repo_version",
    "published_version",
    "source_type",
    "source_repo",
    "source_ref",
    "source_commit_sha",
    "archive_sha256",
    "tree_sha256",
    "size_bytes",
    "review_status",
    "publication_status",
    "risk_level",
    "rejection_code",
    "download_url",
    "submitted_by",
    "owner_user_id",
    "suggested_category",
    "category_confidence",
    "category_reason",
    "policy_version_id",
    "base_artifact_id",
    "supersedes_artifact_id",
    "review_coverage",
    "automated_review_completed_at",
    "created_at",
    "updated_at",
    "reviewed_at",
    "published_at",
    "revoked_at",
}
_PUBLIC_RUN_FIELDS = {
    "id",
    "artifact_id",
    "type",
    "status",
    "attempt",
    "summary",
    "error_code",
    "model",
    "ruleset_version",
    "tool_name",
    "tool_version",
    "policy_version_id",
    "input_sha256",
    "output_sha256",
    "coverage",
    "prompt_version",
    "result_schema_version",
    "astrbot_version",
    "python_version",
    "platform",
    "dependency_snapshot_sha256",
    "queued_at",
    "started_at",
    "completed_at",
    "created_at",
}
_PUBLIC_FINDING_FIELDS = {
    "id",
    "artifact_id",
    "run_id",
    "fingerprint",
    "rule_id",
    "file_path",
    "line_start",
    "line_end",
    "severity",
    "category",
    "message",
    "suggestion",
    "evidence_excerpt",
    "confidence",
    "status",
    "source",
    "deterministic",
    "affects_current_release",
    "version",
    "created_at",
    "status_updated_at",
}
_PUBLIC_DECISION_FIELDS = {
    "id",
    "artifact_id",
    "action",
    "from_status",
    "to_status",
    "reason",
    "reviewer_nickname",
    "policy_version",
    "policy_version_id",
    "source",
    "input_run_ids",
    "input_fingerprints",
    "coverage_sha256",
    "metadata",
    "created_at",
}
_PRIVATE_REPORT_KEYS = {
    "api_key",
    "authorization",
    "content_key",
    "credential",
    "credentials",
    "env",
    "endpoint",
    "environment",
    "idempotency_key",
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
    "response",
    "result_key",
    "secret",
    "stderr",
    "stdout",
    "token",
    "url",
    "worker_id",
}
_PRIVATE_REPORT_SUFFIXES = (
    "_api_key",
    "_credential",
    "_endpoint",
    "_key",
    "_password",
    "_secret",
    "_token",
    "_url",
)
_ADVISORY_RUN_TYPES = {"category", "llm_package", "llm_file", "llm_summary"}


def public_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick_fields(artifact, _PUBLIC_ARTIFACT_FIELDS)
    if (
        result.get("review_status") != ReviewStatus.APPROVED.value
        or result.get("publication_status") != PublicationStatus.PUBLISHED.value
    ):
        result["download_url"] = None
    if "review_coverage" in result:
        result["review_coverage"] = _public_report_value(result["review_coverage"])
    return result


def public_review_run(run: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick_fields(run, _PUBLIC_RUN_FIELDS)
    result["coverage"] = _public_report_value(result.get("coverage") or {})
    advisory = str(run.get("type") or "") in _ADVISORY_RUN_TYPES
    result["advisory"] = advisory
    result["label"] = "自动审查建议" if advisory else "确定性检查"
    return result


def public_review_finding(finding: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick_fields(finding, _PUBLIC_FINDING_FIELDS)
    advisory = not bool(finding.get("deterministic", True)) or str(
        finding.get("source") or ""
    ) == "llm"
    result["advisory"] = advisory
    result["label"] = "自动审查建议" if advisory else "确定性检查"
    return result


def public_review_decision(decision: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick_fields(decision, _PUBLIC_DECISION_FIELDS)
    result["metadata"] = _public_report_value(result.get("metadata") or {})
    return result


def _pick_fields(value: Mapping[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key in fields}


def _public_report_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _PRIVATE_REPORT_KEYS or normalized.endswith(_PRIVATE_REPORT_SUFFIXES):
                continue
            result[str(key)] = _public_report_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_public_report_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
