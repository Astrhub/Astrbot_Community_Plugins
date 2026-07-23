from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import asyncpg

from ..artifacts.observability import normalize_worker_heartbeat

_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_TERMINAL_STATUSES = {"succeeded", "failed", "timed_out", "cancelled"}


class PgRuntimeRunnerRepository:
    """只暴露 runner 所需的 claim、续租和完成操作。"""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PgRuntimeRunnerRepository:
        if not database_url:
            raise ValueError("runtime_runner_database_url_missing")
        pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=4,
            command_timeout=30,
            init=_configure_connection,
        )
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

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
        row = await self._pool.fetchrow(
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
        return dict(row)

    async def claim_runtime_dispatches(
        self,
        runner_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
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
        return [dict(row) for row in rows]

    async def renew_runtime_dispatch_lease(
        self,
        dispatch_id: str,
        runner_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await self._pool.execute(
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
        completion = _validate_completion(payload)
        row = await self._pool.fetchrow(
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
            completion["status"],
            completion["result_key"],
            completion["result_sha256"],
            completion["image_digest"],
            completion["error_code"],
            completion["error_message"],
        )
        return dict(row) if row else None


async def _configure_connection(connection: Any) -> None:
    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=lambda value: json.dumps(value, ensure_ascii=True, allow_nan=False),
        decoder=json.loads,
        format="text",
    )


def _validate_completion(payload: Mapping[str, Any]) -> dict[str, str | None]:
    status = str(payload.get("status") or "")
    if status not in _TERMINAL_STATUSES:
        raise ValueError("invalid_runtime_completion_status")
    result_key = str(payload.get("result_key") or "") or None
    result_sha256 = str(payload.get("result_sha256") or "") or None
    image_digest = str(payload.get("image_digest") or "")
    error_code = str(payload.get("error_code") or "").strip()
    error_message = " ".join(str(payload.get("error_message") or "").split())[:500]
    if (result_key is None) != (result_sha256 is None):
        raise ValueError("runtime_result_reference_incomplete")
    if status == "succeeded" and (not result_key or not image_digest or error_code):
        raise ValueError("runtime_success_payload_invalid")
    if status != "succeeded" and not error_code:
        raise ValueError("runtime_failure_error_code_missing")
    if error_code and not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("invalid_runtime_error_code")
    return {
        "status": status,
        "result_key": result_key,
        "result_sha256": result_sha256,
        "image_digest": image_digest,
        "error_code": error_code,
        "error_message": error_message,
    }
