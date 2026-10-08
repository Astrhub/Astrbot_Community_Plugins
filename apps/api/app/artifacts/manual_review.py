from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from ..cdn import publication_payload
from .models import new_domain_id

REVIEWABLE = frozenset({"scanning", "pending_review", "processing_failed"})
REVIEW_JOB_TYPES = frozenset(
    {
        "precheck",
        "static_scan",
        "diff_graph",
        "clamav_scan",
        "yara_scan",
        "runtime_dispatch",
        "runtime_collect",
        "dependency_scan",
        "category",
        "llm_package",
        "llm_file",
        "llm_summary",
        "route_review",
    }
)


def request_digest(action: str, actor: Mapping[str, Any], reason: str, sha256: str) -> str:
    value = [action, str(actor.get("id") or ""), reason, sha256]
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode()).hexdigest()


def validate_action(
    artifact: Mapping[str, Any],
    action: str,
    actor: Mapping[str, Any],
    reason: str,
    sha256: str,
    confirmed: bool,
) -> None:
    if actor.get("role") not in {"admin", "core_admin"}:
        raise ValueError("review_admin_required")
    if action not in {"comment", "manual_approve", "retry_review"}:
        raise ValueError("review_action_invalid")
    if not reason.strip() or len(reason) > 10000:
        raise ValueError("review_reason_required")
    if sha256 != artifact.get("archive_sha256"):
        raise ValueError("artifact_identity_changed")
    if action == "comment":
        return
    if (
        artifact.get("review_status") not in REVIEWABLE
        or artifact.get("publication_status") != "unpublished"
    ):
        raise ValueError("artifact_not_reviewable")
    if not confirmed:
        raise ValueError("review_confirmation_required")
    if action == "manual_approve":
        if (
            actor.get("role") != "core_admin"
            and str(artifact.get("owner_user_id") or "") == str(actor.get("id") or "")
        ):
            raise ValueError("self_approval_forbidden")
        if not artifact.get("tree_sha256") or not artifact.get("normalized_version"):
            raise ValueError("artifact_manifest_missing")
        from .archive import normalize_version

        if (
            normalize_version(str(artifact.get("repo_version") or ""))
            != artifact["normalized_version"]
        ):
            raise ValueError("repo_version_changed")


def decision_metadata(
    artifact: Mapping[str, Any], runs: list[Mapping[str, Any]], digest: str
) -> dict[str, Any]:
    return {
        "request_sha256": digest,
        "archive_sha256": artifact["archive_sha256"],
        "review_coverage": deepcopy(artifact.get("review_coverage") or {}),
        "scan_snapshot": [
            {key: run.get(key) for key in ("id", "type", "status", "error_code")} for run in runs
        ],
    }


def approval_notification(
    artifact: Mapping[str, Any], decision_id: str, reason: str
) -> dict[str, Any]:
    return {
        "id": new_domain_id("outbox"),
        "event_type": "artifact_approved",
        "aggregate_type": "artifact",
        "aggregate_id": artifact["id"],
        "recipient_user_id": artifact.get("submitted_by") or artifact.get("owner_user_id"),
        "payload": {"artifact_id": artifact["id"], "decision_id": decision_id, "reason": reason},
        "dedupe_key": f"manual-approved:{decision_id}",
    }


class PgManualReviewMixin:
    async def review_action(
        self,
        artifact_id: str,
        *,
        action: str,
        actor: Mapping[str, Any],
        reason: str,
        archive_sha256: str,
        confirmed: bool,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        digest = request_digest(action, actor, reason, archive_sha256)
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """SELECT a.*, p.owner_user_id, p.repo_version,
                         COALESCE((p.metadata->>'cdn_generation')::integer, 0) AS cdn_generation
                         FROM plugin_artifacts a JOIN market_plugins p ON p.id = a.plugin_id
                        WHERE a.id = $1 FOR UPDATE OF a, p""",
                    artifact_id,
                )
                if row is None:
                    return None
                existing = await connection.fetchrow(
                    "SELECT * FROM review_decisions WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing:
                    if (
                        existing["artifact_id"] != artifact_id
                        or existing["metadata"].get("request_sha256") != digest
                    ):
                        raise ValueError("idempotency_key_conflict")
                    return dict(row)
                validate_action(row, action, actor, reason, archive_sha256, confirmed)
                jobs = await connection.fetch(
                    "SELECT * FROM artifact_jobs WHERE artifact_id = $1 FOR UPDATE",
                    artifact_id,
                )
                dispatches = await connection.fetch(
                    "SELECT * FROM runtime_dispatches WHERE artifact_id = $1 FOR UPDATE",
                    artifact_id,
                )
                if action == "retry_review" and (
                    any(j["status"] == "running" for j in jobs)
                    or any(d["status"] in {"queued", "running"} for d in dispatches)
                ):
                    raise ValueError("review_still_running")
                runs = await connection.fetch(
                    "SELECT * FROM review_runs WHERE artifact_id = $1 AND superseded_at IS NULL ORDER BY created_at",
                    artifact_id,
                )
                target = row["review_status"]
                if action == "manual_approve":
                    target = "approved"
                elif action == "retry_review":
                    target = "scanning" if row["tree_sha256"] else "processing_failed"
                metadata = decision_metadata(row, list(runs), digest)
                decision_id = new_domain_id("decision")
                await connection.execute(
                    """INSERT INTO review_decisions (
                        id, artifact_id, action, from_status, to_status, reason,
                        reviewer_user_id, reviewer_nickname, policy_version, idempotency_key,
                        source, policy_version_id, metadata
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,
                        COALESCE((SELECT version FROM review_policies WHERE id=$9),'p1'),
                        $10,'admin',$9,$11::jsonb)""",
                    decision_id,
                    artifact_id,
                    action,
                    row["review_status"],
                    target,
                    reason,
                    actor.get("id"),
                    str(
                        actor.get("github_login")
                        or actor.get("nickname")
                        or actor.get("username")
                        or "管理员"
                    ),
                    row["policy_version_id"],
                    idempotency_key,
                    metadata,
                )
                if action == "comment":
                    return dict(row)
                await connection.execute(
                    """UPDATE artifact_jobs SET status='cancelled', lease_owner=NULL,
                        lease_expires_at=NULL, completed_at=now(), updated_at=now()
                        WHERE artifact_id=$1 AND type=ANY($2::text[]) AND status IN ('queued','running','failed')""",
                    artifact_id,
                    list(REVIEW_JOB_TYPES),
                )
                if action == "manual_approve":
                    await connection.execute(
                        """UPDATE review_runs SET status='cancelled', error_code='manual_review_completed',
                            summary='管理员已完成人工审查，取消自动检查', completed_at=now()
                            WHERE artifact_id=$1 AND status IN ('queued','running')""",
                        artifact_id,
                    )
                    await connection.execute(
                        """UPDATE runtime_dispatches SET status='cancelled', error_code='manual_review_completed',
                            completed_at=now(), lease_owner=NULL, lease_expires_at=NULL
                            WHERE artifact_id=$1 AND status IN ('queued','running')""",
                        artifact_id,
                    )
                    await connection.execute(
                        "UPDATE review_comments SET locked_at=COALESCE(locked_at,now()) WHERE artifact_id=$1",
                        artifact_id,
                    )
                else:
                    # Keep historical reports; release their dedupe keys for a new review attempt.
                    await connection.execute(
                        """UPDATE review_runs SET superseded_at=now(),
                            idempotency_key=CASE WHEN idempotency_key IS NULL THEN NULL ELSE idempotency_key || ':superseded:' || id END
                            WHERE artifact_id=$1 AND superseded_at IS NULL AND type <> 'precheck'""",
                        artifact_id,
                    )
                    await connection.execute(
                        """UPDATE artifact_jobs SET superseded_at=now(),
                            idempotency_key=idempotency_key || ':superseded:' || id
                            WHERE artifact_id=$1 AND superseded_at IS NULL AND type=ANY($2::text[])""",
                        artifact_id,
                        list(REVIEW_JOB_TYPES),
                    )
                updated = await connection.fetchrow(
                    """UPDATE plugin_artifacts SET review_status=$2, updated_at=now(),
                        reviewed_at=CASE WHEN $2='approved' THEN now() ELSE NULL END,
                        automated_review_completed_at=CASE WHEN $2='approved' THEN automated_review_completed_at ELSE NULL END,
                        review_coverage=CASE WHEN $2='approved' THEN
                            review_coverage || jsonb_build_object('manual_approval', $3::text)
                            ELSE '{}'::jsonb END
                        WHERE id=$1 RETURNING *""",
                    artifact_id,
                    target,
                    decision_id,
                )
                job_type = (
                    "publish"
                    if action == "manual_approve"
                    else ("static_scan" if row["tree_sha256"] else "precheck")
                )
                await connection.execute(
                    """INSERT INTO artifact_jobs (id,artifact_id,type,payload,idempotency_key,policy_version_id,max_attempts)
                        VALUES ($1,$2,$3,$4::jsonb,$5,$6,5)""",
                    new_domain_id("job"),
                    artifact_id,
                    job_type,
                    publication_payload(row, {"expected_repo_version": row["repo_version"]})
                    if action == "manual_approve"
                    else {},
                    f"{job_type}:{decision_id}",
                    row["policy_version_id"],
                )
                if action == "manual_approve":
                    event = approval_notification(row, decision_id, reason)
                    await connection.execute(
                        """INSERT INTO outbox_events (id,event_type,aggregate_type,aggregate_id,
                            recipient_user_id,payload,dedupe_key)
                            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)""",
                        event["id"],
                        event["event_type"],
                        event["aggregate_type"],
                        event["aggregate_id"],
                        event["recipient_user_id"],
                        event["payload"],
                        event["dedupe_key"],
                    )
                return dict(updated)


class InMemoryManualReviewMixin:
    async def review_action(
        self,
        artifact_id: str,
        *,
        action: str,
        actor: Mapping[str, Any],
        reason: str,
        archive_sha256: str,
        confirmed: bool,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        digest = request_digest(action, actor, reason, archive_sha256)
        async with self._lock:
            artifact = self.artifacts.get(artifact_id)
            if artifact is None:
                return None
            for existing in self.decisions.values():
                if existing["idempotency_key"] == idempotency_key:
                    if (
                        existing["artifact_id"] != artifact_id
                        or existing["metadata"].get("request_sha256") != digest
                    ):
                        raise ValueError("idempotency_key_conflict")
                    return deepcopy(artifact)
            row = self._with_plugin(artifact)
            validate_action(row, action, actor, reason, archive_sha256, confirmed)
            jobs = [j for j in self.jobs.values() if j.get("artifact_id") == artifact_id]
            dispatches = [d for d in self.dispatches.values() if d["artifact_id"] == artifact_id]
            if action == "retry_review" and (
                any(j["status"] == "running" for j in jobs)
                or any(d["status"] in {"queued", "running"} for d in dispatches)
            ):
                raise ValueError("review_still_running")
            runs = [
                r
                for r in self.runs.values()
                if r["artifact_id"] == artifact_id and not r.get("superseded_at")
            ]
            now = datetime.now(UTC).isoformat()
            target = artifact["review_status"]
            if action == "manual_approve":
                target = "approved"
            elif action == "retry_review":
                target = "scanning" if artifact["tree_sha256"] else "processing_failed"
            decision_id = new_domain_id("decision")
            self.decisions[decision_id] = {
                "id": decision_id,
                "artifact_id": artifact_id,
                "action": action,
                "from_status": artifact["review_status"],
                "to_status": target,
                "reason": reason,
                "reviewer_user_id": actor.get("id"),
                "reviewer_nickname": str(
                    actor.get("github_login")
                    or actor.get("nickname")
                    or actor.get("username")
                    or "管理员"
                ),
                "policy_version": self.policies.get(artifact.get("policy_version_id"), {}).get(
                    "version", "p1"
                ),
                "policy_version_id": artifact.get("policy_version_id"),
                "source": "admin",
                "input_run_ids": [],
                "input_fingerprints": [],
                "coverage_sha256": "",
                "metadata": decision_metadata(row, runs, digest),
                "idempotency_key": idempotency_key,
                "created_at": now,
            }
            if action == "comment":
                return deepcopy(artifact)
            for job in jobs:
                if job["type"] not in REVIEW_JOB_TYPES:
                    continue
                if job["status"] in {"queued", "running", "failed"}:
                    job.update(
                        status="cancelled",
                        lease_owner=None,
                        lease_expires_at=None,
                        completed_at=now,
                        updated_at=now,
                    )
                if action == "retry_review" and not job.get("superseded_at"):
                    job["superseded_at"] = now
                    job["idempotency_key"] += ":superseded:" + job["id"]
            if action == "manual_approve":
                for run in runs:
                    if run["status"] in {"queued", "running"}:
                        run.update(
                            status="cancelled",
                            error_code="manual_review_completed",
                            summary="管理员已完成人工审查，取消自动检查",
                            completed_at=now,
                        )
                for dispatch in dispatches:
                    if dispatch["status"] in {"queued", "running"}:
                        dispatch.update(
                            status="cancelled",
                            error_code="manual_review_completed",
                            completed_at=now,
                            lease_owner=None,
                            lease_expires_at=None,
                        )
                for thread in self.review_comments.values():
                    if thread["artifact_id"] == artifact_id:
                        thread["locked_at"] = thread.get("locked_at") or now
            else:
                for run in runs:
                    if run["type"] != "precheck":
                        run["superseded_at"] = now
                        if run.get("idempotency_key"):
                            run["idempotency_key"] += ":superseded:" + run["id"]
                artifact["review_coverage"] = {}
                artifact["automated_review_completed_at"] = None
            if action == "manual_approve":
                artifact["review_coverage"] = {
                    **artifact.get("review_coverage", {}),
                    "manual_approval": decision_id,
                }
            artifact.update(
                review_status=target,
                updated_at=now,
                reviewed_at=now if target == "approved" else None,
            )
            job_type = (
                "publish"
                if action == "manual_approve"
                else ("static_scan" if artifact["tree_sha256"] else "precheck")
            )
            job_id = new_domain_id("job")
            self.jobs[job_id] = {
                "id": job_id,
                "artifact_id": artifact_id,
                "type": job_type,
                "status": "queued",
                "payload": publication_payload(row, {"expected_repo_version": row["repo_version"]})
                if action == "manual_approve"
                else {},
                "attempts": 0,
                "max_attempts": 5,
                "available_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "idempotency_key": f"{job_type}:{decision_id}",
                "policy_version_id": artifact.get("policy_version_id"),
                "run_id": None,
                "stage_name": "",
                "last_error_code": "",
                "last_error": "",
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            if action == "manual_approve":
                await self.enqueue_outbox(approval_notification(row, decision_id, reason))
            return deepcopy(artifact)
