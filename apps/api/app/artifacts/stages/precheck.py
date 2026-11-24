from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..archive import ArchivePrechecker, PrecheckError, read_member
from ..models import JobType, ReviewStatus
from ..storage import build_content_key
from .base import StageContext, StageOutcome


class PrecheckStage:
    job_type = JobType.PRECHECK.value

    def __init__(self, *, advanced_review_enabled: bool) -> None:
        self.advanced_review_enabled = advanced_review_enabled

    async def execute(self, context: StageContext) -> StageOutcome:
        current_status = str(context.artifact["review_status"])
        if current_status == ReviewStatus.PRECHECKING.value:
            transitioned = context.artifact
        elif current_status in {
            ReviewStatus.QUARANTINED.value,
            ReviewStatus.PROCESSING_FAILED.value,
        }:
            transitioned = await context.repository.transition_review_status(
                str(context.artifact["id"]), ReviewStatus.PRECHECKING.value
            )
            if not transitioned:
                return StageOutcome.terminal_failure(
                    "artifact_state_changed",
                    "Artifact left precheck state",
                )
        else:
            return await _recover_completed_precheck(context)
        context = context.with_snapshots(artifact=transitioned)
        if self.advanced_review_enabled:
            if context.artifact.get("policy_version_id"):
                snapshot = context.artifact
                policy = context.policy or await context.repository.get_review_policy(
                    str(context.artifact["policy_version_id"])
                )
            else:
                snapshot = await context.repository.snapshot_active_review_policy(
                    str(context.artifact["id"])
                )
                policy = (
                    await context.repository.get_review_policy(str(snapshot["policy_version_id"]))
                    if snapshot and snapshot.get("policy_version_id")
                    else None
                )
            if not snapshot or not snapshot.get("policy_version_id"):
                return StageOutcome.terminal_failure(
                    "review_policy_unavailable",
                    "No validated active review policy is available",
                )
            if policy is None:
                return StageOutcome.terminal_failure(
                    "review_policy_unavailable",
                    "The fixed review policy snapshot is unavailable",
                )
            context = context.with_snapshots(artifact=snapshot, policy=policy)

        prechecker = context.require_tool("prechecker", ArchivePrechecker)
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "precheck",
                "status": "running",
                "attempt": context.attempt,
                "ruleset_version": "p1.1",
                "policy_version_id": context.artifact.get("policy_version_id"),
            }
        )
        with tempfile.TemporaryDirectory(prefix="artifact-precheck-") as directory:
            archive_path = Path(directory) / "source.zip"
            downloaded = await context.storage.download_quarantine(
                str(context.artifact["quarantine_key"]), archive_path
            )
            if downloaded.sha256 != context.artifact["archive_sha256"]:
                return StageOutcome.terminal_failure(
                    "sha256_mismatch",
                    "Quarantine artifact digest changed",
                )
            snapshot = context.artifact.get("submitted_by_snapshot")
            github_release = (
                snapshot.get("github_release") if isinstance(snapshot, Mapping) else None
            )
            expected_release_tag = (
                str(github_release.get("tag_name") or "").strip()
                if isinstance(github_release, Mapping)
                else ""
            )
            try:
                result = await asyncio.to_thread(
                    prechecker.inspect,
                    archive_path,
                    expected_repo=str(context.artifact["source_repo"]),
                    expected_release_tag=expected_release_tag,
                )
            except PrecheckError as exc:
                await context.repository.complete_review_run(
                    run["id"],
                    {
                        "status": "failed",
                        "summary": str(exc),
                        "raw_result": {"code": exc.code, "path": exc.path},
                        "error_code": exc.code,
                    },
                )
                risk = (
                    "critical" if exc.code in {"path_traversal", "zip_bomb_suspected"} else "high"
                )
                rejected = await context.repository.decide_artifact(
                    str(context.artifact["id"]),
                    action="auto_reject",
                    target_status=ReviewStatus.REJECTED.value,
                    reason=str(exc),
                    reviewer=None,
                    idempotency_key=f"precheck-reject:{context.artifact['id']}",
                    policy_version_id=context.artifact.get("policy_version_id"),
                    risk_level=risk,
                    rejection_code=exc.code,
                )
                if rejected:
                    await context.emit_status(
                        "artifact_precheck_failed",
                        "precheck-failed",
                        {"code": exc.code},
                    )
                return StageOutcome.blocked(
                    exc.code,
                    str(exc),
                    coverage={"precheck_completed": True},
                    details={"path": exc.path, "risk_level": risk},
                )

            manifests: list[dict[str, Any]] = []
            for member in result.members:
                file_id = _file_id(str(context.artifact["id"]), member.path)
                content_key = None
                if member.is_text:
                    content_key = build_content_key(str(context.artifact["id"]), file_id)
                    content = await asyncio.to_thread(read_member, archive_path, member.source_name)
                    await context.storage.put_text_content(content_key, content)
                manifest = member.as_manifest(content_key=content_key)
                manifest.update(
                    {
                        "id": file_id,
                        "flags": {"source_name": member.source_name},
                    }
                )
                manifests.append(manifest)
            updated = await context.repository.update_artifact_manifest(
                str(context.artifact["id"]),
                version=result.version,
                normalized_version=result.normalized_version,
                tree_sha256=result.tree_sha256,
            )
            if not updated:
                return StageOutcome.terminal_failure(
                    "artifact_state_changed",
                    "Artifact left precheck state",
                )
            await context.repository.replace_artifact_files(
                str(context.artifact["id"]), manifests, result.tree_sha256
            )
            await context.repository.complete_review_run(
                run["id"],
                {
                    "status": "succeeded",
                    "summary": "基础校验通过",
                    "coverage": {
                        "outcome": "completed",
                        "stage_name": "precheck",
                        "file_count": len(result.members),
                        "tree_sha256": result.tree_sha256,
                    },
                    "raw_result": {
                        "version": result.version,
                        "normalized_version": result.normalized_version,
                        "file_count": len(result.members),
                        "tree_sha256": result.tree_sha256,
                        "metadata": {
                            key: result.metadata.get(key)
                            for key in (
                                "name",
                                "display_name",
                                "desc",
                                "version",
                                "author",
                                "repo",
                                "astrbot_version",
                                "tags",
                                "category",
                            )
                            if key in result.metadata
                        },
                    },
                },
            )
        refreshed_artifact = await context.repository.get_artifact(str(context.artifact["id"]))
        if (
            refreshed_artifact
            and refreshed_artifact.get("cdn_enabled") is True
            and refreshed_artifact.get("cdn_whitelist") is True
            and str(refreshed_artifact.get("owner_role") or "") in {"admin", "core_admin"}
            and str(refreshed_artifact.get("submitter_role") or "") in {"admin", "core_admin"}
            and hasattr(context.repository, "whitelist_approve_artifact")
        ):
            try:
                await context.repository.whitelist_approve_artifact(
                    str(context.artifact["id"]),
                    reason="管理员 CDN 白名单直发，跳过安全审查",
                    expected_repo_version=str(refreshed_artifact.get("repo_version") or ""),
                    expected_normalized_version=result.normalized_version,
                    expected_version=result.version,
                    idempotency_key=f"cdn-whitelist-approve:{context.artifact['id']}",
                    precheck_run_id=str(run["id"]),
                )
                return StageOutcome.completed(
                    "管理员 CDN 白名单直发，已跳过审查并进入发布",
                    coverage={
                        "file_count": len(result.members),
                        "tree_sha256": result.tree_sha256,
                        "cdn_whitelist": True,
                    },
                )
            except (ValueError, KeyError):
                pass
        await context.repository.transition_review_status(
            str(context.artifact["id"]), ReviewStatus.SCANNING.value
        )
        await context.repository.enqueue_job(
            {
                "artifact_id": context.artifact["id"],
                "type": JobType.STATIC_SCAN.value,
                "max_attempts": 3,
                "idempotency_key": f"static:{context.artifact['id']}",
                "policy_version_id": context.artifact.get("policy_version_id"),
            }
        )
        return StageOutcome.completed(
            "基础校验通过",
            coverage={
                "file_count": len(result.members),
                "tree_sha256": result.tree_sha256,
            },
        )


def _file_id(artifact_id: str, path: str) -> str:
    digest = hashlib.sha256(f"{artifact_id}\x00{path}".encode()).hexdigest()[:32]
    return f"file_{digest}"


async def _recover_completed_precheck(context: StageContext) -> StageOutcome:
    runs = await context.repository.list_review_runs(str(context.artifact["id"]))
    policy_version_id = context.artifact.get("policy_version_id")
    matching = [
        run
        for run in runs
        if run["type"] == "precheck" and run.get("policy_version_id") == policy_version_id
    ]
    if context.artifact["review_status"] == ReviewStatus.REJECTED.value and matching:
        error_code = str(context.artifact.get("rejection_code") or "precheck_rejected")
        return StageOutcome.blocked(
            error_code,
            "Precheck already rejected the artifact",
            coverage={"recovered": True},
        )
    if any(run["status"] == "succeeded" for run in matching):
        return StageOutcome.completed(
            "Precheck side effects were already completed",
            coverage={"recovered": True},
        )
    return StageOutcome.terminal_failure(
        "artifact_state_changed",
        "Artifact left precheck state without a completed precheck run",
    )
