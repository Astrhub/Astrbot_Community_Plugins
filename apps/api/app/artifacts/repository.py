from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import asyncpg

from .advanced_repository import (
    InMemoryAdvancedReviewRepositoryMixin,
    PgAdvancedReviewRepositoryMixin,
)
from .models import (
    ArtifactErrorCode,
    ArtifactStateError,
    JobStatus,
    JobType,
    PublicationStatus,
    ReviewStatus,
    TERMINAL_REVIEW_STATUSES,
    new_domain_id,
    review_target_for_decision,
    validate_publication_transition,
    validate_review_transition,
)


class ArtifactRepository(Protocol):
    async def create_artifact(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None: ...

    async def get_artifact_by_sha(
        self, plugin_id: str, archive_sha256: str
    ) -> dict[str, Any] | None: ...

    async def list_user_artifacts(
        self, user_id: str, limit: int, offset: int
    ) -> list[dict[str, Any]]: ...

    async def list_review_queue(
        self,
        *,
        review_status: str,
        risk_level: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]: ...

    async def replace_artifact_files(
        self, artifact_id: str, files: Sequence[Mapping[str, Any]], tree_sha256: str
    ) -> list[dict[str, Any]]: ...

    async def update_artifact_manifest(
        self,
        artifact_id: str,
        *,
        version: str,
        normalized_version: str,
        tree_sha256: str,
    ) -> dict[str, Any] | None: ...

    async def list_artifact_files(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def get_artifact_file(self, artifact_id: str, file_id: str) -> dict[str, Any] | None: ...

    async def get_artifact_category_state(self, artifact_id: str) -> dict[str, Any] | None: ...

    async def apply_category_suggestion(
        self,
        artifact_id: str,
        *,
        suggested_category: str,
        confidence: float,
        reason: str,
        minimum_confidence: float,
    ) -> dict[str, Any] | None: ...

    async def create_review_policy(
        self,
        payload: Mapping[str, Any],
        event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def get_review_policy(self, policy_id: str) -> dict[str, Any] | None: ...

    async def get_active_review_policy(self) -> dict[str, Any] | None: ...

    async def list_review_policies(self, limit: int, offset: int) -> list[dict[str, Any]]: ...

    async def append_review_policy_event(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def list_review_policy_events(self, policy_id: str) -> list[dict[str, Any]]: ...

    async def transition_review_policy(
        self,
        policy_id: str,
        *,
        action: str,
        expected_policy_sha256: str,
        expected_active_policy_id: str | None,
        validation_summary: Mapping[str, Any] | None,
        event: Mapping[str, Any],
    ) -> dict[str, Any] | None: ...

    async def bind_artifact_policy(
        self, artifact_id: str, policy_id: str
    ) -> dict[str, Any] | None: ...

    async def snapshot_active_review_policy(
        self,
        artifact_id: str,
    ) -> dict[str, Any] | None: ...

    async def migrate_artifact_policy(
        self,
        artifact_id: str,
        target_policy_id: str,
        *,
        actor: Mapping[str, Any],
        reason: str,
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    async def update_artifact_review_coverage(
        self,
        artifact_id: str,
        coverage: Mapping[str, Any],
        *,
        automated_review_completed: bool = False,
    ) -> dict[str, Any] | None: ...

    async def replace_artifact_diffs(
        self,
        artifact_id: str,
        base_artifact_id: str | None,
        *,
        current_tree_sha256: str,
        base_tree_sha256: str | None,
        diffs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]: ...

    async def list_artifact_diffs(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def get_artifact_diff(self, artifact_id: str, diff_id: str) -> dict[str, Any] | None: ...

    async def replace_dependency_edges(
        self,
        artifact_id: str,
        *,
        tree_sha256: str,
        edges: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]: ...

    async def list_dependency_edges(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def replace_artifact_graph(
        self,
        artifact_id: str,
        *,
        tree_sha256: str,
        files: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        coverage: Mapping[str, Any],
        base_artifact_id: str | None = None,
        base_tree_sha256: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...

    async def create_runtime_dispatch(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def get_runtime_dispatch(self, dispatch_id: str) -> dict[str, Any] | None: ...

    async def claim_runtime_dispatches(
        self, runner_id: str, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]: ...

    async def renew_runtime_dispatch_lease(
        self, dispatch_id: str, runner_id: str, lease_seconds: int
    ) -> bool: ...

    async def complete_runtime_dispatch(
        self, dispatch_id: str, runner_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    async def expire_runtime_dispatches(self, limit: int) -> list[dict[str, Any]]: ...

    async def collect_runtime_dispatch(
        self,
        dispatch_id: str,
        run_payload: Mapping[str, Any] | None = None,
        findings: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any] | None: ...

    async def create_review_comment(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def append_review_comment_event(
        self, thread_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    async def list_review_comments(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def lock_review_comments(self, artifact_id: str) -> int: ...

    async def update_finding_state(
        self, finding_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    async def list_finding_events(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def create_artifact_sbom(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def list_artifact_sboms(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def get_review_history_sources(self, artifact_id: str) -> dict[str, Any]: ...

    async def create_review_run(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def complete_review_run(
        self, run_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    async def list_review_runs(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def fail_open_review_runs(
        self,
        artifact_id: str,
        run_type: str,
        *,
        error_code: str,
        summary: str,
    ) -> int: ...

    async def replace_findings(
        self, artifact_id: str, run_id: str, findings: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]: ...

    async def list_findings(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def transition_review_status(
        self,
        artifact_id: str,
        target: str,
        *,
        risk_level: str | None = None,
        rejection_code: str | None = None,
    ) -> dict[str, Any] | None: ...

    async def transition_publication_status(
        self, artifact_id: str, target: str
    ) -> dict[str, Any] | None: ...

    async def enqueue_job(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def list_artifact_jobs(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def claim_jobs(
        self, worker_id: str, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]: ...

    async def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool: ...

    async def complete_job(self, job_id: str, worker_id: str) -> bool: ...

    async def fail_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
        retry: bool,
        retry_delay_seconds: int = 0,
    ) -> bool: ...

    async def decide_artifact(
        self,
        artifact_id: str,
        *,
        action: str,
        target_status: str,
        reason: str,
        reviewer: Mapping[str, Any] | None,
        idempotency_key: str,
        policy_version: str = "p1",
        policy_version_id: str | None = None,
        source: str | None = None,
        input_run_ids: Sequence[str] = (),
        input_fingerprints: Sequence[str] = (),
        coverage_sha256: str = "",
        metadata: Mapping[str, Any] | None = None,
        risk_level: str | None = None,
        rejection_code: str | None = None,
    ) -> dict[str, Any] | None: ...

    async def approve_artifact(
        self,
        artifact_id: str,
        *,
        reviewer: Mapping[str, Any],
        reason: str,
        expected_repo_version: str,
        expected_normalized_version: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    async def auto_approve_artifact(
        self,
        artifact_id: str,
        *,
        reason: str,
        expected_repo_version: str,
        expected_normalized_version: str,
        expected_version: str,
        idempotency_key: str,
        policy_version_id: str,
        input_run_ids: Sequence[str],
        input_fingerprints: Sequence[str],
        coverage_sha256: str,
        metadata: Mapping[str, Any],
        risk_level: str,
    ) -> dict[str, Any] | None: ...

    async def request_revoke_artifact(
        self,
        artifact_id: str,
        *,
        reason: str,
        reviewer: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    async def publish_artifact(
        self,
        artifact_id: str,
        *,
        expected_repo_version: str,
        published_key: str,
        download_url: str,
    ) -> dict[str, Any] | None: ...

    async def revoke_artifact(self, artifact_id: str) -> dict[str, Any] | None: ...

    async def list_current_publications(
        self, plugin_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...

    async def list_review_decisions(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def record_decision(
        self,
        artifact_id: str,
        *,
        action: str,
        from_status: str,
        to_status: str,
        reason: str,
        reviewer: Mapping[str, Any] | None,
        idempotency_key: str,
        policy_version: str = "p1",
        policy_version_id: str | None = None,
        source: str | None = None,
        input_run_ids: Sequence[str] = (),
        input_fingerprints: Sequence[str] = (),
        coverage_sha256: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def enqueue_outbox(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def list_pending_outbox(self, limit: int) -> list[dict[str, Any]]: ...

    async def claim_outbox(
        self, worker_id: str, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]: ...

    async def complete_outbox(self, event_id: str, worker_id: str) -> bool: ...

    async def fail_outbox(
        self,
        event_id: str,
        worker_id: str,
        *,
        error_message: str,
        retry: bool,
        retry_delay_seconds: int = 0,
    ) -> bool: ...

    async def mark_outbox_delivered(self, event_id: str) -> bool: ...


class PgArtifactRepository(PgAdvancedReviewRepositoryMixin):
    def __init__(self, store: Any) -> None:
        self.store = store

    def rebind_store(self, store: Any) -> None:
        self.store = store

    async def create_artifact(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        artifact_id = str(payload.get("id") or new_domain_id("artifact"))
        path_suffix = str(payload.get("path_suffix") or secrets.token_hex(5))
        row = await self._pool().fetchrow(
            """
            INSERT INTO plugin_artifacts (
                id, plugin_id, version, normalized_version, source_type, source_repo,
                source_ref, source_commit_sha, archive_sha256, tree_sha256, size_bytes,
                quarantine_key, path_suffix, submitted_by, submitted_by_snapshot,
                base_artifact_id, supersedes_artifact_id, policy_version_id,
                review_coverage, suggested_category, category_confidence,
                category_reason
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                $14, $15::jsonb, $16, $17, $18, $19::jsonb, $20, $21, $22
            )
            ON CONFLICT (plugin_id, archive_sha256) DO UPDATE
               SET plugin_id = EXCLUDED.plugin_id
            RETURNING *
            """,
            artifact_id,
            payload["plugin_id"],
            payload.get("version", ""),
            payload.get("normalized_version", ""),
            payload["source_type"],
            payload["source_repo"],
            payload.get("source_ref", ""),
            payload.get("source_commit_sha", ""),
            payload["archive_sha256"],
            payload.get("tree_sha256", ""),
            int(payload.get("size_bytes") or 0),
            payload["quarantine_key"],
            path_suffix,
            payload.get("submitted_by"),
            dict(payload.get("submitted_by_snapshot") or {}),
            payload.get("base_artifact_id"),
            payload.get("supersedes_artifact_id"),
            payload.get("policy_version_id"),
            dict(payload.get("review_coverage") or {}),
            payload.get("suggested_category", ""),
            payload.get("category_confidence"),
            payload.get("category_reason", ""),
        )
        return _record(row)

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT a.*,
                   p.name AS plugin_name,
                   p.repo AS plugin_repo,
                   p.repo_version,
                   p.current_artifact_id,
                   p.owner_user_id,
                   p.owner_github_login,
                   current.version AS published_version
              FROM plugin_artifacts a
              JOIN market_plugins p ON p.id = a.plugin_id
         LEFT JOIN plugin_artifacts current ON current.id = p.current_artifact_id
             WHERE a.id = $1
            """,
            artifact_id,
        )
        return _record(row) if row else None

    async def get_artifact_by_sha(
        self, plugin_id: str, archive_sha256: str
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT a.*,
                   p.name AS plugin_name,
                   p.repo AS plugin_repo,
                   p.repo_version,
                   p.current_artifact_id,
                   p.owner_user_id,
                   p.owner_github_login,
                   current.version AS published_version
              FROM plugin_artifacts a
              JOIN market_plugins p ON p.id = a.plugin_id
         LEFT JOIN plugin_artifacts current ON current.id = p.current_artifact_id
             WHERE a.plugin_id = $1
               AND a.archive_sha256 = $2
            """,
            plugin_id,
            archive_sha256,
        )
        return _record(row) if row else None

    async def list_user_artifacts(
        self, user_id: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT a.*,
                   p.name AS plugin_name,
                   p.repo AS plugin_repo,
                   p.repo_version,
                   current.version AS published_version
              FROM plugin_artifacts a
              JOIN market_plugins p ON p.id = a.plugin_id
         LEFT JOIN plugin_artifacts current ON current.id = p.current_artifact_id
             WHERE p.owner_user_id = $1 OR a.submitted_by = $1
          ORDER BY a.created_at DESC
             LIMIT $2 OFFSET $3
            """,
            user_id,
            limit,
            offset,
        )
        return [_record(row) for row in rows]

    async def list_review_queue(
        self,
        *,
        review_status: str = "",
        risk_level: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT a.*,
                   p.name AS plugin_name,
                   p.repo AS plugin_repo,
                   p.repo_version,
                   p.owner_user_id,
                   p.owner_github_login,
                   current.version AS published_version
              FROM plugin_artifacts a
              JOIN market_plugins p ON p.id = a.plugin_id
         LEFT JOIN plugin_artifacts current ON current.id = p.current_artifact_id
             WHERE ($1 = '' OR a.review_status = $1)
               AND ($2 = '' OR a.risk_level = $2)
          ORDER BY a.created_at ASC
             LIMIT $3 OFFSET $4
            """,
            review_status,
            risk_level,
            limit,
            offset,
        )
        return [_record(row) for row in rows]

    async def replace_artifact_files(
        self,
        artifact_id: str,
        files: Sequence[Mapping[str, Any]],
        tree_sha256: str,
    ) -> list[dict[str, Any]]:
        inserted: list[dict[str, Any]] = []
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM artifact_files WHERE artifact_id = $1", artifact_id
                )
                for item in files:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO artifact_files (
                            id, artifact_id, path, language, mime_type, sha256,
                            size_bytes, line_count, is_text, content_key, flags,
                            is_entrypoint, is_reachable, graph_status, scan_summary
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11::jsonb, $12, $13, $14, $15::jsonb
                        )
                        RETURNING *
                        """,
                        item.get("id") or new_domain_id("file"),
                        artifact_id,
                        item["path"],
                        item.get("language", ""),
                        item.get("mime_type", "application/octet-stream"),
                        item["sha256"],
                        int(item.get("size_bytes") or 0),
                        item.get("line_count"),
                        bool(item.get("is_text")),
                        item.get("content_key"),
                        dict(item.get("flags") or {}),
                        bool(item.get("is_entrypoint")),
                        bool(item.get("is_reachable")),
                        item.get("graph_status", "not_analyzed"),
                        dict(item.get("scan_summary") or {}),
                    )
                    inserted.append(_record(row))
                await connection.execute(
                    """
                    UPDATE plugin_artifacts
                       SET tree_sha256 = $2,
                           updated_at = now()
                     WHERE id = $1
                    """,
                    artifact_id,
                    tree_sha256,
                )
        return inserted

    async def update_artifact_manifest(
        self,
        artifact_id: str,
        *,
        version: str,
        normalized_version: str,
        tree_sha256: str,
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE plugin_artifacts
               SET version = $2,
                   normalized_version = $3,
                   tree_sha256 = $4,
                   updated_at = now()
             WHERE id = $1
               AND review_status = 'prechecking'
         RETURNING *
            """,
            artifact_id,
            version,
            normalized_version,
            tree_sha256,
        )
        return _record(row) if row else None

    async def list_artifact_files(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM artifact_files WHERE artifact_id = $1 ORDER BY path",
            artifact_id,
        )
        return [_record(row) for row in rows]

    async def get_artifact_file(self, artifact_id: str, file_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM artifact_files WHERE artifact_id = $1 AND id = $2",
            artifact_id,
            file_id,
        )
        return _record(row) if row else None

    async def get_artifact_category_state(self, artifact_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT p.category,
                   p.category_source,
                   CASE
                       WHEN p.metadata->>'category_explicit' = 'false' THEN false
                       ELSE true
                   END AS category_explicit,
                   p.suggested_category,
                   p.category_confidence,
                   p.category_reason
              FROM plugin_artifacts a
              JOIN market_plugins p ON p.id = a.plugin_id
             WHERE a.id = $1
            """,
            artifact_id,
        )
        return _record(row) if row else None

    async def apply_category_suggestion(
        self,
        artifact_id: str,
        *,
        suggested_category: str,
        confidence: float,
        reason: str,
        minimum_confidence: float,
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                artifact = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET suggested_category = $2,
                           category_confidence = $3,
                           category_reason = $4,
                           updated_at = now()
                     WHERE id = $1
                       AND review_status = 'scanning'
                 RETURNING *
                    """,
                    artifact_id,
                    suggested_category,
                    confidence,
                    reason,
                )
                if artifact is None:
                    return None
                plugin = await connection.fetchrow(
                    """
                    WITH target AS (
                        SELECT p.id,
                               (
                                   $2 <> 'other'
                                   AND $3::numeric >= $5::numeric
                                   AND (
                                       p.category_source = 'ai'
                                       OR (
                                           p.category = 'other'
                                           AND p.category_source = 'user'
                                           AND COALESCE(
                                               p.metadata->>'category_explicit', 'true'
                                           ) = 'false'
                                       )
                                   )
                               ) AS should_apply
                          FROM market_plugins p
                          JOIN plugin_artifacts a ON a.plugin_id = p.id
                         WHERE a.id = $1
                           FOR UPDATE OF p
                    )
                    UPDATE market_plugins p
                       SET suggested_category = $2,
                           category_confidence = $3::numeric,
                           category_reason = $4,
                           category = CASE
                               WHEN target.should_apply THEN $2
                               ELSE p.category
                           END,
                           category_source = CASE
                               WHEN target.should_apply THEN 'ai'
                               ELSE p.category_source
                           END,
                           updated_at = now()
                      FROM target
                     WHERE p.id = target.id
                 RETURNING p.category,
                           p.category_source,
                           p.suggested_category,
                           p.category_confidence,
                           p.category_reason,
                           target.should_apply AS category_applied
                    """,
                    artifact_id,
                    suggested_category,
                    confidence,
                    reason,
                    minimum_confidence,
                )
                if plugin is None:
                    raise RuntimeError("artifact_plugin_missing")
        return {"artifact": _record(artifact), **_record(plugin)}

    async def create_review_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        pool = self._pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                idempotency_key = payload.get("idempotency_key")
                if idempotency_key:
                    existing = await connection.fetchrow(
                        """
                        SELECT * FROM review_runs
                         WHERE idempotency_key = $1
                        """,
                        idempotency_key,
                    )
                    if existing and (
                        str(existing["artifact_id"]) != str(payload["artifact_id"])
                        or str(existing["type"]) != str(payload["type"])
                    ):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                artifact = await connection.fetchrow(
                    "SELECT policy_version_id FROM plugin_artifacts WHERE id = $1 FOR SHARE",
                    payload["artifact_id"],
                )
                if not artifact:
                    raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                policy_version_id = _resolved_policy_snapshot(
                    artifact["policy_version_id"],
                    payload.get("policy_version_id"),
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO review_runs (
                        id, artifact_id, type, status, attempt, ruleset_version,
                        model, summary, raw_result, raw_result_key, error_code, started_at,
                        completed_at, tool_name, tool_version, policy_version_id, input_sha256,
                        output_sha256, coverage, prompt_version, result_schema_version,
                        container_image_digest, astrbot_version, python_version, platform,
                        dependency_snapshot_sha256, worker_id, idempotency_key
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11,
                        CASE WHEN $4 = 'running' THEN now() ELSE NULL END,
                        CASE
                            WHEN $4 IN ('succeeded', 'failed', 'timed_out', 'cancelled')
                            THEN now()
                            ELSE NULL
                        END,
                        $12, $13, $14, $15, $16, $17::jsonb, $18, $19, $20,
                        $21, $22, $23, $24, $25, $26
                    )
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
                    RETURNING *
                    """,
                    payload.get("id") or new_domain_id("run"),
                    payload["artifact_id"],
                    payload["type"],
                    payload.get("status", "queued"),
                    int(payload.get("attempt") or 1),
                    payload.get("ruleset_version", ""),
                    payload.get("model", ""),
                    payload.get("summary", ""),
                    dict(payload.get("raw_result") or {}),
                    payload.get("raw_result_key"),
                    payload.get("error_code", ""),
                    payload.get("tool_name", ""),
                    payload.get("tool_version", ""),
                    policy_version_id,
                    payload.get("input_sha256", ""),
                    payload.get("output_sha256", ""),
                    dict(payload.get("coverage") or {}),
                    payload.get("prompt_version", ""),
                    payload.get("result_schema_version", ""),
                    payload.get("container_image_digest", ""),
                    payload.get("astrbot_version", ""),
                    payload.get("python_version", ""),
                    payload.get("platform", ""),
                    payload.get("dependency_snapshot_sha256", ""),
                    payload.get("worker_id", ""),
                    payload.get("idempotency_key"),
                )
        saved = _record(row)
        if (
            str(saved["artifact_id"]) != str(payload["artifact_id"])
            or str(saved["type"]) != str(payload["type"])
            or saved.get("policy_version_id") != policy_version_id
        ):
            raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
        return saved

    async def complete_review_run(
        self, run_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE review_runs
               SET status = $2,
                   summary = $3,
                   raw_result = $4::jsonb,
                   raw_result_key = $5,
                   error_code = $6,
                   output_sha256 = COALESCE(NULLIF($7, ''), output_sha256),
                   coverage = COALESCE($8::jsonb, coverage),
                   container_image_digest = COALESCE(NULLIF($9, ''), container_image_digest),
                   dependency_snapshot_sha256 = COALESCE(
                       NULLIF($10, ''), dependency_snapshot_sha256
                   ),
                   worker_id = COALESCE(NULLIF($11, ''), worker_id),
                   input_sha256 = COALESCE(NULLIF($12, ''), input_sha256),
                   completed_at = now()
             WHERE id = $1
         RETURNING *
            """,
            run_id,
            payload["status"],
            payload.get("summary", ""),
            dict(payload.get("raw_result") or {}),
            payload.get("raw_result_key"),
            payload.get("error_code", ""),
            payload.get("output_sha256", ""),
            dict(payload["coverage"]) if "coverage" in payload else None,
            payload.get("container_image_digest", ""),
            payload.get("dependency_snapshot_sha256", ""),
            payload.get("worker_id", ""),
            payload.get("input_sha256", ""),
        )
        return _record(row) if row else None

    async def list_review_runs(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM review_runs WHERE artifact_id = $1 ORDER BY created_at",
            artifact_id,
        )
        return [_record(row) for row in rows]

    async def fail_open_review_runs(
        self,
        artifact_id: str,
        run_type: str,
        *,
        error_code: str,
        summary: str,
    ) -> int:
        result = await self._pool().execute(
            """
            UPDATE review_runs
               SET status = 'failed',
                   error_code = $3,
                   summary = $4,
                   completed_at = now()
             WHERE artifact_id = $1
               AND type = $2
               AND status IN ('queued', 'running')
            """,
            artifact_id,
            run_type,
            error_code,
            summary,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def replace_findings(
        self,
        artifact_id: str,
        run_id: str,
        findings: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        saved: list[dict[str, Any]] = []
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                run = await connection.fetchrow(
                    "SELECT type FROM review_runs WHERE id = $1 AND artifact_id = $2",
                    run_id,
                    artifact_id,
                )
                if not run:
                    raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                default_source = _finding_source_for_run(str(run["type"]))
                for finding in findings:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO review_findings (
                            id, artifact_id, run_id, fingerprint, rule_id, file_path,
                            line_start, line_end, severity, category, message, suggestion,
                            evidence_excerpt, confidence, status, metadata, source,
                            deterministic, file_id, file_sha256,
                            affects_current_release, correlation
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                            $13, $14, $15, $16::jsonb, $17, $18, $19, $20,
                            $21, $22::jsonb
                        )
                        ON CONFLICT (artifact_id, fingerprint) DO UPDATE
                           SET run_id = EXCLUDED.run_id,
                               rule_id = EXCLUDED.rule_id,
                               file_path = EXCLUDED.file_path,
                               line_start = EXCLUDED.line_start,
                               line_end = EXCLUDED.line_end,
                               severity = EXCLUDED.severity,
                               category = EXCLUDED.category,
                               message = EXCLUDED.message,
                               suggestion = EXCLUDED.suggestion,
                               evidence_excerpt = EXCLUDED.evidence_excerpt,
                               confidence = EXCLUDED.confidence,
                               metadata = EXCLUDED.metadata,
                               source = EXCLUDED.source,
                               deterministic = EXCLUDED.deterministic,
                               file_id = EXCLUDED.file_id,
                               file_sha256 = EXCLUDED.file_sha256
                        RETURNING *
                        """,
                        finding.get("id") or new_domain_id("finding"),
                        artifact_id,
                        run_id,
                        finding["fingerprint"],
                        finding.get("rule_id", ""),
                        finding.get("file_path", ""),
                        finding.get("line_start"),
                        finding.get("line_end"),
                        finding["severity"],
                        finding.get("category", ""),
                        finding["message"],
                        finding.get("suggestion", ""),
                        finding.get("evidence_excerpt", ""),
                        finding.get("confidence"),
                        finding.get("status", "open"),
                        dict(finding.get("metadata") or {}),
                        finding.get("source", default_source),
                        bool(finding.get("deterministic", default_source != "llm")),
                        finding.get("file_id"),
                        finding.get("file_sha256"),
                        bool(finding.get("affects_current_release")),
                        dict(finding.get("correlation") or {}),
                    )
                    saved.append(_record(row))
                incoming_fingerprints = [str(item["fingerprint"]) for item in findings]
                if incoming_fingerprints:
                    await connection.execute(
                        """
                        DELETE FROM review_findings
                         WHERE run_id = $1
                           AND NOT (fingerprint = ANY($2::text[]))
                        """,
                        run_id,
                        incoming_fingerprints,
                    )
                else:
                    await connection.execute(
                        "DELETE FROM review_findings WHERE run_id = $1",
                        run_id,
                    )
        return saved

    async def list_findings(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT * FROM review_findings
             WHERE artifact_id = $1
          ORDER BY CASE severity
                       WHEN 'critical' THEN 5
                       WHEN 'high' THEN 4
                       WHEN 'medium' THEN 3
                       WHEN 'low' THEN 2
                       ELSE 1
                   END DESC,
                   file_path,
                   line_start NULLS FIRST
            """,
            artifact_id,
        )
        return [_record(row) for row in rows]

    async def transition_review_status(
        self,
        artifact_id: str,
        target: str,
        *,
        risk_level: str | None = None,
        rejection_code: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchrow(
                    "SELECT * FROM plugin_artifacts WHERE id = $1 FOR UPDATE", artifact_id
                )
                if not current:
                    return None
                validate_review_transition(str(current["review_status"]), target)
                row = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET review_status = $2,
                           risk_level = COALESCE($3, risk_level),
                           rejection_code = COALESCE($4, rejection_code),
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    artifact_id,
                    target,
                    risk_level,
                    rejection_code,
                )
        return _record(row)

    async def transition_publication_status(
        self, artifact_id: str, target: str
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchrow(
                    "SELECT * FROM plugin_artifacts WHERE id = $1 FOR UPDATE", artifact_id
                )
                if not current:
                    return None
                validate_publication_transition(str(current["publication_status"]), target)
                row = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET publication_status = $2,
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    artifact_id,
                    target,
                )
        return _record(row)

    async def enqueue_job(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        pool = self._pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                artifact_id = payload.get("artifact_id")
                policy_version_id = payload.get("policy_version_id")
                if artifact_id:
                    artifact = await connection.fetchrow(
                        "SELECT policy_version_id FROM plugin_artifacts WHERE id = $1 FOR SHARE",
                        artifact_id,
                    )
                    if not artifact:
                        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                    policy_version_id = _resolved_policy_snapshot(
                        artifact["policy_version_id"],
                        policy_version_id,
                    )
                row = await connection.fetchrow(
                    """
                    INSERT INTO artifact_jobs (
                        id, artifact_id, type, payload, max_attempts,
                        available_at, idempotency_key, policy_version_id, run_id, stage_name
                    )
                    VALUES (
                        $1, $2, $3, $4::jsonb, $5,
                        COALESCE($6::timestamptz, now()), $7, $8, $9, $10
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE
                       SET idempotency_key = EXCLUDED.idempotency_key
                    RETURNING *
                    """,
                    payload.get("id") or new_domain_id("job"),
                    artifact_id,
                    payload["type"],
                    dict(payload.get("payload") or {}),
                    int(payload.get("max_attempts") or 3),
                    payload.get("available_at"),
                    payload["idempotency_key"],
                    policy_version_id,
                    payload.get("run_id"),
                    payload.get("stage_name", ""),
                )
        saved = _record(row)
        if (
            str(saved.get("artifact_id") or "") != str(payload.get("artifact_id") or "")
            or str(saved["type"]) != str(payload["type"])
            or saved.get("policy_version_id") != policy_version_id
        ):
            raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
        return saved

    async def list_artifact_jobs(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM artifact_jobs WHERE artifact_id = $1 ORDER BY created_at",
            artifact_id,
        )
        return [_record(row) for row in rows]

    async def claim_jobs(
        self, worker_id: str, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            WITH candidates AS (
                SELECT id
                  FROM artifact_jobs
                 WHERE attempts < max_attempts
                   AND (
                       (status = 'queued' AND available_at <= now())
                       OR (status = 'running' AND lease_expires_at < now())
                   )
              ORDER BY available_at, created_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT $2
            )
            UPDATE artifact_jobs jobs
               SET status = 'running',
                   attempts = jobs.attempts + 1,
                   lease_owner = $1,
                   lease_expires_at = now() + ($3 * interval '1 second'),
                   updated_at = now()
              FROM candidates
             WHERE jobs.id = candidates.id
         RETURNING jobs.*
            """,
            worker_id,
            limit,
            lease_seconds,
        )
        return [_record(row) for row in rows]

    async def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        result = await self._pool().execute(
            """
            UPDATE artifact_jobs
               SET lease_expires_at = now() + ($3 * interval '1 second'),
                   updated_at = now()
             WHERE id = $1
               AND status = 'running'
               AND lease_owner = $2
            """,
            job_id,
            worker_id,
            lease_seconds,
        )
        return result.endswith(" 1")

    async def complete_job(self, job_id: str, worker_id: str) -> bool:
        result = await self._pool().execute(
            """
            UPDATE artifact_jobs
               SET status = 'succeeded',
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   completed_at = now(),
                   updated_at = now()
             WHERE id = $1
               AND status = 'running'
               AND lease_owner = $2
            """,
            job_id,
            worker_id,
        )
        return result.endswith(" 1")

    async def fail_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
        retry: bool,
        retry_delay_seconds: int = 0,
    ) -> bool:
        result = await self._pool().execute(
            """
            UPDATE artifact_jobs
               SET status = CASE
                       WHEN $5 AND attempts < max_attempts THEN 'queued'
                       ELSE 'failed'
                   END,
                   available_at = CASE
                       WHEN $5 THEN now() + ($6 * interval '1 second')
                       ELSE available_at
                   END,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   last_error_code = $3,
                   last_error = $4,
                   completed_at = CASE
                       WHEN $5 AND attempts < max_attempts THEN NULL
                       ELSE now()
                   END,
                   updated_at = now()
             WHERE id = $1
               AND status = 'running'
               AND lease_owner = $2
            """,
            job_id,
            worker_id,
            error_code,
            error_message,
            retry,
            retry_delay_seconds,
        )
        return result.endswith(" 1")

    async def decide_artifact(
        self,
        artifact_id: str,
        *,
        action: str,
        target_status: str,
        reason: str,
        reviewer: Mapping[str, Any] | None,
        idempotency_key: str,
        policy_version: str = "p1",
        policy_version_id: str | None = None,
        source: str | None = None,
        input_run_ids: Sequence[str] = (),
        input_fingerprints: Sequence[str] = (),
        coverage_sha256: str = "",
        metadata: Mapping[str, Any] | None = None,
        risk_level: str | None = None,
        rejection_code: str | None = None,
    ) -> dict[str, Any] | None:
        expected_target = review_target_for_decision(action)
        if expected_target is not None and expected_target.value != target_status:
            raise ValueError(ArtifactErrorCode.DECISION_TARGET_MISMATCH.value)
        decision_source = source or (
            "policy" if action in {"auto_reject", "auto_approve"} else "admin"
        )
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    "SELECT artifact_id FROM review_decisions WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing:
                    if str(existing["artifact_id"]) != artifact_id:
                        raise ValueError("idempotency_key_conflict")
                    row = await connection.fetchrow(
                        "SELECT * FROM plugin_artifacts WHERE id = $1", existing["artifact_id"]
                    )
                    return _record(row) if row else None
                current = await connection.fetchrow(
                    "SELECT * FROM plugin_artifacts WHERE id = $1 FOR UPDATE", artifact_id
                )
                if not current:
                    return None
                effective_policy_id = _resolved_policy_snapshot(
                    current["policy_version_id"],
                    policy_version_id,
                )
                effective_policy_version = policy_version
                if effective_policy_id:
                    policy_row = await connection.fetchrow(
                        "SELECT version FROM review_policies WHERE id = $1",
                        effective_policy_id,
                    )
                    if not policy_row:
                        raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                    effective_policy_version = str(policy_row["version"])
                if ReviewStatus(str(current["review_status"])) in TERMINAL_REVIEW_STATUSES:
                    raise ArtifactStateError(
                        ArtifactErrorCode.ARTIFACT_ALREADY_DECIDED,
                        str(current["review_status"]),
                        target_status,
                    )
                validate_review_transition(str(current["review_status"]), target_status)
                await connection.execute(
                    """
                    INSERT INTO review_decisions (
                        id, artifact_id, action, from_status, to_status, reason,
                        reviewer_user_id, reviewer_nickname, policy_version, idempotency_key,
                        source, policy_version_id, input_run_ids, input_fingerprints,
                        coverage_sha256, metadata
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13::text[], $14::text[], $15, $16::jsonb
                    )
                    """,
                    new_domain_id("decision"),
                    artifact_id,
                    action,
                    current["review_status"],
                    target_status,
                    reason,
                    (reviewer or {}).get("id"),
                    _reviewer_name(reviewer),
                    effective_policy_version,
                    idempotency_key,
                    decision_source,
                    effective_policy_id,
                    list(input_run_ids),
                    list(input_fingerprints),
                    coverage_sha256,
                    dict(metadata or {}),
                )
                row = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET review_status = $2,
                           risk_level = COALESCE($3, risk_level),
                           rejection_code = COALESCE($4, rejection_code),
                           download_url = CASE WHEN $2 = 'approved' THEN download_url ELSE NULL END,
                           published_key = CASE WHEN $2 = 'approved' THEN published_key ELSE NULL END,
                           reviewed_at = now(),
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    artifact_id,
                    target_status,
                    risk_level,
                    rejection_code,
                )
                await connection.execute(
                    """
                    UPDATE review_comments
                       SET locked_at = COALESCE(locked_at, now()),
                           updated_at = now()
                     WHERE artifact_id = $1
                       AND locked_at IS NULL
                    """,
                    artifact_id,
                )
        return _record(row)

    async def approve_artifact(
        self,
        artifact_id: str,
        *,
        reviewer: Mapping[str, Any],
        reason: str,
        expected_repo_version: str,
        expected_normalized_version: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    "SELECT artifact_id FROM review_decisions WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing:
                    if str(existing["artifact_id"]) != artifact_id:
                        raise ValueError("idempotency_key_conflict")
                    row = await connection.fetchrow(
                        "SELECT * FROM plugin_artifacts WHERE id = $1", existing["artifact_id"]
                    )
                    return _record(row) if row else None

                current = await connection.fetchrow(
                    """
                    SELECT a.*, p.repo_version, p.owner_user_id
                      FROM plugin_artifacts a
                      JOIN market_plugins p ON p.id = a.plugin_id
                     WHERE a.id = $1
                     FOR UPDATE OF a, p
                    """,
                    artifact_id,
                )
                if not current:
                    return None
                existing = await connection.fetchrow(
                    "SELECT artifact_id FROM review_decisions WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing:
                    if str(existing["artifact_id"]) != artifact_id:
                        raise ValueError("idempotency_key_conflict")
                    return _record(current)
                if str(current["review_status"]) != ReviewStatus.PENDING_REVIEW.value:
                    raise ArtifactStateError(
                        "artifact_not_pending_review",
                        str(current["review_status"]),
                        ReviewStatus.APPROVED.value,
                    )
                if str(current["owner_user_id"] or "") == str(reviewer.get("id") or ""):
                    raise ArtifactStateError(
                        "self_approval_forbidden",
                        str(current["review_status"]),
                        ReviewStatus.APPROVED.value,
                    )
                if str(current["repo_version"] or "") != expected_repo_version:
                    raise ValueError("repo_version_changed")
                if str(current["normalized_version"] or "") != expected_normalized_version:
                    raise ValueError("artifact_version_changed")

                effective_policy_id = str(current["policy_version_id"] or "") or None
                effective_policy_version = "p1"
                if effective_policy_id:
                    policy_row = await connection.fetchrow(
                        "SELECT version FROM review_policies WHERE id = $1",
                        effective_policy_id,
                    )
                    if not policy_row:
                        raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                    effective_policy_version = str(policy_row["version"])

                run_rows = await connection.fetch(
                    """
                    SELECT type, bool_or(status = 'succeeded') AS succeeded
                      FROM review_runs
                     WHERE artifact_id = $1
                       AND type IN ('precheck', 'static')
                       AND policy_version_id IS NOT DISTINCT FROM $2
                  GROUP BY type
                    """,
                    artifact_id,
                    effective_policy_id,
                )
                succeeded = {str(run["type"]) for run in run_rows if bool(run["succeeded"])}
                if succeeded != {"precheck", "static"}:
                    raise ValueError("required_review_runs_missing")

                await connection.execute(
                    """
                    INSERT INTO review_decisions (
                        id, artifact_id, action, from_status, to_status, reason,
                        reviewer_user_id, reviewer_nickname, policy_version, idempotency_key,
                        source, policy_version_id
                    )
                    VALUES (
                        $1, $2, 'approve', $3, 'approved', $4, $5, $6,
                        $7, $8, 'admin', $9
                    )
                    """,
                    new_domain_id("decision"),
                    artifact_id,
                    current["review_status"],
                    reason,
                    reviewer.get("id"),
                    _reviewer_name(reviewer),
                    effective_policy_version,
                    idempotency_key,
                    effective_policy_id,
                )
                approved = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET review_status = 'approved',
                           reviewed_at = now(),
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    artifact_id,
                )
                await connection.execute(
                    """
                    UPDATE review_comments
                       SET locked_at = COALESCE(locked_at, now()),
                           updated_at = now()
                     WHERE artifact_id = $1
                       AND locked_at IS NULL
                    """,
                    artifact_id,
                )
                publish_job = await connection.fetchrow(
                    """
                    INSERT INTO artifact_jobs (
                        id, artifact_id, type, payload, max_attempts, idempotency_key,
                        policy_version_id
                    )
                    VALUES ($1, $2, 'publish', $3::jsonb, 5, $4, $5)
                    ON CONFLICT (idempotency_key) DO UPDATE
                       SET idempotency_key = EXCLUDED.idempotency_key
                    RETURNING artifact_id, type, policy_version_id
                    """,
                    new_domain_id("job"),
                    artifact_id,
                    {"expected_repo_version": expected_repo_version},
                    f"publish:{artifact_id}",
                    effective_policy_id,
                )
                if (
                    str(publish_job["artifact_id"] or "") != artifact_id
                    or str(publish_job["type"] or "") != JobType.PUBLISH.value
                    or str(publish_job["policy_version_id"] or "") != effective_policy_id
                ):
                    raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
        return _record(approved)

    async def auto_approve_artifact(
        self,
        artifact_id: str,
        *,
        reason: str,
        expected_repo_version: str,
        expected_normalized_version: str,
        expected_version: str,
        idempotency_key: str,
        policy_version_id: str,
        input_run_ids: Sequence[str],
        input_fingerprints: Sequence[str],
        coverage_sha256: str,
        metadata: Mapping[str, Any],
        risk_level: str,
    ) -> dict[str, Any] | None:
        if not input_run_ids:
            raise ValueError("required_review_runs_missing")
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    "SELECT artifact_id FROM review_decisions WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing:
                    if str(existing["artifact_id"]) != artifact_id:
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    row = await connection.fetchrow(
                        "SELECT * FROM plugin_artifacts WHERE id = $1",
                        artifact_id,
                    )
                    return _record(row) if row else None

                current = await connection.fetchrow(
                    """
                    SELECT a.*, p.repo_version
                      FROM plugin_artifacts a
                      JOIN market_plugins p ON p.id = a.plugin_id
                     WHERE a.id = $1
                     FOR UPDATE OF a, p
                    """,
                    artifact_id,
                )
                if not current:
                    return None
                existing = await connection.fetchrow(
                    "SELECT artifact_id FROM review_decisions WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing:
                    if str(existing["artifact_id"]) != artifact_id:
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    return _record(current)
                if str(current["review_status"]) != ReviewStatus.SCANNING.value:
                    raise ArtifactStateError(
                        ArtifactErrorCode.ARTIFACT_ALREADY_DECIDED,
                        str(current["review_status"]),
                        ReviewStatus.APPROVED.value,
                    )
                effective_policy_id = _resolved_policy_snapshot(
                    current["policy_version_id"],
                    policy_version_id,
                )
                if not effective_policy_id:
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                policy_row = await connection.fetchrow(
                    "SELECT version FROM review_policies WHERE id = $1",
                    effective_policy_id,
                )
                if not policy_row:
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                if str(current["repo_version"] or "") != expected_repo_version:
                    raise ValueError("repo_version_changed")
                if str(current["normalized_version"] or "") != expected_normalized_version:
                    raise ValueError("artifact_version_changed")
                if str(current["version"] or "") != expected_version:
                    raise ValueError("artifact_version_changed")

                unique_run_ids = sorted(set(input_run_ids))
                run_rows = await connection.fetch(
                    """
                    SELECT id, status
                      FROM review_runs
                     WHERE id = ANY($1::text[])
                       AND artifact_id = $2
                       AND policy_version_id = $3
                    """,
                    unique_run_ids,
                    artifact_id,
                    effective_policy_id,
                )
                if {str(item["id"]) for item in run_rows} != set(unique_run_ids) or any(
                    str(item["status"]) != "succeeded" for item in run_rows
                ):
                    raise ValueError("required_review_runs_invalid")

                await connection.execute(
                    """
                    INSERT INTO review_decisions (
                        id, artifact_id, action, from_status, to_status, reason,
                        reviewer_user_id, reviewer_nickname, policy_version,
                        idempotency_key, source, policy_version_id, input_run_ids,
                        input_fingerprints, coverage_sha256, metadata
                    )
                    VALUES (
                        $1, $2, 'auto_approve', $3, 'approved', $4,
                        NULL, '自动策略', $5, $6, 'policy', $7,
                        $8::text[], $9::text[], $10, $11::jsonb
                    )
                    """,
                    new_domain_id("decision"),
                    artifact_id,
                    current["review_status"],
                    reason,
                    str(policy_row["version"]),
                    idempotency_key,
                    effective_policy_id,
                    unique_run_ids,
                    sorted(set(input_fingerprints)),
                    coverage_sha256,
                    dict(metadata),
                )
                approved = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET review_status = 'approved',
                           risk_level = $2,
                           reviewed_at = now(),
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    artifact_id,
                    risk_level,
                )
                await connection.execute(
                    """
                    UPDATE review_comments
                       SET locked_at = COALESCE(locked_at, now()),
                           updated_at = now()
                     WHERE artifact_id = $1
                       AND locked_at IS NULL
                    """,
                    artifact_id,
                )
                publish_job = await connection.fetchrow(
                    """
                    INSERT INTO artifact_jobs (
                        id, artifact_id, type, payload, max_attempts,
                        idempotency_key, policy_version_id
                    )
                    VALUES ($1, $2, 'publish', $3::jsonb, 5, $4, $5)
                    ON CONFLICT (idempotency_key) DO UPDATE
                       SET idempotency_key = EXCLUDED.idempotency_key
                    RETURNING artifact_id, type, policy_version_id
                    """,
                    new_domain_id("job"),
                    artifact_id,
                    {"expected_repo_version": expected_repo_version},
                    f"publish:{artifact_id}",
                    effective_policy_id,
                )
                if (
                    str(publish_job["artifact_id"] or "") != artifact_id
                    or str(publish_job["type"] or "") != JobType.PUBLISH.value
                    or str(publish_job["policy_version_id"] or "") != effective_policy_id
                ):
                    raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
        return _record(approved)

    async def request_revoke_artifact(
        self,
        artifact_id: str,
        *,
        reason: str,
        reviewer: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchrow(
                    """
                    SELECT a.*, p.current_artifact_id
                      FROM plugin_artifacts a
                      JOIN market_plugins p ON p.id = a.plugin_id
                     WHERE a.id = $1
                     FOR UPDATE OF a, p
                    """,
                    artifact_id,
                )
                if not current:
                    return None
                existing = await connection.fetchrow(
                    "SELECT artifact_id FROM review_decisions WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing:
                    if str(existing["artifact_id"]) != artifact_id:
                        raise ValueError("idempotency_key_conflict")
                    return _record(current)
                if str(current["current_artifact_id"] or "") != artifact_id:
                    raise ArtifactStateError(
                        "artifact_not_current_release",
                        str(current["publication_status"]),
                        PublicationStatus.REVOKING.value,
                    )
                validate_publication_transition(
                    str(current["publication_status"]), PublicationStatus.REVOKING.value
                )
                effective_policy_id = str(current["policy_version_id"] or "") or None
                effective_policy_version = "p1"
                if effective_policy_id:
                    policy_row = await connection.fetchrow(
                        "SELECT version FROM review_policies WHERE id = $1",
                        effective_policy_id,
                    )
                    if not policy_row:
                        raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                    effective_policy_version = str(policy_row["version"])
                await connection.execute(
                    """
                    INSERT INTO review_decisions (
                        id, artifact_id, action, from_status, to_status, reason,
                        reviewer_user_id, reviewer_nickname, policy_version, idempotency_key,
                        source, policy_version_id
                    )
                    VALUES (
                        $1, $2, 'revoke', $3, 'revoking', $4, $5, $6,
                        $7, $8, 'admin', $9
                    )
                    """,
                    new_domain_id("decision"),
                    artifact_id,
                    current["publication_status"],
                    reason,
                    reviewer.get("id"),
                    _reviewer_name(reviewer),
                    effective_policy_version,
                    idempotency_key,
                    effective_policy_id,
                )
                revoking = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET publication_status = 'revoking',
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    artifact_id,
                )
                await connection.execute(
                    """
                    UPDATE market_plugins
                       SET status = 'unlisted',
                           updated_at = now()
                     WHERE id = $1
                       AND current_artifact_id = $2
                    """,
                    current["plugin_id"],
                    artifact_id,
                )
                await connection.execute(
                    """
                    INSERT INTO artifact_jobs (
                        id, artifact_id, type, payload, max_attempts, idempotency_key,
                        policy_version_id
                    )
                    VALUES ($1, $2, 'revoke', $3::jsonb, 5, $4, $5)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    new_domain_id("job"),
                    artifact_id,
                    {"reason": reason},
                    idempotency_key,
                    effective_policy_id,
                )
        return _record(revoking)

    async def publish_artifact(
        self,
        artifact_id: str,
        *,
        expected_repo_version: str,
        published_key: str,
        download_url: str,
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT a.*, p.repo_version
                      FROM plugin_artifacts a
                      JOIN market_plugins p ON p.id = a.plugin_id
                     WHERE a.id = $1
                     FOR UPDATE OF a, p
                    """,
                    artifact_id,
                )
                if not row:
                    return None
                if str(row["repo_version"]) != expected_repo_version:
                    raise ValueError("repo_version_changed")
                validate_publication_transition(
                    str(row["publication_status"]), PublicationStatus.PUBLISHED.value
                )
                published = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET publication_status = 'published',
                           published_key = $2,
                           download_url = $3,
                           published_at = now(),
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    artifact_id,
                    published_key,
                    download_url,
                )
                await connection.execute(
                    """
                    UPDATE market_plugins
                       SET current_artifact_id = $2,
                           status = 'listed',
                           updated_at = now()
                     WHERE id = $1
                    """,
                    row["plugin_id"],
                    artifact_id,
                )
        return _record(published)

    async def revoke_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchrow(
                    "SELECT * FROM plugin_artifacts WHERE id = $1 FOR UPDATE", artifact_id
                )
                if not current:
                    return None
                validate_publication_transition(
                    str(current["publication_status"]), PublicationStatus.REVOKED.value
                )
                revoked = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET publication_status = 'revoked',
                           download_url = NULL,
                           revoked_at = now(),
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    artifact_id,
                )
                await connection.execute(
                    """
                    UPDATE market_plugins
                       SET current_artifact_id = NULL,
                           status = 'unlisted',
                           updated_at = now()
                     WHERE id = $1
                       AND current_artifact_id = $2
                    """,
                    current["plugin_id"],
                    artifact_id,
                )
        return _record(revoked)

    async def list_current_publications(
        self, plugin_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        if not plugin_ids:
            return {}
        rows = await self._pool().fetch(
            """
            SELECT p.id AS plugin_id,
                   p.repo_version,
                   a.*
              FROM market_plugins p
              JOIN plugin_artifacts a ON a.id = p.current_artifact_id
             WHERE p.id = ANY($1::text[])
            """,
            list(plugin_ids),
        )
        return {str(row["plugin_id"]): _record(row) for row in rows}

    async def list_review_decisions(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM review_decisions WHERE artifact_id = $1 ORDER BY created_at",
            artifact_id,
        )
        return [_record(row) for row in rows]

    async def record_decision(
        self,
        artifact_id: str,
        *,
        action: str,
        from_status: str,
        to_status: str,
        reason: str,
        reviewer: Mapping[str, Any] | None,
        idempotency_key: str,
        policy_version: str = "p1",
        policy_version_id: str | None = None,
        source: str | None = None,
        input_run_ids: Sequence[str] = (),
        input_fingerprints: Sequence[str] = (),
        coverage_sha256: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        decision_source = source or (
            "policy" if action in {"auto_reject", "auto_approve"} else "admin"
        )
        pool = self._pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    "SELECT * FROM review_decisions WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing:
                    if str(existing["artifact_id"]) != artifact_id:
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    return _record(existing)
                artifact = await connection.fetchrow(
                    "SELECT policy_version_id FROM plugin_artifacts WHERE id = $1 FOR SHARE",
                    artifact_id,
                )
                if not artifact:
                    raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                effective_policy_id = _resolved_policy_snapshot(
                    artifact["policy_version_id"],
                    policy_version_id,
                )
                effective_policy_version = policy_version
                if effective_policy_id:
                    policy_row = await connection.fetchrow(
                        "SELECT version FROM review_policies WHERE id = $1",
                        effective_policy_id,
                    )
                    if not policy_row:
                        raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                    effective_policy_version = str(policy_row["version"])
                row = await connection.fetchrow(
                    """
                    INSERT INTO review_decisions (
                        id, artifact_id, action, from_status, to_status, reason,
                        reviewer_user_id, reviewer_nickname, policy_version,
                        idempotency_key, source, policy_version_id, input_run_ids,
                        input_fingerprints, coverage_sha256, metadata
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13::text[], $14::text[], $15, $16::jsonb
                    )
                    RETURNING *
                    """,
                    new_domain_id("decision"),
                    artifact_id,
                    action,
                    from_status,
                    to_status,
                    reason,
                    (reviewer or {}).get("id"),
                    _reviewer_name(reviewer),
                    effective_policy_version,
                    idempotency_key,
                    decision_source,
                    effective_policy_id,
                    list(input_run_ids),
                    list(input_fingerprints),
                    coverage_sha256,
                    dict(metadata or {}),
                )
        return _record(row)

    async def enqueue_outbox(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        row = await self._pool().fetchrow(
            """
            INSERT INTO outbox_events (
                id, event_type, aggregate_type, aggregate_id,
                recipient_user_id, payload, dedupe_key
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            ON CONFLICT (dedupe_key) DO UPDATE
               SET dedupe_key = EXCLUDED.dedupe_key
            RETURNING *
            """,
            payload.get("id") or new_domain_id("outbox"),
            payload["event_type"],
            payload["aggregate_type"],
            payload["aggregate_id"],
            payload.get("recipient_user_id"),
            dict(payload.get("payload") or {}),
            payload["dedupe_key"],
        )
        return _record(row)

    async def list_pending_outbox(self, limit: int) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT * FROM outbox_events
             WHERE status IN ('queued', 'failed')
               AND available_at <= now()
          ORDER BY created_at
             LIMIT $1
            """,
            limit,
        )
        return [_record(row) for row in rows]

    async def claim_outbox(
        self, worker_id: str, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            WITH candidates AS (
                SELECT id
                  FROM outbox_events
                 WHERE attempts < 5
                   AND (
                       (status IN ('queued', 'failed') AND available_at <= now())
                       OR (status = 'running' AND lease_expires_at < now())
                   )
              ORDER BY available_at, created_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT $2
            )
            UPDATE outbox_events events
               SET status = 'running',
                   attempts = events.attempts + 1,
                   lease_owner = $1,
                   lease_expires_at = now() + ($3 * interval '1 second'),
                   updated_at = now()
              FROM candidates
             WHERE events.id = candidates.id
         RETURNING events.*
            """,
            worker_id,
            limit,
            lease_seconds,
        )
        return [_record(row) for row in rows]

    async def complete_outbox(self, event_id: str, worker_id: str) -> bool:
        result = await self._pool().execute(
            """
            UPDATE outbox_events
               SET status = 'delivered',
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   delivered_at = now(),
                   updated_at = now()
             WHERE id = $1
               AND status = 'running'
               AND lease_owner = $2
            """,
            event_id,
            worker_id,
        )
        return result.endswith(" 1")

    async def fail_outbox(
        self,
        event_id: str,
        worker_id: str,
        *,
        error_message: str,
        retry: bool,
        retry_delay_seconds: int = 0,
    ) -> bool:
        result = await self._pool().execute(
            """
            UPDATE outbox_events
               SET status = CASE WHEN $4 AND attempts < 5 THEN 'failed' ELSE 'cancelled' END,
                   available_at = CASE
                       WHEN $4 THEN now() + ($5 * interval '1 second')
                       ELSE available_at
                   END,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   last_error = $3,
                   updated_at = now()
             WHERE id = $1
               AND status = 'running'
               AND lease_owner = $2
            """,
            event_id,
            worker_id,
            error_message,
            retry,
            retry_delay_seconds,
        )
        return result.endswith(" 1")

    async def mark_outbox_delivered(self, event_id: str) -> bool:
        result = await self._pool().execute(
            """
            UPDATE outbox_events
               SET status = 'delivered',
                   delivered_at = now(),
                   updated_at = now()
             WHERE id = $1
               AND status <> 'delivered'
            """,
            event_id,
        )
        return result.endswith(" 1")

    def _pool(self) -> asyncpg.Pool:
        return self.store._pool()


class InMemoryArtifactRepository(InMemoryAdvancedReviewRepositoryMixin):
    def __init__(self, store: Any | None = None) -> None:
        self.store = store
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.files: dict[str, list[dict[str, Any]]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.findings: dict[str, list[dict[str, Any]]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.outbox: dict[str, dict[str, Any]] = {}
        self.policies: dict[str, dict[str, Any]] = {}
        self.policy_events: dict[str, dict[str, Any]] = {}
        self.diffs: dict[str, list[dict[str, Any]]] = {}
        self.dependency_edges: dict[str, list[dict[str, Any]]] = {}
        self.dispatches: dict[str, dict[str, Any]] = {}
        self.review_comments: dict[str, dict[str, Any]] = {}
        self.comment_events: dict[str, dict[str, Any]] = {}
        self.finding_events: dict[str, dict[str, Any]] = {}
        self.sboms: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def rebind_store(self, store: Any) -> None:
        self.store = store

    async def create_artifact(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            for artifact in self.artifacts.values():
                if (
                    artifact["plugin_id"] == payload["plugin_id"]
                    and artifact["archive_sha256"] == payload["archive_sha256"]
                ):
                    return deepcopy(artifact)
            now = _utc_now()
            artifact = {
                "id": str(payload.get("id") or new_domain_id("artifact")),
                "plugin_id": str(payload["plugin_id"]),
                "version": str(payload.get("version") or ""),
                "normalized_version": str(payload.get("normalized_version") or ""),
                "source_type": str(payload["source_type"]),
                "source_repo": str(payload["source_repo"]),
                "source_ref": str(payload.get("source_ref") or ""),
                "source_commit_sha": str(payload.get("source_commit_sha") or ""),
                "archive_sha256": str(payload["archive_sha256"]),
                "tree_sha256": str(payload.get("tree_sha256") or ""),
                "size_bytes": int(payload.get("size_bytes") or 0),
                "quarantine_key": str(payload["quarantine_key"]),
                "published_key": None,
                "path_suffix": str(payload.get("path_suffix") or secrets.token_hex(5)),
                "download_url": None,
                "review_status": ReviewStatus.QUARANTINED.value,
                "publication_status": PublicationStatus.UNPUBLISHED.value,
                "risk_level": "none",
                "base_artifact_id": payload.get("base_artifact_id"),
                "supersedes_artifact_id": payload.get("supersedes_artifact_id"),
                "policy_version_id": payload.get("policy_version_id"),
                "review_coverage": dict(payload.get("review_coverage") or {}),
                "automated_review_completed_at": None,
                "submitted_by": payload.get("submitted_by"),
                "submitted_by_snapshot": dict(payload.get("submitted_by_snapshot") or {}),
                "suggested_category": str(payload.get("suggested_category") or ""),
                "category_confidence": payload.get("category_confidence"),
                "category_reason": str(payload.get("category_reason") or ""),
                "rejection_code": "",
                "created_at": now,
                "updated_at": now,
                "reviewed_at": None,
                "published_at": None,
                "revoked_at": None,
            }
            self.artifacts[artifact["id"]] = artifact
            return deepcopy(artifact)

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        artifact = self.artifacts.get(artifact_id)
        return deepcopy(self._with_plugin(artifact)) if artifact else None

    async def get_artifact_by_sha(
        self, plugin_id: str, archive_sha256: str
    ) -> dict[str, Any] | None:
        artifact = next(
            (
                item
                for item in self.artifacts.values()
                if item["plugin_id"] == plugin_id and item["archive_sha256"] == archive_sha256
            ),
            None,
        )
        return deepcopy(self._with_plugin(artifact)) if artifact else None

    async def list_user_artifacts(
        self, user_id: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        items = [
            self._with_plugin(artifact)
            for artifact in self.artifacts.values()
            if artifact.get("submitted_by") == user_id
            or self._plugin_owner(artifact["plugin_id"]) == user_id
        ]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return deepcopy(items[offset : offset + limit])

    async def list_review_queue(
        self,
        *,
        review_status: str = "",
        risk_level: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        items = [
            self._with_plugin(artifact)
            for artifact in self.artifacts.values()
            if (not review_status or artifact["review_status"] == review_status)
            and (not risk_level or artifact["risk_level"] == risk_level)
        ]
        items.sort(key=lambda item: item["created_at"])
        return deepcopy(items[offset : offset + limit])

    async def replace_artifact_files(
        self,
        artifact_id: str,
        files: Sequence[Mapping[str, Any]],
        tree_sha256: str,
    ) -> list[dict[str, Any]]:
        saved = [
            {
                **dict(item),
                "id": str(item.get("id") or new_domain_id("file")),
                "artifact_id": artifact_id,
                "is_entrypoint": bool(item.get("is_entrypoint")),
                "is_reachable": bool(item.get("is_reachable")),
                "graph_status": str(item.get("graph_status") or "not_analyzed"),
                "scan_summary": dict(item.get("scan_summary") or {}),
                "created_at": _utc_now(),
            }
            for item in files
        ]
        self.files[artifact_id] = saved
        artifact = self.artifacts[artifact_id]
        artifact["tree_sha256"] = tree_sha256
        artifact["updated_at"] = _utc_now()
        return deepcopy(saved)

    async def update_artifact_manifest(
        self,
        artifact_id: str,
        *,
        version: str,
        normalized_version: str,
        tree_sha256: str,
    ) -> dict[str, Any] | None:
        artifact = self.artifacts.get(artifact_id)
        if not artifact or artifact["review_status"] != ReviewStatus.PRECHECKING.value:
            return None
        artifact.update(
            {
                "version": version,
                "normalized_version": normalized_version,
                "tree_sha256": tree_sha256,
                "updated_at": _utc_now(),
            }
        )
        return deepcopy(artifact)

    async def list_artifact_files(self, artifact_id: str) -> list[dict[str, Any]]:
        return deepcopy(sorted(self.files.get(artifact_id, []), key=lambda item: item["path"]))

    async def get_artifact_file(self, artifact_id: str, file_id: str) -> dict[str, Any] | None:
        item = next(
            (row for row in self.files.get(artifact_id, []) if str(row.get("id") or "") == file_id),
            None,
        )
        return deepcopy(item) if item else None

    async def get_artifact_category_state(self, artifact_id: str) -> dict[str, Any] | None:
        artifact = self.artifacts.get(artifact_id)
        plugin = self._plugin(str(artifact.get("plugin_id") or "")) if artifact else None
        if artifact is None or plugin is None:
            return None
        return deepcopy(
            {
                "category": plugin.get("category", "other"),
                "category_source": plugin.get("category_source", "user"),
                "category_explicit": bool(plugin.get("category_explicit", True)),
                "suggested_category": plugin.get("suggested_category", ""),
                "category_confidence": plugin.get("category_confidence"),
                "category_reason": plugin.get("category_reason", ""),
            }
        )

    async def apply_category_suggestion(
        self,
        artifact_id: str,
        *,
        suggested_category: str,
        confidence: float,
        reason: str,
        minimum_confidence: float,
    ) -> dict[str, Any] | None:
        async with self._lock:
            artifact = self.artifacts.get(artifact_id)
            if artifact is None or artifact["review_status"] != ReviewStatus.SCANNING.value:
                return None
            plugin = self._plugin(str(artifact["plugin_id"]))
            if plugin is None:
                raise RuntimeError("artifact_plugin_missing")
            should_apply = (
                suggested_category != "other"
                and confidence >= minimum_confidence
                and (
                    plugin.get("category_source") == "ai"
                    or (
                        plugin.get("category", "other") == "other"
                        and plugin.get("category_source", "user") == "user"
                        and plugin.get("category_explicit") is False
                    )
                )
            )
            plugin_patch: dict[str, Any] = {
                "suggested_category": suggested_category,
                "category_confidence": confidence,
                "category_reason": reason,
            }
            if should_apply:
                plugin_patch.update(
                    {
                        "category": suggested_category,
                        "category_source": "ai",
                    }
                )
            updater = getattr(self.store, "update_plugin_metadata", None)
            if updater is None:
                raise RuntimeError("artifact_plugin_store_unavailable")
            updated_plugin = updater(str(artifact["plugin_id"]), plugin_patch)
            if updated_plugin is None:
                raise RuntimeError("artifact_plugin_missing")
            artifact.update(
                {
                    "suggested_category": suggested_category,
                    "category_confidence": confidence,
                    "category_reason": reason,
                    "updated_at": _utc_now(),
                }
            )
            return deepcopy(
                {
                    "artifact": artifact,
                    "category": updated_plugin.get("category", "other"),
                    "category_source": updated_plugin.get("category_source", "user"),
                    "suggested_category": suggested_category,
                    "category_confidence": confidence,
                    "category_reason": reason,
                    "category_applied": should_apply,
                }
            )

    async def create_review_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        idempotency_key = payload.get("idempotency_key")
        if idempotency_key:
            for existing in self.runs.values():
                if existing.get("idempotency_key") == idempotency_key:
                    if existing["artifact_id"] != str(payload["artifact_id"]) or existing[
                        "type"
                    ] != str(payload["type"]):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    break
        artifact = self.artifacts.get(str(payload["artifact_id"]))
        if not artifact:
            raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
        policy_version_id = _resolved_policy_snapshot(
            artifact.get("policy_version_id"),
            payload.get("policy_version_id"),
        )
        if idempotency_key:
            existing = next(
                (
                    item
                    for item in self.runs.values()
                    if item.get("idempotency_key") == idempotency_key
                ),
                None,
            )
            if existing:
                if existing.get("policy_version_id") != policy_version_id:
                    raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                return deepcopy(existing)
        now = _utc_now()
        run = {
            "id": str(payload.get("id") or new_domain_id("run")),
            "artifact_id": str(payload["artifact_id"]),
            "type": str(payload["type"]),
            "status": str(payload.get("status") or "queued"),
            "attempt": int(payload.get("attempt") or 1),
            "ruleset_version": str(payload.get("ruleset_version") or ""),
            "model": str(payload.get("model") or ""),
            "summary": str(payload.get("summary") or ""),
            "raw_result": dict(payload.get("raw_result") or {}),
            "raw_result_key": payload.get("raw_result_key"),
            "error_code": str(payload.get("error_code") or ""),
            "tool_name": str(payload.get("tool_name") or ""),
            "tool_version": str(payload.get("tool_version") or ""),
            "policy_version_id": policy_version_id,
            "input_sha256": str(payload.get("input_sha256") or ""),
            "output_sha256": str(payload.get("output_sha256") or ""),
            "coverage": dict(payload.get("coverage") or {}),
            "prompt_version": str(payload.get("prompt_version") or ""),
            "result_schema_version": str(payload.get("result_schema_version") or ""),
            "container_image_digest": str(payload.get("container_image_digest") or ""),
            "astrbot_version": str(payload.get("astrbot_version") or ""),
            "python_version": str(payload.get("python_version") or ""),
            "platform": str(payload.get("platform") or ""),
            "dependency_snapshot_sha256": str(payload.get("dependency_snapshot_sha256") or ""),
            "worker_id": str(payload.get("worker_id") or ""),
            "idempotency_key": idempotency_key,
            "queued_at": now,
            "started_at": now if payload.get("status") == "running" else None,
            "completed_at": (
                now
                if str(payload.get("status") or "queued")
                in {"succeeded", "failed", "timed_out", "cancelled"}
                else None
            ),
            "created_at": now,
        }
        self.runs[run["id"]] = run
        return deepcopy(run)

    async def complete_review_run(
        self, run_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        if not run:
            return None
        run.update(
            {
                "status": str(payload["status"]),
                "summary": str(payload.get("summary") or ""),
                "raw_result": dict(payload.get("raw_result") or {}),
                "raw_result_key": payload.get("raw_result_key"),
                "error_code": str(payload.get("error_code") or ""),
                "output_sha256": str(
                    payload.get("output_sha256") or run.get("output_sha256") or ""
                ),
                "coverage": (
                    dict(payload["coverage"])
                    if "coverage" in payload
                    else dict(run.get("coverage") or {})
                ),
                "container_image_digest": str(
                    payload.get("container_image_digest") or run.get("container_image_digest") or ""
                ),
                "dependency_snapshot_sha256": str(
                    payload.get("dependency_snapshot_sha256")
                    or run.get("dependency_snapshot_sha256")
                    or ""
                ),
                "worker_id": str(payload.get("worker_id") or run.get("worker_id") or ""),
                "input_sha256": str(payload.get("input_sha256") or run.get("input_sha256") or ""),
                "completed_at": _utc_now(),
            }
        )
        return deepcopy(run)

    async def list_review_runs(self, artifact_id: str) -> list[dict[str, Any]]:
        values = [run for run in self.runs.values() if run["artifact_id"] == artifact_id]
        return deepcopy(sorted(values, key=lambda item: item["created_at"]))

    async def fail_open_review_runs(
        self,
        artifact_id: str,
        run_type: str,
        *,
        error_code: str,
        summary: str,
    ) -> int:
        changed = 0
        for run in self.runs.values():
            if (
                run["artifact_id"] == artifact_id
                and run["type"] == run_type
                and run["status"] in {"queued", "running"}
            ):
                run.update(
                    {
                        "status": "failed",
                        "error_code": error_code,
                        "summary": summary,
                        "completed_at": _utc_now(),
                    }
                )
                changed += 1
        return changed

    async def replace_findings(
        self,
        artifact_id: str,
        run_id: str,
        findings: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        run = self.runs.get(run_id)
        if not run or run["artifact_id"] != artifact_id:
            raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
        default_source = _finding_source_for_run(str(run["type"]))
        incoming_fingerprints = {str(item["fingerprint"]) for item in findings}
        for existing_run_id, values in list(self.findings.items()):
            if existing_run_id == run_id:
                continue
            self.findings[existing_run_id] = [
                item
                for item in values
                if not (
                    item["artifact_id"] == artifact_id
                    and item["fingerprint"] in incoming_fingerprints
                )
            ]
        existing = {
            item["fingerprint"]: item
            for values in self.findings.values()
            for item in values
            if item["artifact_id"] == artifact_id
        }
        saved: list[dict[str, Any]] = []
        for item in findings:
            finding = {
                **existing.get(str(item["fingerprint"]), {}),
                **dict(item),
                "id": existing.get(str(item["fingerprint"]), {}).get("id")
                or item.get("id")
                or new_domain_id("finding"),
                "artifact_id": artifact_id,
                "run_id": run_id,
                "source": str(item.get("source") or default_source),
                "deterministic": bool(item.get("deterministic", default_source != "llm")),
                "file_id": item.get("file_id"),
                "file_sha256": item.get("file_sha256"),
                "affects_current_release": bool(
                    existing.get(str(item["fingerprint"]), {}).get(
                        "affects_current_release",
                        item.get("affects_current_release", False),
                    )
                ),
                "correlation": dict(
                    existing.get(str(item["fingerprint"]), {}).get("correlation")
                    or item.get("correlation")
                    or {}
                ),
                "status": str(
                    existing.get(str(item["fingerprint"]), {}).get("status")
                    or item.get("status")
                    or "open"
                ),
                "status_actor_user_id": existing.get(str(item["fingerprint"]), {}).get(
                    "status_actor_user_id"
                ),
                "status_actor_nickname": str(
                    existing.get(str(item["fingerprint"]), {}).get("status_actor_nickname") or ""
                ),
                "status_updated_at": existing.get(str(item["fingerprint"]), {}).get(
                    "status_updated_at"
                ),
                "version": int(existing.get(str(item["fingerprint"]), {}).get("version") or 1),
                "created_at": _utc_now(),
            }
            saved.append(finding)
        self.findings[run_id] = saved
        return deepcopy(saved)

    async def list_findings(self, artifact_id: str) -> list[dict[str, Any]]:
        values = [
            item
            for findings in self.findings.values()
            for item in findings
            if item["artifact_id"] == artifact_id
        ]
        order = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
        values.sort(
            key=lambda item: (
                -order.get(str(item.get("severity")), 0),
                str(item.get("file_path") or ""),
                int(item.get("line_start") or 0),
            )
        )
        return deepcopy(values)

    async def transition_review_status(
        self,
        artifact_id: str,
        target: str,
        *,
        risk_level: str | None = None,
        rejection_code: str | None = None,
    ) -> dict[str, Any] | None:
        artifact = self.artifacts.get(artifact_id)
        if not artifact:
            return None
        validate_review_transition(artifact["review_status"], target)
        artifact["review_status"] = target
        if risk_level is not None:
            artifact["risk_level"] = risk_level
        if rejection_code is not None:
            artifact["rejection_code"] = rejection_code
        artifact["updated_at"] = _utc_now()
        return deepcopy(artifact)

    async def transition_publication_status(
        self, artifact_id: str, target: str
    ) -> dict[str, Any] | None:
        artifact = self.artifacts.get(artifact_id)
        if not artifact:
            return None
        validate_publication_transition(artifact["publication_status"], target)
        artifact["publication_status"] = target
        artifact["updated_at"] = _utc_now()
        return deepcopy(artifact)

    async def enqueue_job(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        artifact_id = payload.get("artifact_id")
        policy_version_id = payload.get("policy_version_id")
        if artifact_id:
            artifact = self.artifacts.get(str(artifact_id))
            if not artifact:
                raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
            policy_version_id = _resolved_policy_snapshot(
                artifact.get("policy_version_id"),
                policy_version_id,
            )
        for job in self.jobs.values():
            if job["idempotency_key"] == payload["idempotency_key"]:
                if (
                    str(job.get("artifact_id") or "") != str(artifact_id or "")
                    or job["type"] != str(payload["type"])
                    or job.get("policy_version_id") != policy_version_id
                ):
                    raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                return deepcopy(job)
        now = _utc_now()
        job = {
            "id": str(payload.get("id") or new_domain_id("job")),
            "artifact_id": artifact_id,
            "type": str(payload["type"]),
            "status": JobStatus.QUEUED.value,
            "payload": dict(payload.get("payload") or {}),
            "attempts": 0,
            "max_attempts": int(payload.get("max_attempts") or 3),
            "available_at": payload.get("available_at") or now,
            "lease_owner": None,
            "lease_expires_at": None,
            "idempotency_key": str(payload["idempotency_key"]),
            "policy_version_id": policy_version_id,
            "run_id": payload.get("run_id"),
            "stage_name": str(payload.get("stage_name") or ""),
            "last_error_code": "",
            "last_error": "",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        self.jobs[job["id"]] = job
        return deepcopy(job)

    async def list_artifact_jobs(self, artifact_id: str) -> list[dict[str, Any]]:
        values = [job for job in self.jobs.values() if job.get("artifact_id") == artifact_id]
        return deepcopy(sorted(values, key=lambda item: item["created_at"]))

    async def claim_jobs(
        self, worker_id: str, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        async with self._lock:
            now = datetime.now(UTC)
            candidates = []
            for job in self.jobs.values():
                available = _parse_time(job["available_at"]) <= now
                expired = job["status"] == "running" and (
                    not job["lease_expires_at"] or _parse_time(job["lease_expires_at"]) < now
                )
                if job["attempts"] < job["max_attempts"] and (
                    (job["status"] == "queued" and available) or expired
                ):
                    candidates.append(job)
            candidates.sort(key=lambda item: (item["available_at"], item["created_at"]))
            claimed = candidates[:limit]
            for job in claimed:
                job["status"] = JobStatus.RUNNING.value
                job["attempts"] += 1
                job["lease_owner"] = worker_id
                job["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
                job["updated_at"] = _utc_now()
            return deepcopy(claimed)

    async def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        job = self.jobs.get(job_id)
        if not job or job["status"] != "running" or job["lease_owner"] != worker_id:
            return False
        job["lease_expires_at"] = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        job["updated_at"] = _utc_now()
        return True

    async def complete_job(self, job_id: str, worker_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job["status"] != "running" or job["lease_owner"] != worker_id:
            return False
        job.update(
            {
                "status": JobStatus.SUCCEEDED.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "completed_at": _utc_now(),
                "updated_at": _utc_now(),
            }
        )
        return True

    async def fail_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
        retry: bool,
        retry_delay_seconds: int = 0,
    ) -> bool:
        job = self.jobs.get(job_id)
        if not job or job["status"] != "running" or job["lease_owner"] != worker_id:
            return False
        should_retry = retry and job["attempts"] < job["max_attempts"]
        job.update(
            {
                "status": JobStatus.QUEUED.value if should_retry else JobStatus.FAILED.value,
                "available_at": (
                    datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
                ).isoformat(),
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error_code": error_code,
                "last_error": error_message,
                "completed_at": None if should_retry else _utc_now(),
                "updated_at": _utc_now(),
            }
        )
        return True

    async def decide_artifact(
        self,
        artifact_id: str,
        *,
        action: str,
        target_status: str,
        reason: str,
        reviewer: Mapping[str, Any] | None,
        idempotency_key: str,
        policy_version: str = "p1",
        policy_version_id: str | None = None,
        source: str | None = None,
        input_run_ids: Sequence[str] = (),
        input_fingerprints: Sequence[str] = (),
        coverage_sha256: str = "",
        metadata: Mapping[str, Any] | None = None,
        risk_level: str | None = None,
        rejection_code: str | None = None,
    ) -> dict[str, Any] | None:
        expected_target = review_target_for_decision(action)
        if expected_target is not None and expected_target.value != target_status:
            raise ValueError(ArtifactErrorCode.DECISION_TARGET_MISMATCH.value)
        decision_source = source or (
            "policy" if action in {"auto_reject", "auto_approve"} else "admin"
        )
        async with self._lock:
            for decision in self.decisions.values():
                if decision["idempotency_key"] == idempotency_key:
                    if decision["artifact_id"] != artifact_id:
                        raise ValueError("idempotency_key_conflict")
                    artifact = self.artifacts.get(decision["artifact_id"])
                    return deepcopy(artifact) if artifact else None
            artifact = self.artifacts.get(artifact_id)
            if not artifact:
                return None
            effective_policy_id = _resolved_policy_snapshot(
                artifact.get("policy_version_id"),
                policy_version_id,
            )
            effective_policy_version = policy_version
            if effective_policy_id:
                policy = self.policies.get(effective_policy_id)
                if not policy:
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                effective_policy_version = str(policy["version"])
            if ReviewStatus(artifact["review_status"]) in TERMINAL_REVIEW_STATUSES:
                raise ArtifactStateError(
                    ArtifactErrorCode.ARTIFACT_ALREADY_DECIDED,
                    artifact["review_status"],
                    target_status,
                )
            validate_review_transition(artifact["review_status"], target_status)
            decision = {
                "id": new_domain_id("decision"),
                "artifact_id": artifact_id,
                "action": action,
                "from_status": artifact["review_status"],
                "to_status": target_status,
                "reason": reason,
                "reviewer_user_id": (reviewer or {}).get("id"),
                "reviewer_nickname": _reviewer_name(reviewer),
                "policy_version": effective_policy_version,
                "policy_version_id": effective_policy_id,
                "source": decision_source,
                "input_run_ids": list(input_run_ids),
                "input_fingerprints": list(input_fingerprints),
                "coverage_sha256": coverage_sha256,
                "metadata": dict(metadata or {}),
                "idempotency_key": idempotency_key,
                "created_at": _utc_now(),
            }
            self.decisions[decision["id"]] = decision
            artifact["review_status"] = target_status
            if target_status != ReviewStatus.APPROVED.value:
                artifact["download_url"] = None
                artifact["published_key"] = None
            if risk_level is not None:
                artifact["risk_level"] = risk_level
            if rejection_code is not None:
                artifact["rejection_code"] = rejection_code
            artifact["reviewed_at"] = _utc_now()
            artifact["updated_at"] = _utc_now()
            now = _utc_now()
            for thread in self.review_comments.values():
                if thread["artifact_id"] == artifact_id and not thread.get("locked_at"):
                    thread["locked_at"] = now
                    thread["updated_at"] = now
            return deepcopy(artifact)

    async def approve_artifact(
        self,
        artifact_id: str,
        *,
        reviewer: Mapping[str, Any],
        reason: str,
        expected_repo_version: str,
        expected_normalized_version: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            for decision in self.decisions.values():
                if decision["idempotency_key"] == idempotency_key:
                    if decision["artifact_id"] != artifact_id:
                        raise ValueError("idempotency_key_conflict")
                    artifact = self.artifacts.get(decision["artifact_id"])
                    return deepcopy(artifact) if artifact else None
            artifact = self.artifacts.get(artifact_id)
            if not artifact:
                return None
            plugin = self._plugin(artifact["plugin_id"]) or {}
            if artifact["review_status"] != ReviewStatus.PENDING_REVIEW.value:
                raise ArtifactStateError(
                    "artifact_not_pending_review",
                    artifact["review_status"],
                    ReviewStatus.APPROVED.value,
                )
            if str(plugin.get("owner_user_id") or "") == str(reviewer.get("id") or ""):
                raise ArtifactStateError(
                    "self_approval_forbidden",
                    artifact["review_status"],
                    ReviewStatus.APPROVED.value,
                )
            if str(plugin.get("repo_version") or "") != expected_repo_version:
                raise ValueError("repo_version_changed")
            if artifact["normalized_version"] != expected_normalized_version:
                raise ValueError("artifact_version_changed")
            effective_policy_id = str(artifact.get("policy_version_id") or "") or None
            effective_policy_version = "p1"
            if effective_policy_id:
                policy = self.policies.get(effective_policy_id)
                if not policy:
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                effective_policy_version = str(policy["version"])
            succeeded = {
                run["type"]
                for run in self.runs.values()
                if run["artifact_id"] == artifact_id
                and run["status"] == "succeeded"
                and run.get("policy_version_id") == effective_policy_id
            }
            if not {"precheck", "static"}.issubset(succeeded):
                raise ValueError("required_review_runs_missing")
            decision = {
                "id": new_domain_id("decision"),
                "artifact_id": artifact_id,
                "action": "approve",
                "from_status": artifact["review_status"],
                "to_status": ReviewStatus.APPROVED.value,
                "reason": reason,
                "reviewer_user_id": reviewer.get("id"),
                "reviewer_nickname": _reviewer_name(reviewer),
                "policy_version": effective_policy_version,
                "policy_version_id": effective_policy_id,
                "source": "admin",
                "input_run_ids": [],
                "input_fingerprints": [],
                "coverage_sha256": "",
                "metadata": {},
                "idempotency_key": idempotency_key,
                "created_at": _utc_now(),
            }
            self.decisions[decision["id"]] = decision
            artifact["review_status"] = ReviewStatus.APPROVED.value
            artifact["reviewed_at"] = _utc_now()
            artifact["updated_at"] = _utc_now()
            now = _utc_now()
            for thread in self.review_comments.values():
                if thread["artifact_id"] == artifact_id and not thread.get("locked_at"):
                    thread["locked_at"] = now
                    thread["updated_at"] = now
            job = {
                "id": new_domain_id("job"),
                "artifact_id": artifact_id,
                "type": "publish",
                "status": JobStatus.QUEUED.value,
                "payload": {"expected_repo_version": expected_repo_version},
                "attempts": 0,
                "max_attempts": 5,
                "available_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "idempotency_key": f"publish:{artifact_id}",
                "policy_version_id": artifact.get("policy_version_id"),
                "run_id": None,
                "stage_name": "publish",
                "last_error_code": "",
                "last_error": "",
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            if not any(
                item["idempotency_key"] == job["idempotency_key"] for item in self.jobs.values()
            ):
                self.jobs[job["id"]] = job
            return deepcopy(artifact)

    async def auto_approve_artifact(
        self,
        artifact_id: str,
        *,
        reason: str,
        expected_repo_version: str,
        expected_normalized_version: str,
        expected_version: str,
        idempotency_key: str,
        policy_version_id: str,
        input_run_ids: Sequence[str],
        input_fingerprints: Sequence[str],
        coverage_sha256: str,
        metadata: Mapping[str, Any],
        risk_level: str,
    ) -> dict[str, Any] | None:
        if not input_run_ids:
            raise ValueError("required_review_runs_missing")
        async with self._lock:
            for decision in self.decisions.values():
                if decision["idempotency_key"] == idempotency_key:
                    if decision["artifact_id"] != artifact_id:
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    artifact = self.artifacts.get(artifact_id)
                    return deepcopy(artifact) if artifact else None
            artifact = self.artifacts.get(artifact_id)
            if not artifact:
                return None
            if artifact["review_status"] != ReviewStatus.SCANNING.value:
                raise ArtifactStateError(
                    ArtifactErrorCode.ARTIFACT_ALREADY_DECIDED,
                    artifact["review_status"],
                    ReviewStatus.APPROVED.value,
                )
            effective_policy_id = _resolved_policy_snapshot(
                artifact.get("policy_version_id"),
                policy_version_id,
            )
            policy = self.policies.get(str(effective_policy_id or ""))
            if not effective_policy_id or not policy:
                raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
            plugin = self._plugin(artifact["plugin_id"]) or {}
            if str(plugin.get("repo_version") or "") != expected_repo_version:
                raise ValueError("repo_version_changed")
            if artifact["normalized_version"] != expected_normalized_version:
                raise ValueError("artifact_version_changed")
            if artifact["version"] != expected_version:
                raise ValueError("artifact_version_changed")
            unique_run_ids = sorted(set(input_run_ids))
            matching_runs = [self.runs.get(run_id) for run_id in unique_run_ids]
            if any(
                run is None
                or run["artifact_id"] != artifact_id
                or run.get("policy_version_id") != effective_policy_id
                or run["status"] != "succeeded"
                for run in matching_runs
            ):
                raise ValueError("required_review_runs_invalid")

            publish_key = f"publish:{artifact_id}"
            existing_publish = next(
                (job for job in self.jobs.values() if job["idempotency_key"] == publish_key),
                None,
            )
            if existing_publish and (
                existing_publish.get("artifact_id") != artifact_id
                or existing_publish.get("type") != JobType.PUBLISH.value
                or existing_publish.get("policy_version_id") != effective_policy_id
            ):
                raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)

            now = _utc_now()
            decision = {
                "id": new_domain_id("decision"),
                "artifact_id": artifact_id,
                "action": "auto_approve",
                "from_status": artifact["review_status"],
                "to_status": ReviewStatus.APPROVED.value,
                "reason": reason,
                "reviewer_user_id": None,
                "reviewer_nickname": "自动策略",
                "policy_version": str(policy["version"]),
                "policy_version_id": effective_policy_id,
                "source": "policy",
                "input_run_ids": unique_run_ids,
                "input_fingerprints": sorted(set(input_fingerprints)),
                "coverage_sha256": coverage_sha256,
                "metadata": dict(metadata),
                "idempotency_key": idempotency_key,
                "created_at": now,
            }
            publish_job = {
                "id": new_domain_id("job"),
                "artifact_id": artifact_id,
                "type": JobType.PUBLISH.value,
                "status": JobStatus.QUEUED.value,
                "payload": {"expected_repo_version": expected_repo_version},
                "attempts": 0,
                "max_attempts": 5,
                "available_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "idempotency_key": publish_key,
                "policy_version_id": effective_policy_id,
                "run_id": None,
                "stage_name": "",
                "last_error_code": "",
                "last_error": "",
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            self.decisions[decision["id"]] = decision
            artifact["review_status"] = ReviewStatus.APPROVED.value
            artifact["risk_level"] = risk_level
            artifact["reviewed_at"] = now
            artifact["updated_at"] = now
            for thread in self.review_comments.values():
                if thread["artifact_id"] == artifact_id and not thread.get("locked_at"):
                    thread["locked_at"] = now
                    thread["updated_at"] = now
            if existing_publish is None:
                self.jobs[publish_job["id"]] = publish_job
            return deepcopy(artifact)

    async def request_revoke_artifact(
        self,
        artifact_id: str,
        *,
        reason: str,
        reviewer: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            for decision in self.decisions.values():
                if decision["idempotency_key"] == idempotency_key:
                    if decision["artifact_id"] != artifact_id:
                        raise ValueError("idempotency_key_conflict")
                    artifact = self.artifacts.get(artifact_id)
                    return deepcopy(artifact) if artifact else None
            artifact = self.artifacts.get(artifact_id)
            if not artifact:
                return None
            plugin = self._plugin(artifact["plugin_id"])
            if not plugin or plugin.get("current_artifact_id") != artifact_id:
                raise ArtifactStateError(
                    "artifact_not_current_release",
                    artifact["publication_status"],
                    PublicationStatus.REVOKING.value,
                )
            validate_publication_transition(
                artifact["publication_status"], PublicationStatus.REVOKING.value
            )
            now = _utc_now()
            effective_policy_id = str(artifact.get("policy_version_id") or "") or None
            effective_policy_version = "p1"
            if effective_policy_id:
                policy = self.policies.get(effective_policy_id)
                if not policy:
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                effective_policy_version = str(policy["version"])
            decision = {
                "id": new_domain_id("decision"),
                "artifact_id": artifact_id,
                "action": "revoke",
                "from_status": artifact["publication_status"],
                "to_status": PublicationStatus.REVOKING.value,
                "reason": reason,
                "reviewer_user_id": reviewer.get("id"),
                "reviewer_nickname": _reviewer_name(reviewer),
                "policy_version": effective_policy_version,
                "policy_version_id": effective_policy_id,
                "source": "admin",
                "input_run_ids": [],
                "input_fingerprints": [],
                "coverage_sha256": "",
                "metadata": {},
                "idempotency_key": idempotency_key,
                "created_at": now,
            }
            self.decisions[decision["id"]] = decision
            artifact["publication_status"] = PublicationStatus.REVOKING.value
            artifact["updated_at"] = now
            plugin["status"] = "unlisted"
            job = {
                "id": new_domain_id("job"),
                "artifact_id": artifact_id,
                "type": "revoke",
                "status": JobStatus.QUEUED.value,
                "payload": {"reason": reason},
                "attempts": 0,
                "max_attempts": 5,
                "available_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "idempotency_key": idempotency_key,
                "policy_version_id": artifact.get("policy_version_id"),
                "run_id": None,
                "stage_name": "revoke",
                "last_error_code": "",
                "last_error": "",
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            self.jobs[job["id"]] = job
            return deepcopy(artifact)

    async def publish_artifact(
        self,
        artifact_id: str,
        *,
        expected_repo_version: str,
        published_key: str,
        download_url: str,
    ) -> dict[str, Any] | None:
        artifact = self.artifacts.get(artifact_id)
        if not artifact:
            return None
        plugin = self._plugin(artifact["plugin_id"])
        if not plugin or str(plugin.get("repo_version") or "") != expected_repo_version:
            raise ValueError("repo_version_changed")
        for other in self.artifacts.values():
            if (
                other["id"] != artifact_id
                and other["plugin_id"] == artifact["plugin_id"]
                and other["normalized_version"] == artifact["normalized_version"]
                and other["publication_status"] == PublicationStatus.PUBLISHED.value
            ):
                raise ValueError("published_version_conflict")
        validate_publication_transition(
            artifact["publication_status"], PublicationStatus.PUBLISHED.value
        )
        artifact.update(
            {
                "publication_status": PublicationStatus.PUBLISHED.value,
                "published_key": published_key,
                "download_url": download_url,
                "published_at": _utc_now(),
                "updated_at": _utc_now(),
            }
        )
        plugin["current_artifact_id"] = artifact_id
        plugin["status"] = "listed"
        return deepcopy(artifact)

    async def revoke_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        artifact = self.artifacts.get(artifact_id)
        if not artifact:
            return None
        validate_publication_transition(
            artifact["publication_status"], PublicationStatus.REVOKED.value
        )
        artifact.update(
            {
                "publication_status": PublicationStatus.REVOKED.value,
                "download_url": None,
                "revoked_at": _utc_now(),
                "updated_at": _utc_now(),
            }
        )
        plugin = self._plugin(artifact["plugin_id"])
        if plugin and plugin.get("current_artifact_id") == artifact_id:
            plugin["current_artifact_id"] = None
            plugin["status"] = "unlisted"
        return deepcopy(artifact)

    async def list_current_publications(
        self, plugin_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        publications: dict[str, dict[str, Any]] = {}
        for plugin_id in plugin_ids:
            plugin = self._plugin(plugin_id)
            artifact = self.artifacts.get(str((plugin or {}).get("current_artifact_id") or ""))
            if artifact:
                publications[plugin_id] = deepcopy(
                    {
                        **artifact,
                        "plugin_id": plugin_id,
                        "repo_version": plugin.get("repo_version", ""),
                    }
                )
        return publications

    async def list_review_decisions(self, artifact_id: str) -> list[dict[str, Any]]:
        values = [
            decision
            for decision in self.decisions.values()
            if decision["artifact_id"] == artifact_id
        ]
        return deepcopy(sorted(values, key=lambda item: item["created_at"]))

    async def record_decision(
        self,
        artifact_id: str,
        *,
        action: str,
        from_status: str,
        to_status: str,
        reason: str,
        reviewer: Mapping[str, Any] | None,
        idempotency_key: str,
        policy_version: str = "p1",
        policy_version_id: str | None = None,
        source: str | None = None,
        input_run_ids: Sequence[str] = (),
        input_fingerprints: Sequence[str] = (),
        coverage_sha256: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        for decision in self.decisions.values():
            if decision["idempotency_key"] == idempotency_key:
                if decision["artifact_id"] != artifact_id:
                    raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                return deepcopy(decision)
        artifact = self.artifacts.get(artifact_id)
        if not artifact:
            raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
        effective_policy_id = _resolved_policy_snapshot(
            artifact.get("policy_version_id"),
            policy_version_id,
        )
        effective_policy_version = policy_version
        if effective_policy_id:
            policy = self.policies.get(effective_policy_id)
            if not policy:
                raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
            effective_policy_version = str(policy["version"])
        decision_source = source or (
            "policy" if action in {"auto_reject", "auto_approve"} else "admin"
        )
        decision = {
            "id": new_domain_id("decision"),
            "artifact_id": artifact_id,
            "action": action,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "reviewer_user_id": (reviewer or {}).get("id"),
            "reviewer_nickname": _reviewer_name(reviewer),
            "policy_version": effective_policy_version,
            "policy_version_id": effective_policy_id,
            "source": decision_source,
            "input_run_ids": list(input_run_ids),
            "input_fingerprints": list(input_fingerprints),
            "coverage_sha256": coverage_sha256,
            "metadata": dict(metadata or {}),
            "idempotency_key": idempotency_key,
            "created_at": _utc_now(),
        }
        self.decisions[decision["id"]] = decision
        return deepcopy(decision)

    async def enqueue_outbox(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        for event in self.outbox.values():
            if event["dedupe_key"] == payload["dedupe_key"]:
                return deepcopy(event)
        now = _utc_now()
        event = {
            "id": str(payload.get("id") or new_domain_id("outbox")),
            "event_type": str(payload["event_type"]),
            "aggregate_type": str(payload["aggregate_type"]),
            "aggregate_id": str(payload["aggregate_id"]),
            "recipient_user_id": payload.get("recipient_user_id"),
            "payload": dict(payload.get("payload") or {}),
            "dedupe_key": str(payload["dedupe_key"]),
            "status": "queued",
            "attempts": 0,
            "available_at": now,
            "lease_owner": None,
            "lease_expires_at": None,
            "delivered_at": None,
            "last_error": "",
            "created_at": now,
            "updated_at": now,
        }
        self.outbox[event["id"]] = event
        return deepcopy(event)

    async def list_pending_outbox(self, limit: int) -> list[dict[str, Any]]:
        items = [event for event in self.outbox.values() if event["status"] in {"queued", "failed"}]
        items.sort(key=lambda item: item["created_at"])
        return deepcopy(items[:limit])

    async def claim_outbox(
        self, worker_id: str, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        async with self._lock:
            now = datetime.now(UTC)
            candidates = []
            for event in self.outbox.values():
                available = _parse_time(event["available_at"]) <= now
                expired = event["status"] == "running" and (
                    not event.get("lease_expires_at")
                    or _parse_time(event["lease_expires_at"]) < now
                )
                if event["attempts"] < 5 and (
                    (event["status"] in {"queued", "failed"} and available) or expired
                ):
                    candidates.append(event)
            candidates.sort(key=lambda item: (item["available_at"], item["created_at"]))
            claimed = candidates[:limit]
            for event in claimed:
                event["status"] = "running"
                event["attempts"] += 1
                event["lease_owner"] = worker_id
                event["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
                event["updated_at"] = _utc_now()
            return deepcopy(claimed)

    async def complete_outbox(self, event_id: str, worker_id: str) -> bool:
        event = self.outbox.get(event_id)
        if not event or event["status"] != "running" or event.get("lease_owner") != worker_id:
            return False
        event.update(
            {
                "status": "delivered",
                "lease_owner": None,
                "lease_expires_at": None,
                "delivered_at": _utc_now(),
                "updated_at": _utc_now(),
            }
        )
        return True

    async def fail_outbox(
        self,
        event_id: str,
        worker_id: str,
        *,
        error_message: str,
        retry: bool,
        retry_delay_seconds: int = 0,
    ) -> bool:
        event = self.outbox.get(event_id)
        if not event or event["status"] != "running" or event.get("lease_owner") != worker_id:
            return False
        should_retry = retry and event["attempts"] < 5
        event.update(
            {
                "status": "failed" if should_retry else "cancelled",
                "available_at": (
                    datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
                ).isoformat(),
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": error_message,
                "updated_at": _utc_now(),
            }
        )
        return True

    async def mark_outbox_delivered(self, event_id: str) -> bool:
        event = self.outbox.get(event_id)
        if not event or event["status"] == "delivered":
            return False
        event["status"] = "delivered"
        event["delivered_at"] = _utc_now()
        event["updated_at"] = _utc_now()
        return True

    def _with_plugin(self, artifact: Mapping[str, Any]) -> dict[str, Any]:
        plugin = self._plugin(str(artifact["plugin_id"])) or {}
        current = self.artifacts.get(str(plugin.get("current_artifact_id") or "")) or {}
        return {
            **artifact,
            "plugin_name": plugin.get("name", artifact["plugin_id"]),
            "plugin_repo": plugin.get("repo", artifact.get("source_repo", "")),
            "repo_version": plugin.get("repo_version", ""),
            "published_version": current.get("version", ""),
            "owner_user_id": plugin.get("owner_user_id"),
            "owner_github_login": plugin.get("owner_github_login", ""),
        }

    def _plugin(self, plugin_id: str) -> dict[str, Any] | None:
        if not self.store:
            return None
        finder = getattr(self.store, "get_plugin", None)
        return finder(plugin_id) if finder else None

    def _plugin_owner(self, plugin_id: str) -> str | None:
        return (self._plugin(plugin_id) or {}).get("owner_user_id")


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _serialize(value) for key, value in dict(row).items()}


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _reviewer_name(reviewer: Mapping[str, Any] | None) -> str:
    value = reviewer or {}
    return str(
        value.get("github_name")
        or value.get("github_login")
        or value.get("internal_username")
        or ""
    )


def _resolved_policy_snapshot(current: Any, requested: Any) -> str | None:
    current_id = str(current or "") or None
    requested_id = str(requested or "") or None
    if requested_id is not None and requested_id != current_id:
        raise ValueError(ArtifactErrorCode.ARTIFACT_POLICY_SNAPSHOT_CONFLICT.value)
    return current_id


def _finding_source_for_run(run_type: str) -> str:
    if run_type == "precheck":
        return "precheck"
    if run_type == "runtime":
        return "runtime"
    if run_type.startswith("llm_"):
        return "llm"
    if run_type in {"clamav", "yara", "dependency"}:
        return run_type
    return "static"
