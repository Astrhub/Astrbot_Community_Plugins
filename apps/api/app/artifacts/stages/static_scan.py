from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..archive import ArchiveMember
from ..models import JobType, ReviewStatus
from ..static_scan import RULESET_VERSION, StaticScanner
from .base import StageContext, StageOutcome


class StaticScanStage:
    job_type = JobType.STATIC_SCAN.value

    def __init__(self, *, advanced_review_enabled: bool) -> None:
        self.advanced_review_enabled = advanced_review_enabled

    async def execute(self, context: StageContext) -> StageOutcome:
        if context.artifact["review_status"] != ReviewStatus.SCANNING.value:
            return await _recover_completed_static_scan(context)
        if self.advanced_review_enabled and (
            not context.artifact.get("policy_version_id") or context.policy is None
        ):
            return StageOutcome.terminal_failure(
                "review_policy_unavailable",
                "Artifact has no available fixed review policy snapshot",
            )
        if context.job.get("policy_version_id") != context.artifact.get("policy_version_id"):
            return StageOutcome.terminal_failure(
                "artifact_policy_snapshot_conflict",
                "Static job policy does not match the artifact snapshot",
            )

        scanner = context.require_tool("scanner", StaticScanner)
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "static",
                "status": "running",
                "attempt": context.attempt,
                "ruleset_version": RULESET_VERSION,
                "policy_version_id": context.artifact.get("policy_version_id"),
            }
        )
        files = await context.repository.list_artifact_files(str(context.artifact["id"]))
        members = tuple(_member_from_manifest(item) for item in files)
        with tempfile.TemporaryDirectory(prefix="artifact-static-") as directory:
            archive_path = Path(directory) / "source.zip"
            await context.storage.download_quarantine(
                str(context.artifact["quarantine_key"]), archive_path
            )
            findings = await asyncio.to_thread(scanner.scan, str(archive_path), members)
        await context.repository.replace_findings(context.artifact["id"], run["id"], findings)
        risk_level = scanner.risk_level(findings)
        await context.repository.complete_review_run(
            run["id"],
            {
                "status": "succeeded",
                "summary": f"静态扫描完成，共 {len(findings)} 条发现",
                "raw_result": {
                    "finding_count": len(findings),
                    "risk_level": risk_level,
                    "ruleset_version": RULESET_VERSION,
                },
                "coverage": {
                    "outcome": "blocked" if risk_level == "critical" else "completed",
                    "stage_name": "static",
                    "finding_count": len(findings),
                    "risk_level": risk_level,
                    "ruleset_version": RULESET_VERSION,
                },
            },
        )
        coverage = {
            "finding_count": len(findings),
            "risk_level": risk_level,
            "ruleset_version": RULESET_VERSION,
        }
        if risk_level == "critical":
            rejected = await context.repository.decide_artifact(
                str(context.artifact["id"]),
                action="auto_reject",
                target_status=ReviewStatus.REJECTED.value,
                reason="静态扫描发现 critical 风险",
                reviewer=None,
                idempotency_key=f"static-critical-reject:{context.artifact['id']}",
                policy_version_id=context.artifact.get("policy_version_id"),
                risk_level=risk_level,
                rejection_code="critical_static_finding",
            )
            if rejected:
                await context.emit_status(
                    "artifact_rejected",
                    "critical-rejected",
                    {"reason": "critical_static_finding"},
                )
            return StageOutcome.blocked(
                "critical_static_finding",
                "静态扫描发现 critical 风险",
                coverage=coverage,
            )
        if self.advanced_review_enabled:
            return StageOutcome.completed(
                f"静态扫描完成，共 {len(findings)} 条发现",
                coverage=coverage,
            )
        pending = await context.repository.transition_review_status(
            str(context.artifact["id"]),
            ReviewStatus.PENDING_REVIEW.value,
            risk_level=risk_level,
        )
        if pending:
            await context.with_snapshots(artifact=pending).emit_status(
                "artifact_pending_review",
                "pending-review",
            )
        return StageOutcome.completed(
            f"静态扫描完成，共 {len(findings)} 条发现",
            coverage=coverage,
        )


def _member_from_manifest(item: Mapping[str, Any]) -> ArchiveMember:
    flags = item.get("flags") if isinstance(item.get("flags"), Mapping) else {}
    return ArchiveMember(
        path=str(item["path"]),
        source_name=str(flags.get("source_name") or item["path"]),
        language=str(item.get("language") or ""),
        mime_type=str(item.get("mime_type") or "application/octet-stream"),
        sha256=str(item["sha256"]),
        size_bytes=int(item.get("size_bytes") or 0),
        line_count=item.get("line_count"),
        is_text=bool(item.get("is_text")),
    )


async def _recover_completed_static_scan(context: StageContext) -> StageOutcome:
    runs = await context.repository.list_review_runs(str(context.artifact["id"]))
    policy_version_id = context.artifact.get("policy_version_id")
    matching = [
        run
        for run in runs
        if run["type"] == "static" and run.get("policy_version_id") == policy_version_id
    ]
    if (
        context.artifact["review_status"] == ReviewStatus.REJECTED.value
        and context.artifact.get("rejection_code") == "critical_static_finding"
        and matching
    ):
        return StageOutcome.blocked(
            "critical_static_finding",
            "Static scan already rejected the artifact",
            coverage={"recovered": True},
        )
    if any(run["status"] == "succeeded" for run in matching):
        return StageOutcome.completed(
            "Static scan side effects were already completed",
            coverage={"recovered": True},
        )
    return StageOutcome.terminal_failure(
        "artifact_not_scanning",
        "Artifact is not ready for static scan and has no completed static run",
    )
