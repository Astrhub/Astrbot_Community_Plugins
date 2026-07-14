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
        status = RuntimeDispatchStatus(str(payload["status"]))
        if status not in {
            RuntimeDispatchStatus.SUCCEEDED,
            RuntimeDispatchStatus.FAILED,
            RuntimeDispatchStatus.TIMED_OUT,
            RuntimeDispatchStatus.CANCELLED,
        }:
            raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
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

    async def collect_runtime_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        row = await self._advanced_pool().fetchrow(
            """
            UPDATE runtime_dispatches
               SET collected_at = now(),
                   updated_at = now()
             WHERE id = $1
               AND collected_at IS NULL
               AND status IN ('succeeded', 'failed', 'timed_out')
         RETURNING *
            """,
            dispatch_id,
        )
        return _record(row) if row else None

    async def create_review_comment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    "SELECT * FROM review_comments WHERE idempotency_key = $1",
                    payload["idempotency_key"],
                )
                if existing:
                    if str(existing["artifact_id"]) != str(payload["artifact_id"]):
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    return _record(existing)
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
                existing_event = await connection.fetchrow(
                    "SELECT thread_id FROM review_comment_events WHERE idempotency_key = $1",
                    payload["idempotency_key"],
                )
                if existing_event:
                    if str(existing_event["thread_id"]) != thread_id:
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    existing_thread = await connection.fetchrow(
                        "SELECT * FROM review_comments WHERE id = $1",
                        thread_id,
                    )
                    return _record(existing_thread) if existing_thread else None
                thread = await connection.fetchrow(
                    "SELECT * FROM review_comments WHERE id = $1 FOR UPDATE",
                    thread_id,
                )
                if not thread:
                    return None
                if thread["locked_at"] is not None:
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

    async def list_review_comments(self, artifact_id: str) -> list[dict[str, Any]]:
        pool = self._advanced_pool()
        async with pool.acquire() as connection:
            threads = await connection.fetch(
                """
                SELECT * FROM review_comments
                 WHERE artifact_id = $1
              ORDER BY file_path, line_start, created_at, id
                """,
                artifact_id,
            )
            events = await connection.fetch(
                """
                SELECT * FROM review_comment_events
                 WHERE artifact_id = $1
              ORDER BY created_at, id
                """,
                artifact_id,
            )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            grouped.setdefault(str(event["thread_id"]), []).append(_record(event))
        return [
            {**_record(thread), "events": grouped.get(str(thread["id"]), [])} for thread in threads
        ]

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
                    "SELECT finding_id FROM review_finding_events WHERE idempotency_key = $1",
                    payload["idempotency_key"],
                )
                if existing_event:
                    if str(existing_event["finding_id"]) != finding_id:
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
        status = RuntimeDispatchStatus(str(payload["status"]))
        if status not in {
            RuntimeDispatchStatus.SUCCEEDED,
            RuntimeDispatchStatus.FAILED,
            RuntimeDispatchStatus.TIMED_OUT,
            RuntimeDispatchStatus.CANCELLED,
        }:
            raise ValueError(ArtifactErrorCode.RUNTIME_RESULT_INVALID.value)
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

    async def collect_runtime_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        dispatch = self.dispatches.get(dispatch_id)
        if (
            not dispatch
            or dispatch.get("collected_at")
            or dispatch["status"] not in {"succeeded", "failed", "timed_out"}
        ):
            return None
        dispatch["collected_at"] = _utc_now()
        dispatch["updated_at"] = _utc_now()
        return deepcopy(dispatch)

    async def create_review_comment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if str(payload["artifact_id"]) not in self.artifacts:
                raise ValueError(ArtifactErrorCode.COMMENT_LINE_INVALID.value)
            if payload.get("file_id") and not any(
                file["id"] == payload["file_id"]
                for file in self.files.get(str(payload["artifact_id"]), [])
            ):
                raise ValueError(ArtifactErrorCode.COMMENT_LINE_INVALID.value)
            for thread in self.review_comments.values():
                if thread["idempotency_key"] == payload["idempotency_key"]:
                    if thread["artifact_id"] != payload["artifact_id"]:
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    return deepcopy(thread)
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
                    if event["thread_id"] != thread_id:
                        raise ValueError(ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value)
                    thread = self.review_comments.get(thread_id)
                    return deepcopy(thread) if thread else None
            thread = self.review_comments.get(thread_id)
            if not thread:
                return None
            if thread.get("locked_at"):
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

    async def list_review_comments(self, artifact_id: str) -> list[dict[str, Any]]:
        events_by_thread: dict[str, list[dict[str, Any]]] = {}
        for event in sorted(
            self.comment_events.values(),
            key=lambda item: (item["created_at"], item["id"]),
        ):
            if event["artifact_id"] == artifact_id:
                events_by_thread.setdefault(event["thread_id"], []).append(deepcopy(event))
        threads = [
            {**deepcopy(thread), "events": events_by_thread.get(thread["id"], [])}
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
        return threads

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
                    if event["finding_id"] != finding_id:
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


def _policy_validation_is_current(policy: Mapping[str, Any]) -> bool:
    summary = dict(policy["validation_summary"] or {})
    return (
        bool(policy["validated_at"])
        and summary.get("valid") is True
        and str(summary.get("policy_sha256") or "") == str(policy["policy_sha256"])
    )


def _actor_name(actor: Mapping[str, Any]) -> str:
    for key in ("nickname", "github_name", "github_login", "username", "id"):
        value = str(actor.get(key) or "").strip()
        if value:
            return value[:120]
    return "core_admin"


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _serialize(value) for key, value in dict(row).items()}


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
