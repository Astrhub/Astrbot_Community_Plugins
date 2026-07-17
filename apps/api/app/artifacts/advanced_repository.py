from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from .models import (
    ArtifactErrorCode,
    DecisionAction,
    FindingStatus,
    ReviewCommentEventType,
    ReviewPolicyEventAction,
    ReviewPolicyStatus,
    ReviewStatus,
    RuntimeDispatchStatus,
    TERMINAL_REVIEW_STATUSES,
    new_domain_id,
)
from .observability import (
    METRIC_JOB_STATUSES,
    METRIC_RUN_STATUSES,
    normalize_worker_heartbeat,
    percentile_cont,
    timestamp,
)


class PgAdvancedReviewRepositoryMixin:
    store: Any

    async def create_review_policy(
        self,
        payload: Mapping[str, Any],
        event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    "SELECT * FROM review_policies WHERE version = $1 FOR UPDATE",
                    payload["version"],
                )
                if existing:
                    if (
                        str(existing["policy_sha256"]) != str(payload["policy_sha256"])
                        or str(existing["schema_version"]) != str(payload["schema_version"])
                        or str(existing["base_policy_id"] or "")
                        != str(payload.get("base_policy_id") or "")
                    ):
                        raise ValueError(ArtifactErrorCode.REVIEW_POLICY_VERSION_CONFLICT.value)
                    row = existing
                else:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO review_policies (
                            id, version, schema_version, status, is_default, policy,
                            policy_sha256, base_policy_id, created_by_user_id,
                            created_by_nickname, validation_summary, validated_at,
                            activated_at, retired_at
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10,
                            $11::jsonb, $12::timestamptz, $13::timestamptz, $14::timestamptz
                        )
                        RETURNING *
                        """,
                        payload.get("id") or new_domain_id("policy"),
                        payload["version"],
                        payload["schema_version"],
                        payload.get("status", ReviewPolicyStatus.DRAFT.value),
                        bool(payload.get("is_default", True)),
                        dict(payload.get("policy") or {}),
                        payload["policy_sha256"],
                        payload.get("base_policy_id"),
                        payload.get("created_by_user_id"),
                        payload.get("created_by_nickname", ""),
                        dict(payload.get("validation_summary") or {}),
                        _as_datetime(payload.get("validated_at")),
                        _as_datetime(payload.get("activated_at")),
                        _as_datetime(payload.get("retired_at")),
                    )
                if event:
                    await self._insert_review_policy_event(connection, str(row["id"]), event)
        return _record(row)

    async def get_review_policy(self, policy_id: str) -> dict[str, Any] | None:
        row = await self._advanced_pool().fetchrow(
            "SELECT * FROM review_policies WHERE id = $1",
            policy_id,
        )
        return _record(row) if row else None

    async def get_active_review_policy(self) -> dict[str, Any] | None:
        row = await self._advanced_pool().fetchrow(
            """
            SELECT * FROM review_policies
             WHERE status = 'active'
               AND is_default
          ORDER BY activated_at DESC, created_at DESC
             LIMIT 1
            """
        )
        return _record(row) if row else None

    async def list_review_policies(self, limit: int, offset: int) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            SELECT * FROM review_policies
          ORDER BY created_at DESC, id DESC
             LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        return [_record(row) for row in rows]

    async def upsert_review_worker_heartbeat(
        self,
        *,
        worker_kind: str,
        worker_id: str,
        components: Mapping[str, Any],
        ttl_seconds: int,
        capacity: int,
        active_count: int,
    ) -> dict[str, Any]:
        heartbeat = normalize_worker_heartbeat(
            worker_kind=worker_kind,
            worker_id=worker_id,
            components=components,
            ttl_seconds=ttl_seconds,
            capacity=capacity,
            active_count=active_count,
        )
        row = await self._advanced_pool().fetchrow(
            """
            WITH expired AS (
                DELETE FROM review_worker_heartbeats
                 WHERE expires_at < now() - interval '7 days'
            )
            INSERT INTO review_worker_heartbeats (
                worker_kind, worker_id, components, capacity, active_count,
                observed_at, expires_at, updated_at
            )
            VALUES ($1, $2, $3::jsonb, $4, $5, now(), now() + ($6 * interval '1 second'), now())
            ON CONFLICT (worker_kind, worker_id) DO UPDATE
               SET components = EXCLUDED.components,
                   capacity = EXCLUDED.capacity,
                   active_count = EXCLUDED.active_count,
                   observed_at = now(),
                   expires_at = now() + ($6 * interval '1 second'),
                   updated_at = now()
            RETURNING *
            """,
            heartbeat["worker_kind"],
            heartbeat["worker_id"],
            heartbeat["components"],
            heartbeat["capacity"],
            heartbeat["active_count"],
            heartbeat["ttl_seconds"],
        )
        return _record(row)

    async def list_review_worker_heartbeats(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            SELECT *, expires_at > now() AS live
              FROM review_worker_heartbeats
          ORDER BY worker_kind ASC, live DESC, observed_at DESC, worker_id ASC
             LIMIT $1
            """,
            max(1, min(int(limit), 100)),
        )
        return [_record(row) for row in rows]

    async def list_latest_review_tool_runs(self) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            SELECT DISTINCT ON (type)
                   type, status, tool_name, tool_version, ruleset_version,
                   coverage, error_code, completed_at, created_at
              FROM review_runs
             WHERE type IN ('runtime', 'llm_package', 'llm_file', 'llm_summary',
                            'clamav', 'yara', 'dependency')
          ORDER BY type, COALESCE(completed_at, created_at) DESC, id DESC
            """
        )
        return [_record(row) for row in rows]

    async def get_review_observability_snapshot(self, since: datetime) -> dict[str, Any]:
        pool = self._advanced_pool()
        queue_rows = await pool.fetch(
            """
            SELECT type, status, count(*)::bigint AS count
              FROM artifact_jobs
             WHERE status IN ('queued', 'running')
          GROUP BY type, status
          ORDER BY type, status
            """
        )
        stage_rows = await pool.fetch(
            """
            SELECT type,
                   count(*)::bigint AS sample_count,
                   count(*) FILTER (WHERE status = 'failed')::bigint AS failure_count,
                   count(*) FILTER (WHERE status = 'timed_out')::bigint AS timeout_count,
                   COALESCE(
                       avg(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000)
                           FILTER (WHERE started_at IS NOT NULL AND completed_at IS NOT NULL),
                       0
                   ) AS average_duration_ms,
                   COALESCE(
                       percentile_cont(0.95) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000
                       ) FILTER (WHERE started_at IS NOT NULL AND completed_at IS NOT NULL),
                       0
                   ) AS p95_duration_ms
              FROM review_runs
             WHERE queued_at >= $1
          GROUP BY type
          ORDER BY type
            """,
            since,
        )
        manual_row = await pool.fetchrow(
            """
            SELECT count(*)::bigint AS waiting_count,
                   COALESCE(avg(EXTRACT(EPOCH FROM (
                       now() - COALESCE(automated_review_completed_at, updated_at, created_at)
                   ))), 0) AS average_wait_seconds,
                   COALESCE(max(EXTRACT(EPOCH FROM (
                       now() - COALESCE(automated_review_completed_at, updated_at, created_at)
                   ))), 0) AS max_wait_seconds
              FROM plugin_artifacts
             WHERE review_status = 'pending_review'
            """
        )
        routing_rows = await pool.fetch(
            """
            SELECT action, source, count(*)::bigint AS count
              FROM review_decisions
             WHERE created_at >= $1
          GROUP BY action, source
          ORDER BY action, source
            """,
            since,
        )
        revoke_rows = await pool.fetch(
            """
            SELECT status, count(*)::bigint AS count
              FROM artifact_jobs
             WHERE type = 'revoke'
               AND created_at >= $1
          GROUP BY status
          ORDER BY status
            """,
            since,
        )
        latest_rows = await pool.fetch(
            """
            SELECT DISTINCT ON (type)
                   type, status, tool_name, tool_version, ruleset_version,
                   coverage, error_code, completed_at, created_at
              FROM review_runs
             WHERE type IN ('runtime', 'llm_package', 'llm_file', 'llm_summary',
                            'clamav', 'yara', 'dependency')
          ORDER BY type, COALESCE(completed_at, created_at) DESC, id DESC
            """
        )
        return {
            "window_started_at": since,
            "queue": [_record(row) for row in queue_rows],
            "stages": [_record(row) for row in stage_rows],
            "manual_wait": _record(manual_row),
            "routing": [_record(row) for row in routing_rows],
            "revoke": [_record(row) for row in revoke_rows],
            "latest_tool_runs": [_record(row) for row in latest_rows],
        }

    async def append_review_policy_event(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await self._insert_review_policy_event(
                    connection,
                    str(payload["policy_id"]),
                    payload,
                )
        return _record(row)

    async def list_review_policy_events(self, policy_id: str) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            SELECT * FROM review_policy_events
             WHERE policy_id = $1
          ORDER BY created_at ASC, id ASC
            """,
            policy_id,
        )
        return [_record(row) for row in rows]

    async def transition_review_policy(
        self,
        policy_id: str,
        *,
        action: str,
        expected_policy_sha256: str,
        expected_active_policy_id: str | None,
        validation_summary: Mapping[str, Any] | None,
        event: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        normalized_action = ReviewPolicyEventAction(action)
        if str(event.get("action") or "") != normalized_action.value:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
        pool = self._advanced_pool()
        try:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    repeated = await connection.fetchrow(
                        """
                        SELECT * FROM review_policy_events
                         WHERE idempotency_key = $1
                         FOR UPDATE
                        """,
                        event["idempotency_key"],
                    )
                    if repeated:
                        self._validate_review_policy_event_identity(
                            repeated,
                            policy_id,
                            normalized_action.value,
                        )
                        row = await connection.fetchrow(
                            "SELECT * FROM review_policies WHERE id = $1",
                            policy_id,
                        )
                        return _record(row) if row else None

                    if normalized_action is ReviewPolicyEventAction.VALIDATE:
                        row = await self._validate_review_policy_record(
                            connection,
                            policy_id,
                            expected_policy_sha256,
                            validation_summary,
                        )
                    else:
                        row = await self._transition_review_policy_state(
                            connection,
                            policy_id,
                            action=normalized_action,
                            expected_policy_sha256=expected_policy_sha256,
                            expected_active_policy_id=expected_active_policy_id,
                            event=event,
                        )
                    if not row:
                        return None
                    await self._insert_review_policy_event(connection, policy_id, event)
            return _record(row)
        except asyncpg.UniqueViolationError as exc:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_ACTIVATION_CONFLICT.value) from exc

    async def _validate_review_policy_record(
        self,
        connection: asyncpg.Connection,
        policy_id: str,
        expected_policy_sha256: str,
        validation_summary: Mapping[str, Any] | None,
    ) -> asyncpg.Record | None:
        row = await connection.fetchrow(
            "SELECT * FROM review_policies WHERE id = $1 FOR UPDATE",
            policy_id,
        )
        if not row:
            return None
        self._validate_review_policy_sha(row, expected_policy_sha256)
        if str(row["status"]) not in {
            ReviewPolicyStatus.DRAFT.value,
            ReviewPolicyStatus.RETIRED.value,
        }:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
        return await connection.fetchrow(
            """
            UPDATE review_policies
               SET validation_summary = $2::jsonb,
                   validated_at = now(),
                   updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            policy_id,
            dict(validation_summary or {}),
        )

    async def _transition_review_policy_state(
        self,
        connection: asyncpg.Connection,
        policy_id: str,
        *,
        action: ReviewPolicyEventAction,
        expected_policy_sha256: str,
        expected_active_policy_id: str | None,
        event: Mapping[str, Any],
    ) -> asyncpg.Record | None:
        if action not in {
            ReviewPolicyEventAction.ACTIVATE,
            ReviewPolicyEventAction.RETIRE,
            ReviewPolicyEventAction.ROLLBACK,
        }:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)

        await connection.fetch(
            """
            SELECT id FROM review_policies
             WHERE is_default
          ORDER BY id
             FOR UPDATE
            """
        )
        row = await connection.fetchrow(
            "SELECT * FROM review_policies WHERE id = $1 FOR UPDATE",
            policy_id,
        )
        if not row:
            return None
        self._validate_review_policy_sha(row, expected_policy_sha256)
        if not bool(row["is_default"]):
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)

        active = await connection.fetchrow(
            """
            SELECT * FROM review_policies
             WHERE status = 'active' AND is_default
             FOR UPDATE
            """
        )
        active_id = str(active["id"]) if active else None
        if active_id != expected_active_policy_id:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_ACTIVATION_CONFLICT.value)

        if action in {
            ReviewPolicyEventAction.ACTIVATE,
            ReviewPolicyEventAction.ROLLBACK,
        }:
            expected_status = (
                ReviewPolicyStatus.DRAFT
                if action is ReviewPolicyEventAction.ACTIVATE
                else ReviewPolicyStatus.RETIRED
            )
            if str(row["status"]) != expected_status.value or not _policy_validation_is_current(
                row
            ):
                raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
            if active and str(active["id"]) != policy_id:
                retired = await connection.fetchrow(
                    """
                    UPDATE review_policies
                       SET status = 'retired', retired_at = now(), updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    active["id"],
                )
                await self._insert_superseded_policy_event(
                    connection,
                    retired,
                    target=row,
                    event=event,
                )
            return await connection.fetchrow(
                """
                UPDATE review_policies
                   SET status = 'active', activated_at = now(), retired_at = NULL,
                       updated_at = now()
                 WHERE id = $1
             RETURNING *
                """,
                policy_id,
            )

        if str(row["status"]) not in {
            ReviewPolicyStatus.DRAFT.value,
            ReviewPolicyStatus.ACTIVE.value,
        }:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
        return await connection.fetchrow(
            """
            UPDATE review_policies
               SET status = 'retired', retired_at = now(), updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            policy_id,
        )

    async def _insert_superseded_policy_event(
        self,
        connection: asyncpg.Connection,
        retired: Mapping[str, Any],
        *,
        target: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> None:
        payload = {
            **dict(event),
            "action": ReviewPolicyEventAction.RETIRE.value,
            "base_version": str(retired["version"]),
            "diff": {
                "redacted": True,
                "superseded_by_policy_sha256": str(target["policy_sha256"]),
            },
            "idempotency_key": (f"{event['idempotency_key']}:retire:{str(retired['id'])}"),
        }
        await self._insert_review_policy_event(connection, str(retired["id"]), payload)

    async def _insert_review_policy_event(
        self,
        connection: asyncpg.Connection,
        policy_id: str,
        payload: Mapping[str, Any],
    ) -> asyncpg.Record:
        existing = await connection.fetchrow(
            """
            SELECT * FROM review_policy_events
             WHERE idempotency_key = $1
             FOR UPDATE
            """,
            payload["idempotency_key"],
        )
        if existing:
            self._validate_review_policy_event_identity(
                existing,
                policy_id,
                str(payload["action"]),
            )
            return existing
        return await connection.fetchrow(
            """
            INSERT INTO review_policy_events (
                id, policy_id, action, actor_user_id, actor_nickname,
                reason, request_id, base_version, diff, idempotency_key
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
            RETURNING *
            """,
            payload.get("id") or new_domain_id("policy_event"),
            policy_id,
            payload["action"],
            payload.get("actor_user_id"),
            payload.get("actor_nickname", ""),
            payload.get("reason", ""),
            payload["request_id"],
            payload.get("base_version", ""),
            dict(payload.get("diff") or {}),
            payload["idempotency_key"],
        )

    @staticmethod
    def _validate_review_policy_event_identity(
        event: Mapping[str, Any],
        policy_id: str,
        action: str,
    ) -> None:
        if str(event["policy_id"]) != policy_id or str(event["action"]) != action:
            raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)

    @staticmethod
    def _validate_review_policy_sha(
        policy: Mapping[str, Any],
        expected_policy_sha256: str,
    ) -> None:
        if str(policy["policy_sha256"]) != expected_policy_sha256:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_VERSION_CONFLICT.value)

    async def bind_artifact_policy(
        self,
        artifact_id: str,
        policy_id: str,
    ) -> dict[str, Any] | None:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET policy_version_id = $2,
                           updated_at = now()
                     WHERE id = $1
                       AND (policy_version_id IS NULL OR policy_version_id = $2)
                 RETURNING *
                    """,
                    artifact_id,
                    policy_id,
                )
                if row:
                    return _record(row)
                current = await connection.fetchrow(
                    "SELECT * FROM plugin_artifacts WHERE id = $1",
                    artifact_id,
                )
                if not current:
                    return None
                raise ValueError(ArtifactErrorCode.ARTIFACT_POLICY_SNAPSHOT_CONFLICT.value)

    async def snapshot_active_review_policy(
        self,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                artifact = await connection.fetchrow(
                    "SELECT * FROM plugin_artifacts WHERE id = $1 FOR UPDATE",
                    artifact_id,
                )
                if not artifact:
                    return None
                if artifact["policy_version_id"] is not None:
                    return _record(artifact)
                if str(artifact["review_status"]) not in {
                    ReviewStatus.QUARANTINED.value,
                    ReviewStatus.PRECHECKING.value,
                    ReviewStatus.PROCESSING_FAILED.value,
                }:
                    raise ValueError(ArtifactErrorCode.ARTIFACT_POLICY_SNAPSHOT_CONFLICT.value)

                await connection.fetch(
                    """
                    SELECT id FROM review_policies
                     WHERE is_default
                  ORDER BY id
                     FOR SHARE
                    """
                )
                policy = await connection.fetchrow(
                    """
                    SELECT * FROM review_policies
                     WHERE status = 'active' AND is_default
                    """
                )
                if not policy or not _policy_validation_is_current(policy):
                    return _record(artifact)
                artifact = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET policy_version_id = $2, updated_at = now()
                     WHERE id = $1
                       AND policy_version_id IS NULL
                 RETURNING *
                    """,
                    artifact_id,
                    policy["id"],
                )
        return _record(artifact)

    async def migrate_artifact_policy(
        self,
        artifact_id: str,
        target_policy_id: str,
        *,
        actor: Mapping[str, Any],
        reason: str,
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    "SELECT * FROM review_decisions WHERE idempotency_key = $1 FOR UPDATE",
                    idempotency_key,
                )
                if existing:
                    if (
                        str(existing["artifact_id"]) != artifact_id
                        or str(existing["action"]) != DecisionAction.POLICY_MIGRATE.value
                        or str(existing["policy_version_id"] or "") != target_policy_id
                    ):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    artifact = await connection.fetchrow(
                        "SELECT * FROM plugin_artifacts WHERE id = $1",
                        artifact_id,
                    )
                    return _record(artifact) if artifact else None

                artifact = await connection.fetchrow(
                    "SELECT * FROM plugin_artifacts WHERE id = $1 FOR UPDATE",
                    artifact_id,
                )
                if not artifact:
                    return None
                if ReviewStatus(str(artifact["review_status"])) in TERMINAL_REVIEW_STATUSES:
                    raise ValueError(ArtifactErrorCode.ARTIFACT_POLICY_MIGRATION_FORBIDDEN.value)

                await connection.fetch(
                    """
                    SELECT id FROM review_policies
                     WHERE is_default
                  ORDER BY id
                     FOR SHARE
                    """
                )
                target = await connection.fetchrow(
                    "SELECT * FROM review_policies WHERE id = $1",
                    target_policy_id,
                )
                if (
                    not target
                    or str(target["status"])
                    not in {ReviewPolicyStatus.ACTIVE.value, ReviewPolicyStatus.RETIRED.value}
                    or not _policy_validation_is_current(target)
                ):
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)

                previous_policy_id = str(artifact["policy_version_id"] or "")
                if previous_policy_id == target_policy_id:
                    raise ValueError(ArtifactErrorCode.ARTIFACT_POLICY_SNAPSHOT_CONFLICT.value)
                previous = (
                    await connection.fetchrow(
                        "SELECT * FROM review_policies WHERE id = $1",
                        previous_policy_id,
                    )
                    if previous_policy_id
                    else None
                )
                migration = {
                    "from_policy_version_id": previous_policy_id or None,
                    "from_policy_sha256": str(previous["policy_sha256"]) if previous else "",
                    "to_policy_version_id": target_policy_id,
                    "to_policy_sha256": str(target["policy_sha256"]),
                    "invalidates_automated_review": True,
                    "request_id": request_id,
                }
                await connection.execute(
                    """
                    INSERT INTO review_decisions (
                        id, artifact_id, action, from_status, to_status, reason,
                        reviewer_user_id, reviewer_nickname, policy_version,
                        idempotency_key, source, policy_version_id, metadata
                    )
                    VALUES (
                        $1, $2, 'policy_migrate', $3, $3, $4, $5, $6,
                        $7, $8, 'admin', $9, $10::jsonb
                    )
                    """,
                    new_domain_id("decision"),
                    artifact_id,
                    artifact["review_status"],
                    reason,
                    actor.get("id"),
                    _actor_name(actor),
                    target["version"],
                    idempotency_key,
                    target_policy_id,
                    {"policy_migration": migration},
                )
                artifact = await connection.fetchrow(
                    """
                    UPDATE plugin_artifacts
                       SET policy_version_id = $2,
                           review_coverage = review_coverage
                               || jsonb_build_object('policy_migration', $3::jsonb),
                           automated_review_completed_at = NULL,
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    artifact_id,
                    target_policy_id,
                    migration,
                )
        return _record(artifact)

    async def update_artifact_review_coverage(
        self,
        artifact_id: str,
        coverage: Mapping[str, Any],
        *,
        automated_review_completed: bool = False,
    ) -> dict[str, Any] | None:
        row = await self._advanced_pool().fetchrow(
            """
            UPDATE plugin_artifacts
               SET review_coverage = $2::jsonb,
                   automated_review_completed_at = CASE
                       WHEN $3 THEN now()
                       ELSE automated_review_completed_at
                   END,
                   updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            artifact_id,
            dict(coverage),
            automated_review_completed,
        )
        return _record(row) if row else None

    async def replace_artifact_diffs(
        self,
        artifact_id: str,
        base_artifact_id: str | None,
        *,
        current_tree_sha256: str,
        base_tree_sha256: str | None,
        diffs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._validate_diff_trees(
                    connection,
                    artifact_id,
                    base_artifact_id,
                    current_tree_sha256,
                    base_tree_sha256,
                )
                await connection.execute(
                    "DELETE FROM artifact_file_diffs WHERE artifact_id = $1",
                    artifact_id,
                )
                records = [
                    (
                        str(item.get("id") or new_domain_id("diff")),
                        artifact_id,
                        base_artifact_id,
                        item.get("base_file_id"),
                        item.get("current_file_id"),
                        item["path"],
                        item.get("base_path", ""),
                        item["change_type"],
                        item.get("base_sha256"),
                        item.get("current_sha256"),
                        base_tree_sha256,
                        current_tree_sha256,
                        item.get("hunks_key"),
                        dict(item.get("stats") or {}),
                    )
                    for item in diffs
                ]
                if records:
                    await connection.executemany(
                        """
                        INSERT INTO artifact_file_diffs (
                            id, artifact_id, base_artifact_id, base_file_id,
                            current_file_id, path, base_path, change_type,
                            base_sha256, current_sha256, base_tree_sha256,
                            current_tree_sha256, hunks_key, stats
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12, $13, $14::jsonb
                        )
                        """,
                        records,
                    )
                rows = await connection.fetch(
                    """
                    SELECT * FROM artifact_file_diffs
                     WHERE artifact_id = $1
                  ORDER BY path
                    """,
                    artifact_id,
                )
        return [_record(row) for row in rows]

    async def list_artifact_diffs(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            SELECT d.*,
                   base_file.path AS resolved_base_path,
                   current_file.path AS resolved_current_path
              FROM artifact_file_diffs d
         LEFT JOIN artifact_files base_file ON base_file.id = d.base_file_id
         LEFT JOIN artifact_files current_file ON current_file.id = d.current_file_id
             WHERE d.artifact_id = $1
          ORDER BY d.path
            """,
            artifact_id,
        )
        return [_record(row) for row in rows]

    async def get_artifact_diff(self, artifact_id: str, diff_id: str) -> dict[str, Any] | None:
        row = await self._advanced_pool().fetchrow(
            """
            SELECT d.*,
                   base_file.path AS resolved_base_path,
                   current_file.path AS resolved_current_path
              FROM artifact_file_diffs d
         LEFT JOIN artifact_files base_file ON base_file.id = d.base_file_id
         LEFT JOIN artifact_files current_file ON current_file.id = d.current_file_id
             WHERE d.artifact_id = $1
               AND d.id = $2
            """,
            artifact_id,
            diff_id,
        )
        return _record(row) if row else None

    async def replace_dependency_edges(
        self,
        artifact_id: str,
        *,
        tree_sha256: str,
        edges: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                artifact = await connection.fetchrow(
                    "SELECT tree_sha256 FROM plugin_artifacts WHERE id = $1 FOR UPDATE",
                    artifact_id,
                )
                if not artifact:
                    return []
                if str(artifact["tree_sha256"]) != tree_sha256:
                    raise ValueError(ArtifactErrorCode.DIFF_TREE_CHANGED.value)
                await connection.execute(
                    "DELETE FROM artifact_dependency_edges WHERE artifact_id = $1",
                    artifact_id,
                )
                records = [
                    (
                        str(item.get("id") or new_domain_id("edge")),
                        artifact_id,
                        item["source_file_id"],
                        item.get("target_file_id"),
                        item.get("target_name", ""),
                        item["edge_type"],
                        item.get("confidence", 1),
                        item.get("line_start"),
                        dict(item.get("metadata") or {}),
                    )
                    for item in edges
                ]
                if records:
                    await connection.executemany(
                        """
                        INSERT INTO artifact_dependency_edges (
                            id, artifact_id, source_file_id, target_file_id,
                            target_name, edge_type, confidence, line_start, metadata
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                        """,
                        records,
                    )
                rows = await connection.fetch(
                    """
                    SELECT * FROM artifact_dependency_edges
                     WHERE artifact_id = $1
                  ORDER BY source_file_id, line_start NULLS FIRST, id
                    """,
                    artifact_id,
                )
        return [_record(row) for row in rows]

    async def list_dependency_edges(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            SELECT edge.*,
                   source.path AS source_path,
                   target.path AS target_path
              FROM artifact_dependency_edges edge
              JOIN artifact_files source ON source.id = edge.source_file_id
         LEFT JOIN artifact_files target ON target.id = edge.target_file_id
             WHERE edge.artifact_id = $1
          ORDER BY source.path, edge.line_start NULLS FIRST, edge.id
            """,
            artifact_id,
        )
        return [_record(row) for row in rows]

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
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                artifact = await connection.fetchrow(
                    """
                    SELECT plugin_id, tree_sha256, base_artifact_id
                      FROM plugin_artifacts
                     WHERE id = $1
                     FOR UPDATE
                    """,
                    artifact_id,
                )
                if not artifact:
                    return [], []
                if str(artifact["tree_sha256"]) != tree_sha256:
                    raise ValueError(ArtifactErrorCode.DIFF_TREE_CHANGED.value)
                if base_artifact_id is None:
                    if base_tree_sha256 is not None:
                        raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)
                else:
                    declared_base_id = str(artifact["base_artifact_id"] or "") or None
                    if base_artifact_id == artifact_id or base_artifact_id != declared_base_id:
                        raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)
                    base = await connection.fetchrow(
                        """
                        SELECT plugin_id, tree_sha256
                          FROM plugin_artifacts
                         WHERE id = $1
                         FOR SHARE
                        """,
                        base_artifact_id,
                    )
                    if (
                        base is None
                        or str(base["plugin_id"]) != str(artifact["plugin_id"])
                        or str(base["tree_sha256"]) != str(base_tree_sha256 or "")
                    ):
                        raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)
                registered_file_ids = {
                    str(row["id"])
                    for row in await connection.fetch(
                        "SELECT id FROM artifact_files WHERE artifact_id = $1 FOR UPDATE",
                        artifact_id,
                    )
                }
                _validate_graph_projection(files, edges, registered_file_ids)
                updated_files: list[dict[str, Any]] = []
                for item in files:
                    row = await connection.fetchrow(
                        """
                        UPDATE artifact_files
                           SET is_entrypoint = $3,
                               is_reachable = $4,
                               graph_status = $5,
                               scan_summary = $6::jsonb
                         WHERE id = $1
                           AND artifact_id = $2
                     RETURNING *
                        """,
                        item["file_id"],
                        artifact_id,
                        bool(item.get("is_entrypoint")),
                        bool(item.get("is_reachable")),
                        str(item.get("graph_status") or "not_analyzed"),
                        dict(item.get("scan_summary") or {}),
                    )
                    if row is None:
                        raise ValueError(ArtifactErrorCode.IMPORT_GRAPH_INCOMPLETE.value)
                    updated_files.append(_record(row))
                await connection.execute(
                    "DELETE FROM artifact_dependency_edges WHERE artifact_id = $1",
                    artifact_id,
                )
                edge_records = [
                    (
                        str(item.get("id") or new_domain_id("edge")),
                        artifact_id,
                        item["source_file_id"],
                        item.get("target_file_id"),
                        item.get("target_name", ""),
                        item["edge_type"],
                        item.get("confidence", 1),
                        item.get("line_start"),
                        dict(item.get("metadata") or {}),
                    )
                    for item in edges
                ]
                if edge_records:
                    await connection.executemany(
                        """
                        INSERT INTO artifact_dependency_edges (
                            id, artifact_id, source_file_id, target_file_id,
                            target_name, edge_type, confidence, line_start, metadata
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                        """,
                        edge_records,
                    )
                saved_edges = await connection.fetch(
                    """
                    SELECT * FROM artifact_dependency_edges
                     WHERE artifact_id = $1
                  ORDER BY source_file_id, line_start NULLS FIRST, id
                    """,
                    artifact_id,
                )
                await connection.execute(
                    """
                    UPDATE plugin_artifacts
                       SET review_coverage = review_coverage
                           || jsonb_build_object('import_graph', $2::jsonb),
                           updated_at = now()
                     WHERE id = $1
                    """,
                    artifact_id,
                    dict(coverage),
                )
        updated_files.sort(key=lambda item: str(item.get("path") or ""))
        return updated_files, [_record(row) for row in saved_edges]

    async def create_runtime_dispatch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        row = await self._advanced_pool().fetchrow(
            """
            INSERT INTO runtime_dispatches (
                id, artifact_id, run_id, status, request, request_sha256,
                result_key, result_sha256, runner_id, image_digest,
                attempts, max_attempts, error_code, error_message
            )
            VALUES (
                $1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10,
                $11, $12, $13, $14
            )
            ON CONFLICT (run_id) WHERE status <> 'cancelled'
            DO UPDATE SET run_id = EXCLUDED.run_id
            RETURNING *
            """,
            payload.get("id") or new_domain_id("dispatch"),
            payload["artifact_id"],
            payload["run_id"],
            payload.get("status", RuntimeDispatchStatus.QUEUED.value),
            dict(payload.get("request") or {}),
            payload["request_sha256"],
            payload.get("result_key"),
            payload.get("result_sha256"),
            payload.get("runner_id", ""),
            payload.get("image_digest", ""),
            int(payload.get("attempts") or 0),
            int(payload.get("max_attempts") or 3),
            payload.get("error_code", ""),
            payload.get("error_message", ""),
        )
        saved = _record(row)
        if (
            str(saved["artifact_id"]) != str(payload["artifact_id"])
            or str(saved["run_id"]) != str(payload["run_id"])
            or str(saved["request_sha256"]) != str(payload["request_sha256"])
        ):
            raise ValueError(ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value)
        return saved

    async def get_runtime_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        row = await self._advanced_pool().fetchrow(
            "SELECT * FROM runtime_dispatches WHERE id = $1",
            dispatch_id,
        )
        return _record(row) if row else None

    async def claim_runtime_dispatches(
        self,
        runner_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            WITH candidates AS (
                SELECT id
                  FROM runtime_dispatches
                 WHERE attempts < max_attempts
                   AND (
                       status = 'queued'
                       OR (status = 'running' AND lease_expires_at < now())
                   )
              ORDER BY queued_at, id
                 FOR UPDATE SKIP LOCKED
                 LIMIT $2
            )
            UPDATE runtime_dispatches dispatch
               SET status = 'running',
                   attempts = dispatch.attempts + 1,
                   runner_id = $1,
                   lease_owner = $1,
                   lease_expires_at = now() + ($3 * interval '1 second'),
                   started_at = COALESCE(dispatch.started_at, now()),
                   updated_at = now()
              FROM candidates
             WHERE dispatch.id = candidates.id
         RETURNING dispatch.*
            """,
            runner_id,
            limit,
            lease_seconds,
        )
        return [_record(row) for row in rows]

    async def renew_runtime_dispatch_lease(
        self,
        dispatch_id: str,
        runner_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await self._advanced_pool().execute(
            """
            UPDATE runtime_dispatches
               SET lease_expires_at = now() + ($3 * interval '1 second'),
                   updated_at = now()
             WHERE id = $1
               AND status = 'running'
               AND lease_owner = $2
            """,
            dispatch_id,
            runner_id,
            lease_seconds,
        )
        return result.endswith(" 1")

    async def complete_runtime_dispatch(
        self,
        dispatch_id: str,
        runner_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        status = _validate_runtime_completion_payload(payload)
        row = await self._advanced_pool().fetchrow(
            """
            UPDATE runtime_dispatches
               SET status = $3,
                   result_key = $4,
                   result_sha256 = $5,
                   image_digest = COALESCE(NULLIF($6, ''), image_digest),
                   error_code = $7,
                   error_message = $8,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   completed_at = now(),
                   updated_at = now()
             WHERE id = $1
               AND status = 'running'
               AND lease_owner = $2
         RETURNING *
            """,
            dispatch_id,
            runner_id,
            status.value,
            payload.get("result_key"),
            payload.get("result_sha256"),
            payload.get("image_digest", ""),
            payload.get("error_code", ""),
            payload.get("error_message", ""),
        )
        return _record(row) if row else None

    async def expire_runtime_dispatches(self, limit: int) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            WITH candidates AS (
                SELECT id
                  FROM runtime_dispatches
                 WHERE status = 'running'
                   AND attempts >= max_attempts
                   AND lease_expires_at < now()
              ORDER BY lease_expires_at, id
                 FOR UPDATE SKIP LOCKED
                 LIMIT $1
            )
            UPDATE runtime_dispatches dispatch
               SET status = 'timed_out',
                   error_code = 'runtime_dispatch_timeout',
                   error_message = 'Runtime runner exhausted all lease attempts',
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   completed_at = now(),
                   updated_at = now()
              FROM candidates
             WHERE dispatch.id = candidates.id
         RETURNING dispatch.*
            """,
            limit,
        )
        return [_record(row) for row in rows]

    async def cancel_runtime_dispatch(
        self,
        dispatch_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any] | None:
        row = await self._advanced_pool().fetchrow(
            """
            UPDATE runtime_dispatches
               SET status = 'cancelled',
                   error_code = $2,
                   error_message = $3,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   completed_at = now(),
                   updated_at = now()
             WHERE id = $1
               AND status IN ('queued', 'running')
         RETURNING *
            """,
            dispatch_id,
            error_code,
            error_message,
        )
        return _record(row) if row else None

    async def collect_runtime_dispatch(
        self,
        dispatch_id: str,
        run_payload: Mapping[str, Any] | None = None,
        findings: Sequence[Mapping[str, Any]] = (),
        sbom_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                dispatch = await connection.fetchrow(
                    """
                    SELECT * FROM runtime_dispatches
                     WHERE id = $1
                       AND collected_at IS NULL
                       AND status IN ('succeeded', 'failed', 'timed_out', 'cancelled')
                     FOR UPDATE
                    """,
                    dispatch_id,
                )
                if not dispatch:
                    return None
                if run_payload is not None:
                    status = str(run_payload["status"])
                    if status not in {"succeeded", "failed", "timed_out", "cancelled"}:
                        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                    run = await connection.fetchrow(
                        """
                        UPDATE review_runs
                           SET status = $2,
                               summary = $3,
                               raw_result = $4::jsonb,
                               raw_result_key = $5,
                               error_code = $6,
                               output_sha256 = COALESCE(NULLIF($7, ''), output_sha256),
                               coverage = $8::jsonb,
                               container_image_digest = COALESCE(
                                   NULLIF($9, ''), container_image_digest
                               ),
                               dependency_snapshot_sha256 = COALESCE(
                                   NULLIF($10, ''), dependency_snapshot_sha256
                               ),
                               worker_id = COALESCE(NULLIF($11, ''), worker_id),
                               completed_at = now()
                         WHERE id = $1
                           AND type = 'runtime'
                           AND status IN ('queued', 'running')
                     RETURNING *
                        """,
                        dispatch["run_id"],
                        status,
                        run_payload.get("summary", ""),
                        dict(run_payload.get("raw_result") or {}),
                        run_payload.get("raw_result_key"),
                        run_payload.get("error_code", ""),
                        run_payload.get("output_sha256", ""),
                        dict(run_payload.get("coverage") or {}),
                        run_payload.get("container_image_digest", ""),
                        run_payload.get("dependency_snapshot_sha256", ""),
                        run_payload.get("worker_id", ""),
                    )
                    if not run:
                        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                    for finding in findings:
                        await _upsert_runtime_finding(
                            connection,
                            str(dispatch["artifact_id"]),
                            str(dispatch["run_id"]),
                            finding,
                        )
                    if sbom_payload is not None:
                        if str(sbom_payload.get("artifact_id") or "") != str(
                            dispatch["artifact_id"]
                        ) or str(sbom_payload.get("run_id") or "") != str(dispatch["run_id"]):
                            raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                        saved_sbom = await connection.fetchrow(
                            """
                            INSERT INTO artifact_sboms (
                                id, artifact_id, run_id, format, document_sha256,
                                object_key, package_count, generator, tool_version
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                            ON CONFLICT (artifact_id, run_id, format, document_sha256)
                            DO UPDATE SET document_sha256 = EXCLUDED.document_sha256
                              WHERE artifact_sboms.object_key = EXCLUDED.object_key
                            RETURNING object_key
                            """,
                            sbom_payload.get("id") or new_domain_id("sbom"),
                            sbom_payload["artifact_id"],
                            sbom_payload["run_id"],
                            sbom_payload["format"],
                            sbom_payload["document_sha256"],
                            sbom_payload["object_key"],
                            int(sbom_payload.get("package_count") or 0),
                            sbom_payload["generator"],
                            sbom_payload.get("tool_version", ""),
                        )
                        if not saved_sbom:
                            raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                row = await connection.fetchrow(
                    """
                    UPDATE runtime_dispatches
                       SET collected_at = now(),
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    dispatch_id,
                )
        return _record(row)

    async def create_review_comment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    payload["idempotency_key"],
                )
                existing = await connection.fetchrow(
                    "SELECT * FROM review_comments WHERE idempotency_key = $1",
                    payload["idempotency_key"],
                )
                if existing:
                    create_event = await connection.fetchrow(
                        """
                        SELECT * FROM review_comment_events
                         WHERE thread_id = $1 AND type = 'create'
                      ORDER BY created_at, id
                         LIMIT 1
                        """,
                        existing["id"],
                    )
                    if not _same_comment_creation(existing, create_event, payload):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    return _record(existing)
                artifact = await connection.fetchrow(
                    "SELECT review_status FROM plugin_artifacts WHERE id = $1 FOR UPDATE",
                    payload["artifact_id"],
                )
                if not artifact:
                    raise ValueError(ArtifactErrorCode.COMMENT_LINE_INVALID.value)
                if ReviewStatus(str(artifact["review_status"])) in TERMINAL_REVIEW_STATUSES:
                    raise ValueError(ArtifactErrorCode.COMMENT_THREAD_LOCKED.value)
                thread_id = str(payload.get("id") or new_domain_id("comment"))
                row = await connection.fetchrow(
                    """
                    INSERT INTO review_comments (
                        id, artifact_id, source_thread_id, file_id, file_path,
                        file_sha256, side, line_start, line_end, body,
                        reviewer_user_id, reviewer_nickname, reviewer_role,
                        idempotency_key
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14
                    )
                    RETURNING *
                    """,
                    thread_id,
                    payload["artifact_id"],
                    payload.get("source_thread_id"),
                    payload.get("file_id"),
                    payload["file_path"],
                    payload["file_sha256"],
                    payload["side"],
                    int(payload["line_start"]),
                    int(payload["line_end"]),
                    payload["body"],
                    payload.get("reviewer_user_id"),
                    payload.get("reviewer_nickname", ""),
                    payload["reviewer_role"],
                    payload["idempotency_key"],
                )
                await connection.execute(
                    """
                    INSERT INTO review_comment_events (
                        id, thread_id, artifact_id, type, body, actor_user_id,
                        actor_nickname, actor_role, expected_version,
                        resulting_version, metadata, idempotency_key
                    )
                    VALUES (
                        $1, $2, $3, 'create', $4, $5, $6, $7, 0, 1,
                        $8::jsonb, $9
                    )
                    """,
                    new_domain_id("comment_event"),
                    thread_id,
                    payload["artifact_id"],
                    payload["body"],
                    payload.get("reviewer_user_id"),
                    payload.get("reviewer_nickname", ""),
                    payload["reviewer_role"],
                    dict(payload.get("metadata") or {}),
                    payload.get("event_idempotency_key") or f"{payload['idempotency_key']}:create",
                )
        return _record(row)

    async def append_review_comment_event(
        self,
        thread_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        event_type = ReviewCommentEventType(str(payload["type"]))
        if event_type is ReviewCommentEventType.CREATE:
            raise ValueError(ArtifactErrorCode.COMMENT_VERSION_CONFLICT.value)
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    payload["idempotency_key"],
                )
                existing_event = await connection.fetchrow(
                    "SELECT * FROM review_comment_events WHERE idempotency_key = $1",
                    payload["idempotency_key"],
                )
                if existing_event:
                    if not _same_comment_event(existing_event, thread_id, payload):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    existing_thread = await connection.fetchrow(
                        "SELECT * FROM review_comments WHERE id = $1",
                        thread_id,
                    )
                    return _record(existing_thread) if existing_thread else None
                thread_identity = await connection.fetchrow(
                    "SELECT artifact_id FROM review_comments WHERE id = $1",
                    thread_id,
                )
                if not thread_identity:
                    return None
                artifact = await connection.fetchrow(
                    "SELECT review_status FROM plugin_artifacts WHERE id = $1 FOR UPDATE",
                    thread_identity["artifact_id"],
                )
                thread = await connection.fetchrow(
                    "SELECT * FROM review_comments WHERE id = $1 FOR UPDATE",
                    thread_id,
                )
                if not artifact or not thread:
                    return None
                if (
                    ReviewStatus(str(artifact["review_status"])) in TERMINAL_REVIEW_STATUSES
                    or thread["locked_at"] is not None
                ):
                    raise ValueError(ArtifactErrorCode.COMMENT_THREAD_LOCKED.value)
                expected_version = int(payload["expected_version"])
                if int(thread["version"]) != expected_version:
                    raise ValueError(ArtifactErrorCode.COMMENT_VERSION_CONFLICT.value)
                resulting_version = expected_version + 1
                row = await connection.fetchrow(
                    """
                    UPDATE review_comments
                       SET body = CASE WHEN $2 = 'edit' THEN $3 ELSE body END,
                           resolved = CASE
                               WHEN $2 = 'resolve' THEN true
                               WHEN $2 = 'reopen' THEN false
                               ELSE resolved
                           END,
                           resolved_by_user_id = CASE
                               WHEN $2 = 'resolve' THEN $4
                               WHEN $2 = 'reopen' THEN NULL
                               ELSE resolved_by_user_id
                           END,
                           resolved_by_nickname = CASE
                               WHEN $2 = 'resolve' THEN $5
                               WHEN $2 = 'reopen' THEN ''
                               ELSE resolved_by_nickname
                           END,
                           resolved_at = CASE
                               WHEN $2 = 'resolve' THEN now()
                               WHEN $2 = 'reopen' THEN NULL
                               ELSE resolved_at
                           END,
                           version = $6,
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    thread_id,
                    event_type.value,
                    payload.get("body", ""),
                    payload.get("actor_user_id"),
                    payload.get("actor_nickname", ""),
                    resulting_version,
                )
                await connection.execute(
                    """
                    INSERT INTO review_comment_events (
                        id, thread_id, artifact_id, type, body, actor_user_id,
                        actor_nickname, actor_role, expected_version,
                        resulting_version, metadata, idempotency_key
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11::jsonb, $12
                    )
                    """,
                    payload.get("id") or new_domain_id("comment_event"),
                    thread_id,
                    thread["artifact_id"],
                    event_type.value,
                    payload.get("body", ""),
                    payload.get("actor_user_id"),
                    payload.get("actor_nickname", ""),
                    payload["actor_role"],
                    expected_version,
                    resulting_version,
                    dict(payload.get("metadata") or {}),
                    payload["idempotency_key"],
                )
        return _record(row)

    async def get_review_comment(
        self,
        artifact_id: str,
        thread_id: str,
        *,
        event_limit: int = 20,
    ) -> dict[str, Any] | None:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            thread = await connection.fetchrow(
                "SELECT * FROM review_comments WHERE artifact_id = $1 AND id = $2",
                artifact_id,
                thread_id,
            )
            if not thread:
                return None
            events = await connection.fetch(
                """
                SELECT * FROM review_comment_events
                 WHERE artifact_id = $1 AND thread_id = $2
              ORDER BY created_at DESC, id DESC
                 LIMIT $3
                """,
                artifact_id,
                thread_id,
                event_limit,
            )
            event_count = await connection.fetchval(
                """
                SELECT count(*) FROM review_comment_events
                 WHERE artifact_id = $1 AND thread_id = $2
                """,
                artifact_id,
                thread_id,
            )
        selected = [_record(event) for event in reversed(events)]
        return {
            **_record(thread),
            "events": selected,
            "event_count": int(event_count or 0),
            "events_truncated": int(event_count or 0) > len(selected),
        }

    async def list_review_comments(
        self,
        artifact_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        event_limit: int = 20,
    ) -> list[dict[str, Any]]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            threads = await connection.fetch(
                """
                SELECT * FROM review_comments
                 WHERE artifact_id = $1
              ORDER BY file_path, line_start, created_at, id
                 LIMIT $2 OFFSET $3
                """,
                artifact_id,
                limit,
                offset,
            )
            thread_ids = [str(thread["id"]) for thread in threads]
            if not thread_ids:
                return []
            events = await connection.fetch(
                """
                SELECT selected.*
                  FROM unnest($2::text[]) AS target(thread_id)
            CROSS JOIN LATERAL (
                        SELECT * FROM review_comment_events event
                         WHERE event.artifact_id = $1
                           AND event.thread_id = target.thread_id
                      ORDER BY event.created_at DESC, event.id DESC
                         LIMIT $3
                    ) AS selected
                """,
                artifact_id,
                thread_ids,
                event_limit,
            )
            counts = await connection.fetch(
                """
                SELECT thread_id, count(*) AS total
                  FROM review_comment_events
                 WHERE artifact_id = $1
                   AND thread_id = ANY($2::text[])
              GROUP BY thread_id
                """,
                artifact_id,
                thread_ids,
            )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            grouped.setdefault(str(event["thread_id"]), []).append(_record(event))
        event_counts = {str(item["thread_id"]): int(item["total"]) for item in counts}
        for values in grouped.values():
            values.sort(key=lambda item: (item["created_at"], item["id"]))
        return [
            {
                **_record(thread),
                "events": grouped.get(str(thread["id"]), []),
                "event_count": event_counts.get(str(thread["id"]), 0),
                "events_truncated": event_counts.get(str(thread["id"]), 0)
                > len(grouped.get(str(thread["id"]), [])),
            }
            for thread in threads
        ]

    async def count_review_comments(self, artifact_id: str) -> int:
        value = await self._advanced_pool().fetchrow(
            "SELECT count(*) AS total FROM review_comments WHERE artifact_id = $1",
            artifact_id,
        )
        return int(value["total"] if value else 0)

    async def lock_review_comments(self, artifact_id: str) -> int:
        result = await self._advanced_pool().execute(
            """
            UPDATE review_comments
               SET locked_at = COALESCE(locked_at, now()),
                   updated_at = now()
             WHERE artifact_id = $1
               AND locked_at IS NULL
            """,
            artifact_id,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def update_finding_state(
        self,
        finding_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                existing_event = await connection.fetchrow(
                    "SELECT * FROM review_finding_events WHERE idempotency_key = $1",
                    payload["idempotency_key"],
                )
                if existing_event:
                    if not _same_finding_event_request(existing_event, finding_id, payload):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    existing = await connection.fetchrow(
                        "SELECT * FROM review_findings WHERE id = $1",
                        finding_id,
                    )
                    return _record(existing) if existing else None
                finding = await connection.fetchrow(
                    "SELECT * FROM review_findings WHERE id = $1 FOR UPDATE",
                    finding_id,
                )
                if not finding:
                    return None
                if int(finding["version"]) != int(payload["expected_version"]):
                    raise ValueError(ArtifactErrorCode.FINDING_VERSION_CONFLICT.value)
                status = str(payload.get("status") or finding["status"])
                FindingStatus(status)
                correlation = (
                    dict(payload["correlation"])
                    if "correlation" in payload
                    else dict(finding["correlation"] or {})
                )
                affects_current_release = (
                    bool(payload["affects_current_release"])
                    if "affects_current_release" in payload
                    else bool(finding["affects_current_release"])
                )
                next_version = int(finding["version"]) + 1
                row = await connection.fetchrow(
                    """
                    UPDATE review_findings
                       SET status = $2,
                           correlation = $3::jsonb,
                           affects_current_release = $4,
                           status_actor_user_id = $5,
                           status_actor_nickname = $6,
                           status_updated_at = now(),
                           version = $7
                     WHERE id = $1
                 RETURNING *
                    """,
                    finding_id,
                    status,
                    correlation,
                    affects_current_release,
                    payload.get("actor_user_id"),
                    payload.get("actor_nickname", ""),
                    next_version,
                )
                event_type = _finding_event_type(finding, status, affects_current_release)
                await connection.execute(
                    """
                    INSERT INTO review_finding_events (
                        id, finding_id, artifact_id, type, from_status, to_status,
                        actor_user_id, actor_nickname, actor_source, reason,
                        metadata, idempotency_key
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11::jsonb, $12
                    )
                    """,
                    payload.get("id") or new_domain_id("finding_event"),
                    finding_id,
                    finding["artifact_id"],
                    event_type,
                    finding["status"],
                    status,
                    payload.get("actor_user_id"),
                    payload.get("actor_nickname", ""),
                    payload.get("actor_source", "user"),
                    payload.get("reason", ""),
                    dict(payload.get("metadata") or {}),
                    payload["idempotency_key"],
                )
        return _record(row)

    async def list_finding_events(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            SELECT * FROM review_finding_events
             WHERE artifact_id = $1
          ORDER BY created_at, id
            """,
            artifact_id,
        )
        return [_record(row) for row in rows]

    async def get_review_finding(self, artifact_id: str, finding_id: str) -> dict[str, Any] | None:
        row = await self._advanced_pool().fetchrow(
            "SELECT * FROM review_findings WHERE artifact_id = $1 AND id = $2",
            artifact_id,
            finding_id,
        )
        return _record(row) if row else None

    async def create_artifact_sbom(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                run = await connection.fetchrow(
                    "SELECT artifact_id FROM review_runs WHERE id = $1",
                    payload["run_id"],
                )
                if not run or str(run["artifact_id"]) != str(payload["artifact_id"]):
                    raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                row = await connection.fetchrow(
                    """
                    INSERT INTO artifact_sboms (
                        id, artifact_id, run_id, format, document_sha256,
                        object_key, package_count, generator, tool_version
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (artifact_id, run_id, format, document_sha256)
                    DO UPDATE SET document_sha256 = EXCLUDED.document_sha256
                      WHERE artifact_sboms.object_key = EXCLUDED.object_key
                    RETURNING *
                    """,
                    payload.get("id") or new_domain_id("sbom"),
                    payload["artifact_id"],
                    payload["run_id"],
                    payload["format"],
                    payload["document_sha256"],
                    payload["object_key"],
                    int(payload.get("package_count") or 0),
                    payload["generator"],
                    payload.get("tool_version", ""),
                )
                if not row:
                    raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
        return _record(row)

    async def list_artifact_sboms(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = await self._advanced_pool().fetch(
            """
            SELECT * FROM artifact_sboms
             WHERE artifact_id = $1
          ORDER BY created_at, id
            """,
            artifact_id,
        )
        return [_record(row) for row in rows]

    async def list_review_history_records(
        self,
        artifact_id: str,
        *,
        limit: int,
        after: tuple[datetime, str, str] | None,
    ) -> list[dict[str, Any]]:
        after_time, after_type, after_id = after or (None, "", "")
        rows = await self._advanced_pool().fetch(
            """
            WITH history AS (
                SELECT run.id,
                       'run'::text AS type,
                       run.created_at AS occurred_at,
                       'system'::text AS source,
                       ''::text AS actor_nickname,
                       'system'::text AS actor_role,
                       COALESCE(run.idempotency_key, '') AS idempotency_key,
                       run.policy_version_id,
                       jsonb_build_object(
                           'run_type', run.type,
                           'status', run.status,
                           'attempt', run.attempt,
                           'summary', left(run.summary, 2000),
                           'error_code', run.error_code,
                           'tool_name', run.tool_name,
                           'tool_version', run.tool_version,
                           'ruleset_version', run.ruleset_version,
                           'model', run.model,
                           'coverage', run.coverage
                       ) AS payload
                  FROM review_runs run
                 WHERE run.artifact_id = $1

                UNION ALL

                SELECT finding.id,
                       'finding'::text,
                       finding.created_at,
                       finding.source,
                       ''::text,
                       'system'::text,
                       ''::text,
                       run.policy_version_id,
                       jsonb_build_object(
                           'finding_id', finding.id,
                           'run_id', finding.run_id,
                           'fingerprint', finding.fingerprint,
                           'rule_id', finding.rule_id,
                           'severity', finding.severity,
                           'category', finding.category,
                           'message', left(finding.message, 2000),
                           'file_path', finding.file_path,
                           'line_start', finding.line_start,
                           'line_end', finding.line_end,
                           'status', finding.status,
                           'deterministic', finding.deterministic,
                           'affects_current_release', finding.affects_current_release,
                           'correlation', finding.correlation
                       )
                  FROM review_findings finding
             LEFT JOIN review_runs run ON run.id = finding.run_id
                 WHERE finding.artifact_id = $1

                UNION ALL

                SELECT event.id,
                       'finding_event'::text,
                       event.created_at,
                       event.actor_source,
                       event.actor_nickname,
                       CASE WHEN event.actor_source = 'user' THEN 'admin' ELSE 'system' END,
                       event.idempotency_key,
                       run.policy_version_id,
                       jsonb_build_object(
                           'finding_id', event.finding_id,
                           'event_type', event.type,
                           'from_status', event.from_status,
                           'to_status', event.to_status,
                           'reason', left(event.reason, 2000),
                           'metadata', event.metadata
                       )
                  FROM review_finding_events event
                  JOIN review_findings finding ON finding.id = event.finding_id
             LEFT JOIN review_runs run ON run.id = finding.run_id
                 WHERE event.artifact_id = $1

                UNION ALL

                SELECT event.id,
                       'comment_event'::text,
                       event.created_at,
                       CASE WHEN event.actor_role = 'system' THEN 'system' ELSE 'user' END,
                       event.actor_nickname,
                       event.actor_role,
                       event.idempotency_key,
                       artifact.policy_version_id,
                       jsonb_build_object(
                           'thread_id', event.thread_id,
                           'event_type', event.type,
                           'body_preview', left(event.body, 500),
                           'expected_version', event.expected_version,
                           'resulting_version', event.resulting_version
                       )
                  FROM review_comment_events event
                  JOIN plugin_artifacts artifact ON artifact.id = event.artifact_id
                 WHERE event.artifact_id = $1

                UNION ALL

                SELECT decision.id,
                       'decision'::text,
                       decision.created_at,
                       decision.source,
                       decision.reviewer_nickname,
                       CASE WHEN decision.reviewer_user_id IS NULL THEN 'system' ELSE 'admin' END,
                       decision.idempotency_key,
                       decision.policy_version_id,
                       jsonb_build_object(
                           'action', decision.action,
                           'from_status', decision.from_status,
                           'to_status', decision.to_status,
                           'reason', left(decision.reason, 2000),
                           'policy_version', decision.policy_version,
                           'input_run_ids', decision.input_run_ids,
                           'input_fingerprints', decision.input_fingerprints,
                           'coverage_sha256', decision.coverage_sha256,
                           'metadata', decision.metadata
                       )
                  FROM review_decisions decision
                 WHERE decision.artifact_id = $1

                UNION ALL

                SELECT event.id,
                       'policy_event'::text,
                       event.created_at,
                       'user'::text,
                       event.actor_nickname,
                       'core_admin'::text,
                       event.idempotency_key,
                       event.policy_id,
                       jsonb_build_object(
                           'action', event.action,
                           'reason', left(event.reason, 2000),
                           'request_id', event.request_id,
                           'base_version', event.base_version
                       )
                  FROM review_policy_events event
                  JOIN plugin_artifacts artifact ON artifact.policy_version_id = event.policy_id
                 WHERE artifact.id = $1

                UNION ALL

                SELECT artifact.id || ':submitted',
                       'artifact_submitted'::text,
                       artifact.created_at,
                       'user'::text,
                       COALESCE(artifact.submitted_by_snapshot->>'nickname', ''),
                       'author'::text,
                       ''::text,
                       artifact.policy_version_id,
                       jsonb_build_object(
                           'source_type', artifact.source_type,
                           'source_ref', artifact.source_ref,
                           'source_commit_sha', artifact.source_commit_sha
                       )
                  FROM plugin_artifacts artifact
                 WHERE artifact.id = $1

                UNION ALL

                SELECT artifact.id || ':published',
                       'publication_published'::text,
                       artifact.published_at,
                       'system'::text,
                       ''::text,
                       'system'::text,
                       ''::text,
                       artifact.policy_version_id,
                       jsonb_build_object('publication_status', 'published')
                  FROM plugin_artifacts artifact
                 WHERE artifact.id = $1 AND artifact.published_at IS NOT NULL

                UNION ALL

                SELECT artifact.id || ':revoked',
                       'publication_revoked'::text,
                       artifact.revoked_at,
                       'system'::text,
                       ''::text,
                       'system'::text,
                       ''::text,
                       artifact.policy_version_id,
                       jsonb_build_object('publication_status', 'revoked')
                  FROM plugin_artifacts artifact
                 WHERE artifact.id = $1 AND artifact.revoked_at IS NOT NULL

                UNION ALL

                SELECT event.id,
                       CASE event.event_type
                           WHEN 'artifact_publish_failed' THEN 'publication_publish_failed'
                           ELSE 'publication_revoke_failed'
                       END,
                       event.created_at,
                       'system'::text,
                       ''::text,
                       'system'::text,
                       event.dedupe_key,
                       artifact.policy_version_id,
                       jsonb_strip_nulls(
                           jsonb_build_object(
                               'publication_status',
                               CASE event.event_type
                                   WHEN 'artifact_publish_failed' THEN 'publish_failed'
                                   ELSE 'revoke_failed'
                               END,
                               'code', event.payload->>'code'
                           )
                       )
                  FROM outbox_events event
                  JOIN plugin_artifacts artifact ON artifact.id = event.aggregate_id
                 WHERE event.aggregate_type = 'artifact'
                   AND event.aggregate_id = $1
                   AND event.event_type IN (
                       'artifact_publish_failed', 'artifact_revoke_failed'
                   )
            )
            SELECT id, type, occurred_at, source, actor_nickname, actor_role,
                   idempotency_key, policy_version_id, payload
              FROM history
             WHERE $2::timestamptz IS NULL
                OR (occurred_at, type, id) > ($2::timestamptz, $3::text, $4::text)
          ORDER BY occurred_at, type, id
             LIMIT $5
            """,
            artifact_id,
            after_time,
            after_type,
            after_id,
            limit,
        )
        return [_record(row) for row in rows]

    async def get_review_history_sources(self, artifact_id: str) -> dict[str, Any]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            artifact = await connection.fetchrow(
                "SELECT * FROM plugin_artifacts WHERE id = $1",
                artifact_id,
            )
            runs = await connection.fetch(
                "SELECT * FROM review_runs WHERE artifact_id = $1 ORDER BY created_at, id",
                artifact_id,
            )
            finding_events = await connection.fetch(
                """
                SELECT * FROM review_finding_events
                 WHERE artifact_id = $1
              ORDER BY created_at, id
                """,
                artifact_id,
            )
            comment_events = await connection.fetch(
                """
                SELECT * FROM review_comment_events
                 WHERE artifact_id = $1
              ORDER BY created_at, id
                """,
                artifact_id,
            )
            decisions = await connection.fetch(
                """
                SELECT * FROM review_decisions
                 WHERE artifact_id = $1
              ORDER BY created_at, id
                """,
                artifact_id,
            )
            policy_events = await connection.fetch(
                """
                SELECT event.*
                  FROM review_policy_events event
                  JOIN plugin_artifacts artifact ON artifact.policy_version_id = event.policy_id
                 WHERE artifact.id = $1
              ORDER BY event.created_at, event.id
                """,
                artifact_id,
            )
        return {
            "artifact": _record(artifact) if artifact else None,
            "runs": [_record(row) for row in runs],
            "finding_events": [_record(row) for row in finding_events],
            "comment_events": [_record(row) for row in comment_events],
            "decisions": [_record(row) for row in decisions],
            "policy_events": [_record(row) for row in policy_events],
        }

    async def _validate_diff_trees(
        self,
        connection: asyncpg.Connection,
        artifact_id: str,
        base_artifact_id: str | None,
        current_tree_sha256: str,
        base_tree_sha256: str | None,
    ) -> None:
        current = await connection.fetchrow(
            "SELECT plugin_id, tree_sha256 FROM plugin_artifacts WHERE id = $1 FOR UPDATE",
            artifact_id,
        )
        if not current or str(current["tree_sha256"]) != current_tree_sha256:
            raise ValueError(ArtifactErrorCode.DIFF_TREE_CHANGED.value)
        if base_artifact_id is None:
            if base_tree_sha256 is not None:
                raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)
            return
        base = await connection.fetchrow(
            "SELECT plugin_id, tree_sha256 FROM plugin_artifacts WHERE id = $1",
            base_artifact_id,
        )
        if (
            not base
            or str(base["plugin_id"]) != str(current["plugin_id"])
            or str(base["tree_sha256"]) != str(base_tree_sha256 or "")
        ):
            raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)

    def _advanced_pool(self) -> asyncpg.Pool:
        return self.store._pool()


async def _upsert_runtime_finding(
    connection: Any,
    artifact_id: str,
    run_id: str,
    finding: Mapping[str, Any],
) -> None:
    await connection.fetchrow(
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
            $13, $14, $15, $16::jsonb, 'runtime', $17, $18, $19,
            $20, $21::jsonb
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
        bool(finding.get("deterministic", True)),
        finding.get("file_id"),
        finding.get("file_sha256"),
        bool(finding.get("affects_current_release")),
        dict(finding.get("correlation") or {}),
    )


class InMemoryAdvancedReviewRepositoryMixin:
    async def create_review_policy(
        self,
        payload: Mapping[str, Any],
        event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            repeated_event = None
            if event:
                repeated_event = next(
                    (
                        item
                        for item in self.policy_events.values()
                        if item["idempotency_key"] == event["idempotency_key"]
                    ),
                    None,
                )
            policy = next(
                (item for item in self.policies.values() if item["version"] == payload["version"]),
                None,
            )
            if repeated_event and (
                not policy
                or repeated_event["policy_id"] != policy["id"]
                or repeated_event["action"] != str(event["action"])
            ):
                raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
            if policy:
                if (
                    policy["policy_sha256"] != payload["policy_sha256"]
                    or policy["schema_version"] != str(payload["schema_version"])
                    or str(policy.get("base_policy_id") or "")
                    != str(payload.get("base_policy_id") or "")
                ):
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_VERSION_CONFLICT.value)
            else:
                base_policy_id = payload.get("base_policy_id")
                if base_policy_id and str(base_policy_id) not in self.policies:
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                if (
                    payload.get("status", ReviewPolicyStatus.DRAFT.value)
                    == ReviewPolicyStatus.ACTIVE.value
                    and payload.get("is_default", True)
                    and any(
                        item["status"] == ReviewPolicyStatus.ACTIVE.value and item["is_default"]
                        for item in self.policies.values()
                    )
                ):
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_ACTIVATION_CONFLICT.value)
                now = _utc_now()
                policy = {
                    "id": str(payload.get("id") or new_domain_id("policy")),
                    "version": str(payload["version"]),
                    "schema_version": str(payload["schema_version"]),
                    "status": str(payload.get("status") or ReviewPolicyStatus.DRAFT.value),
                    "is_default": bool(payload.get("is_default", True)),
                    "policy": dict(payload.get("policy") or {}),
                    "policy_sha256": str(payload["policy_sha256"]),
                    "base_policy_id": base_policy_id,
                    "created_by_user_id": payload.get("created_by_user_id"),
                    "created_by_nickname": str(payload.get("created_by_nickname") or ""),
                    "validation_summary": dict(payload.get("validation_summary") or {}),
                    "validated_at": payload.get("validated_at"),
                    "activated_at": payload.get("activated_at"),
                    "retired_at": payload.get("retired_at"),
                    "created_at": now,
                    "updated_at": now,
                }
                self.policies[policy["id"]] = policy
            if event:
                self._append_review_policy_event_locked(policy["id"], event)
            return deepcopy(policy)

    async def get_review_policy(self, policy_id: str) -> dict[str, Any] | None:
        policy = self.policies.get(policy_id)
        return deepcopy(policy) if policy else None

    async def get_active_review_policy(self) -> dict[str, Any] | None:
        active = [
            policy
            for policy in self.policies.values()
            if policy["status"] == "active" and policy["is_default"]
        ]
        active.sort(
            key=lambda item: (item.get("activated_at") or "", item["created_at"]), reverse=True
        )
        return deepcopy(active[0]) if active else None

    async def list_review_policies(self, limit: int, offset: int) -> list[dict[str, Any]]:
        values = sorted(
            self.policies.values(),
            key=lambda item: (item["created_at"], item["id"]),
            reverse=True,
        )
        return deepcopy(values[offset : offset + limit])

    async def upsert_review_worker_heartbeat(
        self,
        *,
        worker_kind: str,
        worker_id: str,
        components: Mapping[str, Any],
        ttl_seconds: int,
        capacity: int,
        active_count: int,
    ) -> dict[str, Any]:
        heartbeat = normalize_worker_heartbeat(
            worker_kind=worker_kind,
            worker_id=worker_id,
            components=components,
            ttl_seconds=ttl_seconds,
            capacity=capacity,
            active_count=active_count,
        )
        async with self._lock:
            now = datetime.now(UTC)
            retention_cutoff = now - timedelta(days=7)
            for stale_key, stale in list(self.worker_heartbeats.items()):
                if (_as_datetime(stale.get("expires_at")) or now) < retention_cutoff:
                    self.worker_heartbeats.pop(stale_key, None)
            key = (heartbeat["worker_kind"], heartbeat["worker_id"])
            previous = self.worker_heartbeats.get(key)
            row = {
                "worker_kind": heartbeat["worker_kind"],
                "worker_id": heartbeat["worker_id"],
                "components": deepcopy(heartbeat["components"]),
                "capacity": heartbeat["capacity"],
                "active_count": heartbeat["active_count"],
                "observed_at": now,
                "expires_at": now + timedelta(seconds=heartbeat["ttl_seconds"]),
                "created_at": previous["created_at"] if previous else now,
                "updated_at": now,
            }
            self.worker_heartbeats[key] = row
            return _record(row)

    async def list_review_worker_heartbeats(self, limit: int = 100) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        items = [
            {**deepcopy(item), "live": (_as_datetime(item.get("expires_at")) or now) > now}
            for item in self.worker_heartbeats.values()
        ]
        items.sort(
            key=lambda item: (
                str(item["worker_kind"]),
                not bool(item["live"]),
                -(_as_datetime(item["observed_at"]) or now).timestamp(),
                str(item["worker_id"]),
            )
        )
        return [_record(item) for item in items[: max(1, min(int(limit), 100))]]

    async def list_latest_review_tool_runs(self) -> list[dict[str, Any]]:
        latest_runs: dict[str, dict[str, Any]] = {}
        observed_types = {
            "runtime",
            "llm_package",
            "llm_file",
            "llm_summary",
            "clamav",
            "yara",
            "dependency",
        }
        for run in self.runs.values():
            run_type = str(run.get("type") or "")
            if run_type not in observed_types:
                continue
            latest_at = timestamp(run.get("completed_at") or run.get("created_at"))
            current = latest_runs.get(run_type)
            current_at = timestamp(
                (current or {}).get("completed_at") or (current or {}).get("created_at")
            )
            if latest_at and (current_at is None or latest_at > current_at):
                latest_runs[run_type] = deepcopy(run)
        return [deepcopy(item) for _, item in sorted(latest_runs.items())]

    async def get_review_observability_snapshot(self, since: datetime) -> dict[str, Any]:
        normalized_since = since.astimezone(UTC)
        now = datetime.now(UTC)
        queue_counts: dict[tuple[str, str], int] = {}
        for job in self.jobs.values():
            status = str(job.get("status") or "")
            if status in METRIC_JOB_STATUSES:
                key = (str(job.get("type") or ""), status)
                queue_counts[key] = queue_counts.get(key, 0) + 1

        stage_data: dict[str, dict[str, Any]] = {}
        latest_runs: dict[str, dict[str, Any]] = {}
        observed_types = {
            "runtime",
            "llm_package",
            "llm_file",
            "llm_summary",
            "clamav",
            "yara",
            "dependency",
        }
        for run in self.runs.values():
            created = timestamp(run.get("queued_at") or run.get("created_at"))
            run_type = str(run.get("type") or "")
            status = str(run.get("status") or "")
            if run_type in observed_types:
                latest_at = timestamp(run.get("completed_at") or run.get("created_at"))
                current = latest_runs.get(run_type)
                current_at = timestamp(
                    (current or {}).get("completed_at") or (current or {}).get("created_at")
                )
                if latest_at and (current_at is None or latest_at > current_at):
                    latest_runs[run_type] = deepcopy(run)
            if created is None or created < normalized_since or status not in METRIC_RUN_STATUSES:
                continue
            entry = stage_data.setdefault(
                run_type,
                {"sample_count": 0, "failure_count": 0, "timeout_count": 0, "durations": []},
            )
            entry["sample_count"] += 1
            entry["failure_count"] += int(status == "failed")
            entry["timeout_count"] += int(status == "timed_out")
            started = timestamp(run.get("started_at"))
            completed = timestamp(run.get("completed_at"))
            if started and completed and completed >= started:
                entry["durations"].append((completed - started).total_seconds() * 1000)

        waiting = [
            artifact
            for artifact in self.artifacts.values()
            if artifact.get("review_status") == "pending_review"
        ]
        wait_seconds = []
        for artifact in waiting:
            started = timestamp(
                artifact.get("automated_review_completed_at")
                or artifact.get("updated_at")
                or artifact.get("created_at")
            )
            if started:
                wait_seconds.append(max(0.0, (now - started).total_seconds()))

        routing_counts: dict[tuple[str, str], int] = {}
        for decision in self.decisions.values():
            created = timestamp(decision.get("created_at"))
            if created and created >= normalized_since:
                key = (str(decision.get("action") or ""), str(decision.get("source") or ""))
                routing_counts[key] = routing_counts.get(key, 0) + 1

        revoke_counts: dict[str, int] = {}
        for job in self.jobs.values():
            created = timestamp(job.get("created_at"))
            if job.get("type") == "revoke" and created and created >= normalized_since:
                status = str(job.get("status") or "")
                revoke_counts[status] = revoke_counts.get(status, 0) + 1

        stages = []
        for run_type, entry in sorted(stage_data.items()):
            durations = entry.pop("durations")
            stages.append(
                {
                    "type": run_type,
                    **entry,
                    "average_duration_ms": (sum(durations) / len(durations) if durations else 0.0),
                    "p95_duration_ms": percentile_cont(durations, 0.95),
                }
            )
        return {
            "window_started_at": normalized_since,
            "queue": [
                {"type": key[0], "status": key[1], "count": count}
                for key, count in sorted(queue_counts.items())
            ],
            "stages": stages,
            "manual_wait": {
                "waiting_count": len(waiting),
                "average_wait_seconds": (
                    sum(wait_seconds) / len(wait_seconds) if wait_seconds else 0.0
                ),
                "max_wait_seconds": max(wait_seconds, default=0.0),
            },
            "routing": [
                {"action": key[0], "source": key[1], "count": count}
                for key, count in sorted(routing_counts.items())
            ],
            "revoke": [
                {"status": status, "count": count}
                for status, count in sorted(revoke_counts.items())
            ],
            "latest_tool_runs": [deepcopy(item) for _, item in sorted(latest_runs.items())],
        }

    async def append_review_policy_event(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if str(payload["policy_id"]) not in self.policies:
                raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
            event = self._append_review_policy_event_locked(
                str(payload["policy_id"]),
                payload,
            )
            return deepcopy(event)

    async def list_review_policy_events(self, policy_id: str) -> list[dict[str, Any]]:
        events = [event for event in self.policy_events.values() if event["policy_id"] == policy_id]
        events.sort(key=lambda item: (item["created_at"], item["id"]))
        return deepcopy(events)

    async def transition_review_policy(
        self,
        policy_id: str,
        *,
        action: str,
        expected_policy_sha256: str,
        expected_active_policy_id: str | None,
        validation_summary: Mapping[str, Any] | None,
        event: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        normalized_action = ReviewPolicyEventAction(action)
        if str(event.get("action") or "") != normalized_action.value:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
        async with self._lock:
            repeated = next(
                (
                    item
                    for item in self.policy_events.values()
                    if item["idempotency_key"] == event["idempotency_key"]
                ),
                None,
            )
            if repeated:
                self._validate_review_policy_event_identity(
                    repeated,
                    policy_id,
                    normalized_action.value,
                )
                policy = self.policies.get(policy_id)
                return deepcopy(policy) if policy else None

            policy = self.policies.get(policy_id)
            if not policy:
                return None
            self._validate_review_policy_sha(policy, expected_policy_sha256)
            now = _utc_now()
            if normalized_action is ReviewPolicyEventAction.VALIDATE:
                if policy["status"] not in {
                    ReviewPolicyStatus.DRAFT.value,
                    ReviewPolicyStatus.RETIRED.value,
                }:
                    raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
                policy["validation_summary"] = dict(validation_summary or {})
                policy["validated_at"] = now
                policy["updated_at"] = now
            else:
                self._transition_review_policy_state_locked(
                    policy,
                    action=normalized_action,
                    expected_active_policy_id=expected_active_policy_id,
                    event=event,
                    now=now,
                )
            self._append_review_policy_event_locked(policy_id, event)
            return deepcopy(policy)

    def _transition_review_policy_state_locked(
        self,
        policy: dict[str, Any],
        *,
        action: ReviewPolicyEventAction,
        expected_active_policy_id: str | None,
        event: Mapping[str, Any],
        now: str,
    ) -> None:
        if action not in {
            ReviewPolicyEventAction.ACTIVATE,
            ReviewPolicyEventAction.RETIRE,
            ReviewPolicyEventAction.ROLLBACK,
        }:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
        if not policy["is_default"]:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
        active = next(
            (
                item
                for item in self.policies.values()
                if item["status"] == ReviewPolicyStatus.ACTIVE.value and item["is_default"]
            ),
            None,
        )
        active_id = str(active["id"]) if active else None
        if active_id != expected_active_policy_id:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_ACTIVATION_CONFLICT.value)

        if action in {
            ReviewPolicyEventAction.ACTIVATE,
            ReviewPolicyEventAction.ROLLBACK,
        }:
            expected_status = (
                ReviewPolicyStatus.DRAFT
                if action is ReviewPolicyEventAction.ACTIVATE
                else ReviewPolicyStatus.RETIRED
            )
            if policy["status"] != expected_status.value or not _policy_validation_is_current(
                policy
            ):
                raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
            if active and active["id"] != policy["id"]:
                retire_key = f"{event['idempotency_key']}:retire:{active['id']}"
                repeated_retire = next(
                    (
                        item
                        for item in self.policy_events.values()
                        if item["idempotency_key"] == retire_key
                    ),
                    None,
                )
                if repeated_retire:
                    self._validate_review_policy_event_identity(
                        repeated_retire,
                        str(active["id"]),
                        ReviewPolicyEventAction.RETIRE.value,
                    )
                active["status"] = ReviewPolicyStatus.RETIRED.value
                active["retired_at"] = now
                active["updated_at"] = now
                self._append_superseded_policy_event_locked(active, target=policy, event=event)
            policy["status"] = ReviewPolicyStatus.ACTIVE.value
            policy["activated_at"] = now
            policy["retired_at"] = None
            policy["updated_at"] = now
            return

        if policy["status"] not in {
            ReviewPolicyStatus.DRAFT.value,
            ReviewPolicyStatus.ACTIVE.value,
        }:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
        policy["status"] = ReviewPolicyStatus.RETIRED.value
        policy["retired_at"] = now
        policy["updated_at"] = now

    def _append_superseded_policy_event_locked(
        self,
        retired: Mapping[str, Any],
        *,
        target: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> None:
        payload = {
            **dict(event),
            "action": ReviewPolicyEventAction.RETIRE.value,
            "base_version": str(retired["version"]),
            "diff": {
                "redacted": True,
                "superseded_by_policy_sha256": str(target["policy_sha256"]),
            },
            "idempotency_key": f"{event['idempotency_key']}:retire:{retired['id']}",
        }
        self._append_review_policy_event_locked(str(retired["id"]), payload)

    def _append_review_policy_event_locked(
        self,
        policy_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = next(
            (
                event
                for event in self.policy_events.values()
                if event["idempotency_key"] == payload["idempotency_key"]
            ),
            None,
        )
        if existing:
            self._validate_review_policy_event_identity(
                existing,
                policy_id,
                str(payload["action"]),
            )
            return existing
        event = {
            "id": str(payload.get("id") or new_domain_id("policy_event")),
            "policy_id": policy_id,
            "action": str(payload["action"]),
            "actor_user_id": payload.get("actor_user_id"),
            "actor_nickname": str(payload.get("actor_nickname") or ""),
            "reason": str(payload.get("reason") or ""),
            "request_id": str(payload["request_id"]),
            "base_version": str(payload.get("base_version") or ""),
            "diff": deepcopy(dict(payload.get("diff") or {})),
            "idempotency_key": str(payload["idempotency_key"]),
            "created_at": _utc_now(),
        }
        self.policy_events[event["id"]] = event
        return event

    @staticmethod
    def _validate_review_policy_event_identity(
        event: Mapping[str, Any],
        policy_id: str,
        action: str,
    ) -> None:
        if str(event["policy_id"]) != policy_id or str(event["action"]) != action:
            raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)

    @staticmethod
    def _validate_review_policy_sha(
        policy: Mapping[str, Any],
        expected_policy_sha256: str,
    ) -> None:
        if str(policy["policy_sha256"]) != expected_policy_sha256:
            raise ValueError(ArtifactErrorCode.REVIEW_POLICY_VERSION_CONFLICT.value)

    async def bind_artifact_policy(
        self,
        artifact_id: str,
        policy_id: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            artifact = self.artifacts.get(artifact_id)
            if not artifact:
                return None
            if policy_id not in self.policies:
                raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
            current = artifact.get("policy_version_id")
            if current not in {None, policy_id}:
                raise ValueError(ArtifactErrorCode.ARTIFACT_POLICY_SNAPSHOT_CONFLICT.value)
            artifact["policy_version_id"] = policy_id
            artifact["updated_at"] = _utc_now()
            return deepcopy(artifact)

    async def snapshot_active_review_policy(
        self,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            artifact = self.artifacts.get(artifact_id)
            if not artifact:
                return None
            if artifact.get("policy_version_id") is not None:
                return deepcopy(artifact)
            if artifact["review_status"] not in {
                ReviewStatus.QUARANTINED.value,
                ReviewStatus.PRECHECKING.value,
                ReviewStatus.PROCESSING_FAILED.value,
            }:
                raise ValueError(ArtifactErrorCode.ARTIFACT_POLICY_SNAPSHOT_CONFLICT.value)
            policy = next(
                (
                    item
                    for item in self.policies.values()
                    if item["status"] == ReviewPolicyStatus.ACTIVE.value and item["is_default"]
                ),
                None,
            )
            if not policy or not _policy_validation_is_current(policy):
                return deepcopy(artifact)
            artifact["policy_version_id"] = policy["id"]
            artifact["updated_at"] = _utc_now()
            return deepcopy(artifact)

    async def migrate_artifact_policy(
        self,
        artifact_id: str,
        target_policy_id: str,
        *,
        actor: Mapping[str, Any],
        reason: str,
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            existing = next(
                (
                    decision
                    for decision in self.decisions.values()
                    if decision["idempotency_key"] == idempotency_key
                ),
                None,
            )
            if existing:
                if (
                    existing["artifact_id"] != artifact_id
                    or existing["action"] != DecisionAction.POLICY_MIGRATE.value
                    or str(existing.get("policy_version_id") or "") != target_policy_id
                ):
                    raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                artifact = self.artifacts.get(artifact_id)
                return deepcopy(artifact) if artifact else None

            artifact = self.artifacts.get(artifact_id)
            if not artifact:
                return None
            if ReviewStatus(artifact["review_status"]) in TERMINAL_REVIEW_STATUSES:
                raise ValueError(ArtifactErrorCode.ARTIFACT_POLICY_MIGRATION_FORBIDDEN.value)
            target = self.policies.get(target_policy_id)
            if (
                not target
                or target["status"]
                not in {ReviewPolicyStatus.ACTIVE.value, ReviewPolicyStatus.RETIRED.value}
                or not _policy_validation_is_current(target)
            ):
                raise ValueError(ArtifactErrorCode.REVIEW_POLICY_INVALID.value)
            previous_policy_id = str(artifact.get("policy_version_id") or "")
            if previous_policy_id == target_policy_id:
                raise ValueError(ArtifactErrorCode.ARTIFACT_POLICY_SNAPSHOT_CONFLICT.value)
            previous = self.policies.get(previous_policy_id)
            migration = {
                "from_policy_version_id": previous_policy_id or None,
                "from_policy_sha256": str(previous["policy_sha256"]) if previous else "",
                "to_policy_version_id": target_policy_id,
                "to_policy_sha256": str(target["policy_sha256"]),
                "invalidates_automated_review": True,
                "request_id": request_id,
            }
            decision = {
                "id": new_domain_id("decision"),
                "artifact_id": artifact_id,
                "action": DecisionAction.POLICY_MIGRATE.value,
                "from_status": artifact["review_status"],
                "to_status": artifact["review_status"],
                "reason": reason,
                "reviewer_user_id": actor.get("id"),
                "reviewer_nickname": _actor_name(actor),
                "policy_version": target["version"],
                "policy_version_id": target_policy_id,
                "source": "admin",
                "input_run_ids": [],
                "input_fingerprints": [],
                "coverage_sha256": "",
                "metadata": {"policy_migration": deepcopy(migration)},
                "idempotency_key": idempotency_key,
                "created_at": _utc_now(),
            }
            self.decisions[decision["id"]] = decision
            artifact["policy_version_id"] = target_policy_id
            artifact["review_coverage"] = {
                **dict(artifact.get("review_coverage") or {}),
                "policy_migration": migration,
            }
            artifact["automated_review_completed_at"] = None
            artifact["updated_at"] = _utc_now()
            return deepcopy(artifact)

    async def update_artifact_review_coverage(
        self,
        artifact_id: str,
        coverage: Mapping[str, Any],
        *,
        automated_review_completed: bool = False,
    ) -> dict[str, Any] | None:
        artifact = self.artifacts.get(artifact_id)
        if not artifact:
            return None
        artifact["review_coverage"] = dict(coverage)
        if automated_review_completed:
            artifact["automated_review_completed_at"] = _utc_now()
        artifact["updated_at"] = _utc_now()
        return deepcopy(artifact)

    async def replace_artifact_diffs(
        self,
        artifact_id: str,
        base_artifact_id: str | None,
        *,
        current_tree_sha256: str,
        base_tree_sha256: str | None,
        diffs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        self._validate_memory_diff_trees(
            artifact_id,
            base_artifact_id,
            current_tree_sha256,
            base_tree_sha256,
        )
        now = _utc_now()
        saved = [
            {
                **dict(item),
                "id": str(item.get("id") or new_domain_id("diff")),
                "artifact_id": artifact_id,
                "base_artifact_id": base_artifact_id,
                "base_tree_sha256": base_tree_sha256,
                "current_tree_sha256": current_tree_sha256,
                "stats": dict(item.get("stats") or {}),
                "created_at": now,
            }
            for item in diffs
        ]
        self.diffs[artifact_id] = saved
        return deepcopy(sorted(saved, key=lambda item: item["path"]))

    async def list_artifact_diffs(self, artifact_id: str) -> list[dict[str, Any]]:
        files_by_id = {str(file["id"]): file for files in self.files.values() for file in files}
        values = []
        for item in self.diffs.get(artifact_id, []):
            base = files_by_id.get(str(item.get("base_file_id") or "")) or {}
            current = files_by_id.get(str(item.get("current_file_id") or "")) or {}
            values.append(
                {
                    **item,
                    "resolved_base_path": base.get("path"),
                    "resolved_current_path": current.get("path"),
                }
            )
        return deepcopy(sorted(values, key=lambda item: item["path"]))

    async def get_artifact_diff(self, artifact_id: str, diff_id: str) -> dict[str, Any] | None:
        item = next(
            (row for row in self.diffs.get(artifact_id, []) if str(row.get("id") or "") == diff_id),
            None,
        )
        if item is None:
            return None
        files_by_id = {str(file["id"]): file for files in self.files.values() for file in files}
        base = files_by_id.get(str(item.get("base_file_id") or "")) or {}
        current = files_by_id.get(str(item.get("current_file_id") or "")) or {}
        return deepcopy(
            {
                **item,
                "resolved_base_path": base.get("path"),
                "resolved_current_path": current.get("path"),
            }
        )

    async def replace_dependency_edges(
        self,
        artifact_id: str,
        *,
        tree_sha256: str,
        edges: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        artifact = self.artifacts.get(artifact_id)
        if not artifact:
            return []
        if artifact["tree_sha256"] != tree_sha256:
            raise ValueError(ArtifactErrorCode.DIFF_TREE_CHANGED.value)
        now = _utc_now()
        saved = [
            {
                **dict(item),
                "id": str(item.get("id") or new_domain_id("edge")),
                "artifact_id": artifact_id,
                "metadata": dict(item.get("metadata") or {}),
                "created_at": now,
            }
            for item in edges
        ]
        self.dependency_edges[artifact_id] = saved
        return deepcopy(saved)

    async def list_dependency_edges(self, artifact_id: str) -> list[dict[str, Any]]:
        file_by_id = {str(item["id"]): item for item in self.files.get(artifact_id, [])}
        values = [
            {
                **edge,
                "source_path": (file_by_id.get(str(edge["source_file_id"])) or {}).get("path"),
                "target_path": (file_by_id.get(str(edge.get("target_file_id") or "")) or {}).get(
                    "path"
                ),
            }
            for edge in self.dependency_edges.get(artifact_id, [])
        ]
        values.sort(
            key=lambda item: (
                str(item.get("source_path") or ""),
                int(item.get("line_start") or 0),
                item["id"],
            )
        )
        return deepcopy(values)

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
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        async with self._lock:
            artifact = self.artifacts.get(artifact_id)
            if not artifact:
                return [], []
            if artifact["tree_sha256"] != tree_sha256:
                raise ValueError(ArtifactErrorCode.DIFF_TREE_CHANGED.value)
            if base_artifact_id is None:
                if base_tree_sha256 is not None:
                    raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)
            else:
                declared_base_id = str(artifact.get("base_artifact_id") or "") or None
                if base_artifact_id == artifact_id or base_artifact_id != declared_base_id:
                    raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)
                base = self.artifacts.get(base_artifact_id)
                if (
                    base is None
                    or base["plugin_id"] != artifact["plugin_id"]
                    or base["tree_sha256"] != base_tree_sha256
                ):
                    raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)
            file_by_id = {str(item["id"]): item for item in self.files.get(artifact_id, [])}
            _validate_graph_projection(files, edges, set(file_by_id))
            for item in files:
                target = file_by_id[str(item["file_id"])]
                target.update(
                    {
                        "is_entrypoint": bool(item.get("is_entrypoint")),
                        "is_reachable": bool(item.get("is_reachable")),
                        "graph_status": str(item.get("graph_status") or "not_analyzed"),
                        "scan_summary": dict(item.get("scan_summary") or {}),
                    }
                )
            now = _utc_now()
            saved_edges = [
                {
                    **dict(item),
                    "id": str(item.get("id") or new_domain_id("edge")),
                    "artifact_id": artifact_id,
                    "metadata": dict(item.get("metadata") or {}),
                    "created_at": now,
                }
                for item in edges
            ]
            self.dependency_edges[artifact_id] = saved_edges
            artifact["review_coverage"] = {
                **dict(artifact.get("review_coverage") or {}),
                "import_graph": dict(coverage),
            }
            artifact["updated_at"] = now
            updated_files = sorted(
                file_by_id.values(), key=lambda item: str(item.get("path") or "")
            )
            return deepcopy(updated_files), deepcopy(saved_edges)

    async def create_runtime_dispatch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            run = self.runs.get(str(payload["run_id"]))
            if not run or run["artifact_id"] != payload["artifact_id"] or run["type"] != "runtime":
                raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
            for dispatch in self.dispatches.values():
                if dispatch["run_id"] == payload["run_id"] and dispatch["status"] != "cancelled":
                    if (
                        dispatch["artifact_id"] != payload["artifact_id"]
                        or dispatch["request_sha256"] != payload["request_sha256"]
                    ):
                        raise ValueError(ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value)
                    return deepcopy(dispatch)
            now = _utc_now()
            dispatch = {
                "id": str(payload.get("id") or new_domain_id("dispatch")),
                "artifact_id": str(payload["artifact_id"]),
                "run_id": str(payload["run_id"]),
                "status": str(payload.get("status") or "queued"),
                "request": dict(payload.get("request") or {}),
                "request_sha256": str(payload["request_sha256"]),
                "result_key": payload.get("result_key"),
                "result_sha256": payload.get("result_sha256"),
                "runner_id": str(payload.get("runner_id") or ""),
                "image_digest": str(payload.get("image_digest") or ""),
                "lease_owner": None,
                "lease_expires_at": None,
                "attempts": int(payload.get("attempts") or 0),
                "max_attempts": int(payload.get("max_attempts") or 3),
                "collected_at": None,
                "error_code": str(payload.get("error_code") or ""),
                "error_message": str(payload.get("error_message") or ""),
                "queued_at": now,
                "started_at": None,
                "completed_at": None,
                "updated_at": now,
            }
            self.dispatches[dispatch["id"]] = dispatch
            return deepcopy(dispatch)

    async def get_runtime_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        dispatch = self.dispatches.get(dispatch_id)
        return deepcopy(dispatch) if dispatch else None

    async def claim_runtime_dispatches(
        self,
        runner_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            now = datetime.now(UTC)
            candidates = []
            for dispatch in self.dispatches.values():
                expired = dispatch["status"] == "running" and (
                    not dispatch.get("lease_expires_at")
                    or _parse_time(dispatch["lease_expires_at"]) < now
                )
                if dispatch["attempts"] < dispatch["max_attempts"] and (
                    dispatch["status"] == "queued" or expired
                ):
                    candidates.append(dispatch)
            candidates.sort(key=lambda item: (item["queued_at"], item["id"]))
            claimed = candidates[:limit]
            for dispatch in claimed:
                dispatch["status"] = "running"
                dispatch["attempts"] += 1
                dispatch["runner_id"] = runner_id
                dispatch["lease_owner"] = runner_id
                dispatch["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
                dispatch["started_at"] = dispatch.get("started_at") or _utc_now()
                dispatch["updated_at"] = _utc_now()
            return deepcopy(claimed)

    async def renew_runtime_dispatch_lease(
        self,
        dispatch_id: str,
        runner_id: str,
        lease_seconds: int,
    ) -> bool:
        dispatch = self.dispatches.get(dispatch_id)
        if (
            not dispatch
            or dispatch["status"] != "running"
            or dispatch.get("lease_owner") != runner_id
        ):
            return False
        dispatch["lease_expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=lease_seconds)
        ).isoformat()
        dispatch["updated_at"] = _utc_now()
        return True

    async def complete_runtime_dispatch(
        self,
        dispatch_id: str,
        runner_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        status = _validate_runtime_completion_payload(payload)
        dispatch = self.dispatches.get(dispatch_id)
        if (
            not dispatch
            or dispatch["status"] != "running"
            or dispatch.get("lease_owner") != runner_id
        ):
            return None
        dispatch.update(
            {
                "status": status.value,
                "result_key": payload.get("result_key"),
                "result_sha256": payload.get("result_sha256"),
                "image_digest": str(
                    payload.get("image_digest") or dispatch.get("image_digest") or ""
                ),
                "error_code": str(payload.get("error_code") or ""),
                "error_message": str(payload.get("error_message") or ""),
                "lease_owner": None,
                "lease_expires_at": None,
                "completed_at": _utc_now(),
                "updated_at": _utc_now(),
            }
        )
        return deepcopy(dispatch)

    async def expire_runtime_dispatches(self, limit: int) -> list[dict[str, Any]]:
        async with self._lock:
            now = datetime.now(UTC)
            expired = [
                dispatch
                for dispatch in self.dispatches.values()
                if dispatch["status"] == "running"
                and dispatch["attempts"] >= dispatch["max_attempts"]
                and dispatch.get("lease_expires_at")
                and _parse_time(dispatch["lease_expires_at"]) < now
            ]
            expired.sort(key=lambda item: (item["lease_expires_at"], item["id"]))
            for dispatch in expired[:limit]:
                dispatch.update(
                    {
                        "status": "timed_out",
                        "error_code": "runtime_dispatch_timeout",
                        "error_message": "Runtime runner exhausted all lease attempts",
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "completed_at": _utc_now(),
                        "updated_at": _utc_now(),
                    }
                )
            return deepcopy(expired[:limit])

    async def cancel_runtime_dispatch(
        self,
        dispatch_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            dispatch = self.dispatches.get(dispatch_id)
            if not dispatch or dispatch["status"] not in {"queued", "running"}:
                return None
            dispatch.update(
                {
                    "status": "cancelled",
                    "error_code": error_code,
                    "error_message": error_message,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "completed_at": _utc_now(),
                    "updated_at": _utc_now(),
                }
            )
            return deepcopy(dispatch)

    async def collect_runtime_dispatch(
        self,
        dispatch_id: str,
        run_payload: Mapping[str, Any] | None = None,
        findings: Sequence[Mapping[str, Any]] = (),
        sbom_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        async with self._lock:
            dispatch = self.dispatches.get(dispatch_id)
            if (
                not dispatch
                or dispatch.get("collected_at")
                or dispatch["status"] not in {"succeeded", "failed", "timed_out", "cancelled"}
            ):
                return None
            if run_payload is not None:
                status = str(run_payload["status"])
                if status not in {"succeeded", "failed", "timed_out", "cancelled"}:
                    raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                run = self.runs.get(str(dispatch["run_id"]))
                if (
                    not run
                    or run["type"] != "runtime"
                    or run["status"] not in {"queued", "running"}
                ):
                    raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                run.update(
                    {
                        "status": status,
                        "summary": str(run_payload.get("summary") or ""),
                        "raw_result": dict(run_payload.get("raw_result") or {}),
                        "raw_result_key": run_payload.get("raw_result_key"),
                        "error_code": str(run_payload.get("error_code") or ""),
                        "output_sha256": str(
                            run_payload.get("output_sha256") or run.get("output_sha256") or ""
                        ),
                        "coverage": dict(run_payload.get("coverage") or {}),
                        "container_image_digest": str(
                            run_payload.get("container_image_digest")
                            or run.get("container_image_digest")
                            or ""
                        ),
                        "dependency_snapshot_sha256": str(
                            run_payload.get("dependency_snapshot_sha256")
                            or run.get("dependency_snapshot_sha256")
                            or ""
                        ),
                        "worker_id": str(
                            run_payload.get("worker_id") or run.get("worker_id") or ""
                        ),
                        "completed_at": _utc_now(),
                    }
                )
                if findings:
                    await self.replace_findings(
                        str(dispatch["artifact_id"]),
                        str(dispatch["run_id"]),
                        findings,
                    )
                if sbom_payload is not None:
                    if str(sbom_payload.get("artifact_id") or "") != str(
                        dispatch["artifact_id"]
                    ) or str(sbom_payload.get("run_id") or "") != str(dispatch["run_id"]):
                        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                    await self.create_artifact_sbom(sbom_payload)
            dispatch["collected_at"] = _utc_now()
            dispatch["updated_at"] = _utc_now()
            return deepcopy(dispatch)

    async def create_review_comment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            artifact = self.artifacts.get(str(payload["artifact_id"]))
            if artifact is None:
                raise ValueError(ArtifactErrorCode.COMMENT_LINE_INVALID.value)
            file_artifact_id = str(payload["artifact_id"])
            if str(payload.get("side") or "") == "base":
                file_artifact_id = str(artifact.get("base_artifact_id") or "")
            if payload.get("file_id") and not any(
                file["id"] == payload["file_id"] for file in self.files.get(file_artifact_id, [])
            ):
                raise ValueError(ArtifactErrorCode.COMMENT_LINE_INVALID.value)
            for thread in self.review_comments.values():
                if thread["idempotency_key"] == payload["idempotency_key"]:
                    create_event = next(
                        (
                            event
                            for event in self.comment_events.values()
                            if event["thread_id"] == thread["id"] and event["type"] == "create"
                        ),
                        None,
                    )
                    if not _same_comment_creation(thread, create_event, payload):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    return deepcopy(thread)
            if ReviewStatus(str(artifact["review_status"])) in TERMINAL_REVIEW_STATUSES:
                raise ValueError(ArtifactErrorCode.COMMENT_THREAD_LOCKED.value)
            now = _utc_now()
            thread = {
                "id": str(payload.get("id") or new_domain_id("comment")),
                "artifact_id": str(payload["artifact_id"]),
                "source_thread_id": payload.get("source_thread_id"),
                "file_id": payload.get("file_id"),
                "file_path": str(payload["file_path"]),
                "file_sha256": str(payload["file_sha256"]),
                "side": str(payload["side"]),
                "line_start": int(payload["line_start"]),
                "line_end": int(payload["line_end"]),
                "body": str(payload["body"]),
                "reviewer_user_id": payload.get("reviewer_user_id"),
                "reviewer_nickname": str(payload.get("reviewer_nickname") or ""),
                "reviewer_role": str(payload["reviewer_role"]),
                "resolved": False,
                "resolved_by_user_id": None,
                "resolved_by_nickname": "",
                "locked_at": None,
                "version": 1,
                "idempotency_key": str(payload["idempotency_key"]),
                "created_at": now,
                "updated_at": now,
                "resolved_at": None,
            }
            event = {
                "id": new_domain_id("comment_event"),
                "thread_id": thread["id"],
                "artifact_id": thread["artifact_id"],
                "type": "create",
                "body": thread["body"],
                "actor_user_id": thread["reviewer_user_id"],
                "actor_nickname": thread["reviewer_nickname"],
                "actor_role": thread["reviewer_role"],
                "expected_version": 0,
                "resulting_version": 1,
                "metadata": dict(payload.get("metadata") or {}),
                "idempotency_key": str(
                    payload.get("event_idempotency_key") or f"{payload['idempotency_key']}:create"
                ),
                "created_at": now,
            }
            self.review_comments[thread["id"]] = thread
            self.comment_events[event["id"]] = event
            return deepcopy(thread)

    async def append_review_comment_event(
        self,
        thread_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        event_type = ReviewCommentEventType(str(payload["type"]))
        if event_type is ReviewCommentEventType.CREATE:
            raise ValueError(ArtifactErrorCode.COMMENT_VERSION_CONFLICT.value)
        async with self._lock:
            for event in self.comment_events.values():
                if event["idempotency_key"] == payload["idempotency_key"]:
                    if not _same_comment_event(event, thread_id, payload):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    thread = self.review_comments.get(thread_id)
                    return deepcopy(thread) if thread else None
            thread = self.review_comments.get(thread_id)
            if not thread:
                return None
            artifact = self.artifacts.get(str(thread["artifact_id"]))
            if (
                artifact is None
                or ReviewStatus(str(artifact["review_status"])) in TERMINAL_REVIEW_STATUSES
                or thread.get("locked_at")
            ):
                raise ValueError(ArtifactErrorCode.COMMENT_THREAD_LOCKED.value)
            expected_version = int(payload["expected_version"])
            if int(thread["version"]) != expected_version:
                raise ValueError(ArtifactErrorCode.COMMENT_VERSION_CONFLICT.value)
            if event_type is ReviewCommentEventType.EDIT:
                thread["body"] = str(payload.get("body") or "")
            elif event_type is ReviewCommentEventType.RESOLVE:
                thread["resolved"] = True
                thread["resolved_by_user_id"] = payload.get("actor_user_id")
                thread["resolved_by_nickname"] = str(payload.get("actor_nickname") or "")
                thread["resolved_at"] = _utc_now()
            elif event_type is ReviewCommentEventType.REOPEN:
                thread["resolved"] = False
                thread["resolved_by_user_id"] = None
                thread["resolved_by_nickname"] = ""
                thread["resolved_at"] = None
            thread["version"] = expected_version + 1
            thread["updated_at"] = _utc_now()
            event = {
                **dict(payload),
                "id": str(payload.get("id") or new_domain_id("comment_event")),
                "thread_id": thread_id,
                "artifact_id": thread["artifact_id"],
                "type": event_type.value,
                "expected_version": expected_version,
                "resulting_version": thread["version"],
                "metadata": dict(payload.get("metadata") or {}),
                "created_at": _utc_now(),
            }
            self.comment_events[event["id"]] = event
            return deepcopy(thread)

    async def get_review_comment(
        self,
        artifact_id: str,
        thread_id: str,
        *,
        event_limit: int = 20,
    ) -> dict[str, Any] | None:
        thread = self.review_comments.get(thread_id)
        if not thread or thread["artifact_id"] != artifact_id:
            return None
        all_events = sorted(
            (
                deepcopy(event)
                for event in self.comment_events.values()
                if event["artifact_id"] == artifact_id and event["thread_id"] == thread_id
            ),
            key=lambda item: (item["created_at"], item["id"]),
        )
        events = all_events[-event_limit:]
        return {
            **deepcopy(thread),
            "events": events,
            "event_count": len(all_events),
            "events_truncated": len(events) < len(all_events),
        }

    async def list_review_comments(
        self,
        artifact_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        event_limit: int = 20,
    ) -> list[dict[str, Any]]:
        events_by_thread: dict[str, list[dict[str, Any]]] = {}
        for event in sorted(
            self.comment_events.values(),
            key=lambda item: (item["created_at"], item["id"]),
        ):
            if event["artifact_id"] == artifact_id:
                events_by_thread.setdefault(event["thread_id"], []).append(deepcopy(event))
        threads = [
            {
                **deepcopy(thread),
                "events": events_by_thread.get(thread["id"], [])[-event_limit:],
                "event_count": len(events_by_thread.get(thread["id"], [])),
                "events_truncated": len(events_by_thread.get(thread["id"], [])) > event_limit,
            }
            for thread in self.review_comments.values()
            if thread["artifact_id"] == artifact_id
        ]
        threads.sort(
            key=lambda item: (
                item["file_path"],
                item["line_start"],
                item["created_at"],
                item["id"],
            )
        )
        return threads[offset : offset + limit]

    async def count_review_comments(self, artifact_id: str) -> int:
        return sum(
            1 for thread in self.review_comments.values() if thread["artifact_id"] == artifact_id
        )

    async def lock_review_comments(self, artifact_id: str) -> int:
        changed = 0
        now = _utc_now()
        for thread in self.review_comments.values():
            if thread["artifact_id"] == artifact_id and not thread.get("locked_at"):
                thread["locked_at"] = now
                thread["updated_at"] = now
                changed += 1
        return changed

    async def update_finding_state(
        self,
        finding_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        async with self._lock:
            for event in self.finding_events.values():
                if event["idempotency_key"] == payload["idempotency_key"]:
                    if not _same_finding_event_request(event, finding_id, payload):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    finding = self._memory_finding(finding_id)
                    return deepcopy(finding) if finding else None
            finding = self._memory_finding(finding_id)
            if not finding:
                return None
            if int(finding.get("version") or 1) != int(payload["expected_version"]):
                raise ValueError(ArtifactErrorCode.FINDING_VERSION_CONFLICT.value)
            old = deepcopy(finding)
            status = str(payload.get("status") or finding.get("status") or "open")
            FindingStatus(status)
            finding["status"] = status
            if "correlation" in payload:
                finding["correlation"] = dict(payload["correlation"])
            if "affects_current_release" in payload:
                finding["affects_current_release"] = bool(payload["affects_current_release"])
            finding["status_actor_user_id"] = payload.get("actor_user_id")
            finding["status_actor_nickname"] = str(payload.get("actor_nickname") or "")
            finding["status_updated_at"] = _utc_now()
            finding["version"] = int(finding.get("version") or 1) + 1
            event = {
                "id": str(payload.get("id") or new_domain_id("finding_event")),
                "finding_id": finding_id,
                "artifact_id": finding["artifact_id"],
                "type": _finding_event_type(old, status, bool(finding["affects_current_release"])),
                "from_status": old.get("status"),
                "to_status": status,
                "actor_user_id": payload.get("actor_user_id"),
                "actor_nickname": str(payload.get("actor_nickname") or ""),
                "actor_source": str(payload.get("actor_source") or "user"),
                "reason": str(payload.get("reason") or ""),
                "metadata": dict(payload.get("metadata") or {}),
                "idempotency_key": str(payload["idempotency_key"]),
                "created_at": _utc_now(),
            }
            self.finding_events[event["id"]] = event
            return deepcopy(finding)

    async def list_finding_events(self, artifact_id: str) -> list[dict[str, Any]]:
        values = [
            event for event in self.finding_events.values() if event["artifact_id"] == artifact_id
        ]
        values.sort(key=lambda item: (item["created_at"], item["id"]))
        return deepcopy(values)

    async def get_review_finding(self, artifact_id: str, finding_id: str) -> dict[str, Any] | None:
        finding = self._memory_finding(finding_id)
        if not finding or finding["artifact_id"] != artifact_id:
            return None
        return deepcopy(finding)

    async def create_artifact_sbom(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run = self.runs.get(str(payload["run_id"]))
        if not run or run["artifact_id"] != payload["artifact_id"]:
            raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
        for sbom in self.sboms.values():
            if (
                sbom["artifact_id"] == payload["artifact_id"]
                and sbom["run_id"] == payload["run_id"]
                and sbom["format"] == payload["format"]
                and sbom["document_sha256"] == payload["document_sha256"]
            ):
                if sbom["object_key"] != payload["object_key"]:
                    raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
                return deepcopy(sbom)
        sbom = {
            **dict(payload),
            "id": str(payload.get("id") or new_domain_id("sbom")),
            "package_count": int(payload.get("package_count") or 0),
            "tool_version": str(payload.get("tool_version") or ""),
            "created_at": _utc_now(),
        }
        self.sboms[sbom["id"]] = sbom
        return deepcopy(sbom)

    async def list_artifact_sboms(self, artifact_id: str) -> list[dict[str, Any]]:
        values = [sbom for sbom in self.sboms.values() if sbom["artifact_id"] == artifact_id]
        values.sort(key=lambda item: (item["created_at"], item["id"]))
        return deepcopy(values)

    async def list_review_history_records(
        self,
        artifact_id: str,
        *,
        limit: int,
        after: tuple[datetime, str, str] | None,
    ) -> list[dict[str, Any]]:
        artifact = self.artifacts.get(artifact_id)
        if not artifact:
            return []
        records: list[dict[str, Any]] = [
            _history_record(
                record_id=f"{artifact_id}:submitted",
                event_type="artifact_submitted",
                occurred_at=artifact["created_at"],
                source="user",
                actor_nickname=str(
                    (artifact.get("submitted_by_snapshot") or {}).get("nickname") or ""
                ),
                actor_role="author",
                idempotency_key="",
                policy_version_id=artifact.get("policy_version_id"),
                payload={
                    "source_type": artifact.get("source_type", ""),
                    "source_ref": artifact.get("source_ref", ""),
                    "source_commit_sha": artifact.get("source_commit_sha", ""),
                },
            )
        ]
        for run in self.runs.values():
            if run["artifact_id"] != artifact_id:
                continue
            records.append(
                _history_record(
                    record_id=run["id"],
                    event_type="run",
                    occurred_at=run["created_at"],
                    source="system",
                    actor_nickname="",
                    actor_role="system",
                    idempotency_key=str(run.get("idempotency_key") or ""),
                    policy_version_id=run.get("policy_version_id"),
                    payload={
                        "run_type": run.get("type", ""),
                        "status": run.get("status", ""),
                        "attempt": run.get("attempt", 1),
                        "summary": str(run.get("summary") or "")[:2000],
                        "error_code": run.get("error_code", ""),
                        "tool_name": run.get("tool_name", ""),
                        "tool_version": run.get("tool_version", ""),
                        "ruleset_version": run.get("ruleset_version", ""),
                        "model": run.get("model", ""),
                        "coverage": deepcopy(run.get("coverage") or {}),
                    },
                )
            )
        for finding in (
            item
            for findings in self.findings.values()
            for item in findings
            if item["artifact_id"] == artifact_id
        ):
            run = self.runs.get(str(finding.get("run_id") or "")) or {}
            records.append(
                _history_record(
                    record_id=finding["id"],
                    event_type="finding",
                    occurred_at=finding["created_at"],
                    source=str(finding.get("source") or ""),
                    actor_nickname="",
                    actor_role="system",
                    idempotency_key="",
                    policy_version_id=run.get("policy_version_id"),
                    payload={
                        "finding_id": finding["id"],
                        "run_id": finding.get("run_id"),
                        "fingerprint": finding.get("fingerprint", ""),
                        "rule_id": finding.get("rule_id", ""),
                        "severity": finding.get("severity", ""),
                        "category": finding.get("category", ""),
                        "message": str(finding.get("message") or "")[:2000],
                        "file_path": finding.get("file_path", ""),
                        "line_start": finding.get("line_start"),
                        "line_end": finding.get("line_end"),
                        "status": finding.get("status", "open"),
                        "deterministic": bool(finding.get("deterministic")),
                        "affects_current_release": bool(finding.get("affects_current_release")),
                        "correlation": deepcopy(finding.get("correlation") or {}),
                    },
                )
            )
        for event in self.finding_events.values():
            if event["artifact_id"] != artifact_id:
                continue
            finding = self._memory_finding(str(event["finding_id"])) or {}
            run = self.runs.get(str(finding.get("run_id") or "")) or {}
            records.append(
                _history_record(
                    record_id=event["id"],
                    event_type="finding_event",
                    occurred_at=event["created_at"],
                    source=str(event.get("actor_source") or ""),
                    actor_nickname=str(event.get("actor_nickname") or ""),
                    actor_role="admin" if event.get("actor_source") == "user" else "system",
                    idempotency_key=str(event.get("idempotency_key") or ""),
                    policy_version_id=run.get("policy_version_id"),
                    payload={
                        "finding_id": event.get("finding_id"),
                        "event_type": event.get("type", ""),
                        "from_status": event.get("from_status"),
                        "to_status": event.get("to_status"),
                        "reason": str(event.get("reason") or "")[:2000],
                        "metadata": deepcopy(event.get("metadata") or {}),
                    },
                )
            )
        for event in self.comment_events.values():
            if event["artifact_id"] != artifact_id:
                continue
            records.append(
                _history_record(
                    record_id=event["id"],
                    event_type="comment_event",
                    occurred_at=event["created_at"],
                    source="system" if event.get("actor_role") == "system" else "user",
                    actor_nickname=str(event.get("actor_nickname") or ""),
                    actor_role=str(event.get("actor_role") or ""),
                    idempotency_key=str(event.get("idempotency_key") or ""),
                    policy_version_id=artifact.get("policy_version_id"),
                    payload={
                        "thread_id": event.get("thread_id"),
                        "event_type": event.get("type", ""),
                        "body_preview": str(event.get("body") or "")[:500],
                        "expected_version": event.get("expected_version", 0),
                        "resulting_version": event.get("resulting_version", 1),
                    },
                )
            )
        for decision in self.decisions.values():
            if decision["artifact_id"] != artifact_id:
                continue
            records.append(
                _history_record(
                    record_id=decision["id"],
                    event_type="decision",
                    occurred_at=decision["created_at"],
                    source=str(decision.get("source") or "admin"),
                    actor_nickname=str(decision.get("reviewer_nickname") or ""),
                    actor_role="admin" if decision.get("reviewer_user_id") else "system",
                    idempotency_key=str(decision.get("idempotency_key") or ""),
                    policy_version_id=decision.get("policy_version_id"),
                    payload={
                        "action": decision.get("action", ""),
                        "from_status": decision.get("from_status", ""),
                        "to_status": decision.get("to_status", ""),
                        "reason": str(decision.get("reason") or "")[:2000],
                        "policy_version": decision.get("policy_version", ""),
                        "input_run_ids": list(decision.get("input_run_ids") or []),
                        "input_fingerprints": list(decision.get("input_fingerprints") or []),
                        "coverage_sha256": decision.get("coverage_sha256", ""),
                        "metadata": deepcopy(decision.get("metadata") or {}),
                    },
                )
            )
        policy_id = str(artifact.get("policy_version_id") or "")
        for event in self.policy_events.values():
            if event["policy_id"] != policy_id:
                continue
            records.append(
                _history_record(
                    record_id=event["id"],
                    event_type="policy_event",
                    occurred_at=event["created_at"],
                    source="user",
                    actor_nickname=str(event.get("actor_nickname") or ""),
                    actor_role="core_admin",
                    idempotency_key=str(event.get("idempotency_key") or ""),
                    policy_version_id=policy_id,
                    payload={
                        "action": event.get("action", ""),
                        "reason": str(event.get("reason") or "")[:2000],
                        "request_id": event.get("request_id", ""),
                        "base_version": event.get("base_version", ""),
                    },
                )
            )
        if artifact.get("published_at"):
            records.append(
                _history_record(
                    record_id=f"{artifact_id}:published",
                    event_type="publication_published",
                    occurred_at=artifact["published_at"],
                    source="system",
                    actor_nickname="",
                    actor_role="system",
                    idempotency_key="",
                    policy_version_id=artifact.get("policy_version_id"),
                    payload={"publication_status": "published"},
                )
            )
        if artifact.get("revoked_at"):
            records.append(
                _history_record(
                    record_id=f"{artifact_id}:revoked",
                    event_type="publication_revoked",
                    occurred_at=artifact["revoked_at"],
                    source="system",
                    actor_nickname="",
                    actor_role="system",
                    idempotency_key="",
                    policy_version_id=artifact.get("policy_version_id"),
                    payload={"publication_status": "revoked"},
                )
            )
        for event in self.outbox.values():
            if (
                event.get("aggregate_type") != "artifact"
                or event.get("aggregate_id") != artifact_id
                or event.get("event_type")
                not in {"artifact_publish_failed", "artifact_revoke_failed"}
            ):
                continue
            status = (
                "publish_failed"
                if event["event_type"] == "artifact_publish_failed"
                else "revoke_failed"
            )
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            records.append(
                _history_record(
                    record_id=event["id"],
                    event_type=f"publication_{status}",
                    occurred_at=event["created_at"],
                    source="system",
                    actor_nickname="",
                    actor_role="system",
                    idempotency_key=str(event.get("dedupe_key") or ""),
                    policy_version_id=artifact.get("policy_version_id"),
                    payload={
                        key: value
                        for key, value in {
                            "publication_status": status,
                            "code": payload.get("code"),
                        }.items()
                        if value not in {None, ""}
                    },
                )
            )
        records.sort(key=_history_sort_key)
        if after is not None:
            after_key = (after[0], after[1], after[2])
            records = [item for item in records if _history_sort_key(item) > after_key]
        return deepcopy(records[:limit])

    async def get_review_history_sources(self, artifact_id: str) -> dict[str, Any]:
        artifact = self.artifacts.get(artifact_id)
        policy_id = str((artifact or {}).get("policy_version_id") or "")
        return {
            "artifact": deepcopy(artifact) if artifact else None,
            "runs": deepcopy(
                sorted(
                    (run for run in self.runs.values() if run["artifact_id"] == artifact_id),
                    key=lambda item: (item["created_at"], item["id"]),
                )
            ),
            "finding_events": await self.list_finding_events(artifact_id),
            "comment_events": deepcopy(
                sorted(
                    (
                        event
                        for event in self.comment_events.values()
                        if event["artifact_id"] == artifact_id
                    ),
                    key=lambda item: (item["created_at"], item["id"]),
                )
            ),
            "decisions": deepcopy(
                sorted(
                    (
                        decision
                        for decision in self.decisions.values()
                        if decision["artifact_id"] == artifact_id
                    ),
                    key=lambda item: (item["created_at"], item["id"]),
                )
            ),
            "policy_events": deepcopy(
                sorted(
                    (
                        event
                        for event in self.policy_events.values()
                        if event["policy_id"] == policy_id
                    ),
                    key=lambda item: (item["created_at"], item["id"]),
                )
            ),
        }

    def _validate_memory_diff_trees(
        self,
        artifact_id: str,
        base_artifact_id: str | None,
        current_tree_sha256: str,
        base_tree_sha256: str | None,
    ) -> None:
        current = self.artifacts.get(artifact_id)
        if not current or current["tree_sha256"] != current_tree_sha256:
            raise ValueError(ArtifactErrorCode.DIFF_TREE_CHANGED.value)
        if base_artifact_id is None:
            if base_tree_sha256 is not None:
                raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)
            return
        base = self.artifacts.get(base_artifact_id)
        if (
            not base
            or base["plugin_id"] != current["plugin_id"]
            or base["tree_sha256"] != base_tree_sha256
        ):
            raise ValueError(ArtifactErrorCode.DIFF_BASE_INVALID.value)

    def _memory_finding(self, finding_id: str) -> dict[str, Any] | None:
        for findings in self.findings.values():
            for finding in findings:
                if finding["id"] == finding_id:
                    return finding
        return None


def _history_record(
    *,
    record_id: str,
    event_type: str,
    occurred_at: str | datetime,
    source: str,
    actor_nickname: str,
    actor_role: str,
    idempotency_key: str,
    policy_version_id: Any,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "id": str(record_id),
        "type": event_type,
        "occurred_at": occurred_at,
        "source": source,
        "actor_nickname": actor_nickname,
        "actor_role": actor_role,
        "idempotency_key": idempotency_key,
        "policy_version_id": str(policy_version_id or "") or None,
        "payload": dict(payload),
    }


def _history_sort_key(item: Mapping[str, Any]) -> tuple[datetime, str, str]:
    return (
        _parse_time(item["occurred_at"]),
        str(item["type"]),
        str(item["id"]),
    )


def _finding_event_type(
    current: Mapping[str, Any],
    target_status: str,
    affects_current_release: bool,
) -> str:
    if str(current.get("status") or "") != target_status:
        return "status_changed"
    if not bool(current.get("affects_current_release")) and affects_current_release:
        return "current_release_linked"
    return "correlation_changed"


def _validate_graph_projection(
    files: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    registered_file_ids: set[str],
) -> None:
    file_ids = [str(item.get("file_id") or "") for item in files]
    if len(file_ids) != len(set(file_ids)) or set(file_ids) != registered_file_ids:
        raise ValueError(ArtifactErrorCode.IMPORT_GRAPH_INCOMPLETE.value)
    valid_statuses = {"complete", "incomplete", "not_analyzed", "not_applicable"}
    if any(str(item.get("graph_status") or "not_analyzed") not in valid_statuses for item in files):
        raise ValueError(ArtifactErrorCode.IMPORT_GRAPH_INCOMPLETE.value)
    edge_ids: set[str] = set()
    identities: set[tuple[str, str, str, str, int]] = set()
    for item in edges:
        source_id = str(item.get("source_file_id") or "")
        target_id = str(item.get("target_file_id") or "")
        edge_type = str(item.get("edge_type") or "")
        target_name = str(item.get("target_name") or "")
        line_start = item.get("line_start")
        confidence = item.get("confidence", 1)
        if (
            source_id not in registered_file_ids
            or (target_id and target_id not in registered_file_ids)
            or edge_type not in {"import", "from", "dynamic", "unknown"}
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
            or (
                line_start is not None
                and (
                    not isinstance(line_start, int)
                    or isinstance(line_start, bool)
                    or line_start < 1
                )
            )
        ):
            raise ValueError(ArtifactErrorCode.IMPORT_GRAPH_INCOMPLETE.value)
        edge_id = str(item.get("id") or "")
        if edge_id:
            if edge_id in edge_ids:
                raise ValueError(ArtifactErrorCode.IMPORT_GRAPH_INCOMPLETE.value)
            edge_ids.add(edge_id)
        identity = (source_id, target_id, target_name, edge_type, int(line_start or 0))
        if identity in identities:
            raise ValueError(ArtifactErrorCode.IMPORT_GRAPH_INCOMPLETE.value)
        identities.add(identity)


def _policy_validation_is_current(policy: Mapping[str, Any]) -> bool:
    summary = dict(policy["validation_summary"] or {})
    return (
        bool(policy["validated_at"])
        and summary.get("valid") is True
        and str(summary.get("policy_sha256") or "") == str(policy["policy_sha256"])
    )


def _same_comment_creation(
    existing: Mapping[str, Any],
    event: Mapping[str, Any] | None,
    payload: Mapping[str, Any],
) -> bool:
    if event is None:
        return False
    return (
        str(existing.get("artifact_id") or "") == str(payload.get("artifact_id") or "")
        and (str(existing.get("source_thread_id") or "") or None)
        == (str(payload.get("source_thread_id") or "") or None)
        and (str(existing.get("file_id") or "") or None)
        == (str(payload.get("file_id") or "") or None)
        and str(existing.get("file_path") or "") == str(payload.get("file_path") or "")
        and str(existing.get("file_sha256") or "") == str(payload.get("file_sha256") or "")
        and str(existing.get("side") or "") == str(payload.get("side") or "")
        and int(existing.get("line_start") or 0) == int(payload.get("line_start") or 0)
        and int(existing.get("line_end") or 0) == int(payload.get("line_end") or 0)
        and str(existing.get("body") or "") == str(payload.get("body") or "")
        and (str(existing.get("reviewer_user_id") or "") or None)
        == (str(payload.get("reviewer_user_id") or "") or None)
        and str(existing.get("reviewer_role") or "") == str(payload.get("reviewer_role") or "")
        and dict(event.get("metadata") or {}) == dict(payload.get("metadata") or {})
    )


def _same_comment_event(
    existing: Mapping[str, Any],
    thread_id: str,
    payload: Mapping[str, Any],
) -> bool:
    return (
        str(existing.get("thread_id") or "") == thread_id
        and str(existing.get("type") or "") == str(payload.get("type") or "")
        and str(existing.get("body") or "") == str(payload.get("body") or "")
        and (str(existing.get("actor_user_id") or "") or None)
        == (str(payload.get("actor_user_id") or "") or None)
        and str(existing.get("actor_role") or "") == str(payload.get("actor_role") or "")
        and int(existing.get("expected_version") or 0) == int(payload.get("expected_version") or 0)
        and dict(existing.get("metadata") or {}) == dict(payload.get("metadata") or {})
    )


def _same_finding_event_request(
    existing: Mapping[str, Any],
    finding_id: str,
    payload: Mapping[str, Any],
) -> bool:
    if str(existing.get("finding_id") or "") != finding_id:
        return False
    requested_metadata = dict(payload.get("metadata") or {})
    request_fingerprint = str(requested_metadata.get("request_fingerprint") or "")
    if not request_fingerprint:
        return True
    existing_metadata = dict(existing.get("metadata") or {})
    return str(existing_metadata.get("request_fingerprint") or "") == request_fingerprint


def _actor_name(actor: Mapping[str, Any]) -> str:
    for key in ("nickname", "github_name", "github_login", "username", "id"):
        value = str(actor.get(key) or "").strip()
        if value:
            return value[:120]
    return "core_admin"


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _serialize(value) for key, value in dict(row).items()}


def _validate_runtime_completion_payload(
    payload: Mapping[str, Any],
) -> RuntimeDispatchStatus:
    try:
        status = RuntimeDispatchStatus(str(payload["status"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value) from exc
    if status not in {
        RuntimeDispatchStatus.SUCCEEDED,
        RuntimeDispatchStatus.FAILED,
        RuntimeDispatchStatus.TIMED_OUT,
        RuntimeDispatchStatus.CANCELLED,
    }:
        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
    result_key = str(payload.get("result_key") or "")
    result_sha256 = str(payload.get("result_sha256") or "")
    if bool(result_key) != bool(result_sha256):
        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
    if result_sha256 and (
        len(result_sha256) != 64
        or any(character not in "0123456789abcdef" for character in result_sha256)
    ):
        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
    if status == RuntimeDispatchStatus.SUCCEEDED and not result_key:
        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
    error_code = str(payload.get("error_code") or "")
    error_message = " ".join(str(payload.get("error_message") or "").split())
    if len(error_code) > 96 or len(error_message) > 500:
        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
    if status == RuntimeDispatchStatus.SUCCEEDED and (error_code or error_message):
        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
    if status in {RuntimeDispatchStatus.FAILED, RuntimeDispatchStatus.TIMED_OUT} and not error_code:
        raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
    return status


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _as_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    return _parse_time(value)
