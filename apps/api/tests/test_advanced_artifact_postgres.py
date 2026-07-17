from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app.artifacts.content import ArtifactContentService
from app.artifacts.diff import ArtifactDiffService, DiffBuildError, manifest_tree_sha256
from app.artifacts.import_graph import ImportGraphBuildError, ImportGraphService
from app.artifacts.history import ReviewHistoryService
from app.artifacts.models import ArtifactErrorCode, PublicationStatus, ReviewStatus
from app.artifacts.policy_service import ReviewPolicyService
from app.artifacts.repository import PgArtifactRepository
from app.artifacts.storage import LocalArtifactStorage, build_content_key
from app.runtime_runner.repository import PgRuntimeRunnerRepository
from app.schema_migrations import (
    SchemaMigrationError,
    SqlMigration,
    apply_schema_migrations,
    discover_schema_migrations,
)
from app.store import SCHEMA_SQL


class ConnectionLease(AbstractAsyncContextManager[asyncpg.Connection]):
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> asyncpg.Connection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class SingleConnectionPool:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    def acquire(self) -> ConnectionLease:
        return ConnectionLease(self.connection)

    async def execute(self, query: str, *args: object) -> str:
        return await self.connection.execute(query, *args)

    async def fetch(self, query: str, *args: object) -> list[asyncpg.Record]:
        return await self.connection.fetch(query, *args)

    async def fetchrow(self, query: str, *args: object) -> asyncpg.Record | None:
        return await self.connection.fetchrow(query, *args)


class RepositoryStore:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.pool = SingleConnectionPool(connection)

    def _pool(self) -> SingleConnectionPool:
        return self.pool


class PooledRepositoryStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    def _pool(self) -> asyncpg.Pool:
        return self.pool


def database_url() -> str:
    value = os.getenv("ASTRBOT_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("Set ASTRBOT_TEST_DATABASE_URL to run PostgreSQL repository tests")
    return value


def test_p1_data_upgrades_to_advanced_schema_without_rewriting_history() -> None:
    asyncio.run(run_p1_upgrade_scenario(database_url()))


async def run_p1_upgrade_scenario(url: str) -> None:
    connection, transaction = await begin_isolated_schema(url)
    try:
        migrations = discover_schema_migrations()
        assert await apply_schema_migrations(connection, [migrations[0]]) == [
            "20260710_001_artifact_foundation"
        ]
        await seed_market(connection)
        await connection.execute(
            """
            INSERT INTO plugin_artifacts (
                id, plugin_id, source_type, source_repo, archive_sha256,
                size_bytes, quarantine_key, path_suffix
            )
            VALUES ('artifact-p1', 'plugin-1', 'upload', $1, $2, 128, $3, 'abcdef1234')
            """,
            "https://github.com/alice/astrbot_plugin_advanced",
            "a" * 64,
            "artifacts/artifact-p1/source.zip",
        )
        await connection.execute(
            """
            INSERT INTO review_runs (id, artifact_id, type, status)
            VALUES ('run-p1', 'artifact-p1', 'static', 'succeeded')
            """
        )
        await connection.execute(
            """
            INSERT INTO review_findings (
                id, artifact_id, run_id, fingerprint, severity, message
            )
            VALUES ('finding-p1', 'artifact-p1', 'run-p1', 'fingerprint-p1', 'low', 'legacy')
            """
        )
        await connection.execute(
            """
            INSERT INTO artifact_jobs (id, artifact_id, type, idempotency_key)
            VALUES ('job-p1', 'artifact-p1', 'precheck', 'job-p1-once')
            """
        )

        assert await apply_schema_migrations(connection, [migrations[1]]) == [
            "20260710_002_artifact_advanced_review"
        ]
        assert await apply_schema_migrations(connection, migrations) == [
            "20260715_003_review_policy_snapshot",
            "20260717_004_review_observability",
        ]
        assert await apply_schema_migrations(connection, migrations) == []

        artifact = await connection.fetchrow(
            "SELECT * FROM plugin_artifacts WHERE id = 'artifact-p1'"
        )
        run = await connection.fetchrow("SELECT * FROM review_runs WHERE id = 'run-p1'")
        finding = await connection.fetchrow("SELECT * FROM review_findings WHERE id = 'finding-p1'")
        assert artifact and dict(artifact["review_coverage"]) == {}
        assert artifact["policy_version_id"] is None
        assert run and run["queued_at"] == run["created_at"]
        assert finding and finding["source"] == "static"
        assert finding["deterministic"] is True
        assert finding["version"] == 1

        for status in ReviewStatus:
            await connection.execute(
                "UPDATE plugin_artifacts SET review_status = $2 WHERE id = $1",
                "artifact-p1",
                status.value,
            )
        for status in PublicationStatus:
            await connection.execute(
                "UPDATE plugin_artifacts SET publication_status = $2 WHERE id = $1",
                "artifact-p1",
                status.value,
            )

        changed = SqlMigration(
            version=migrations[1].version,
            checksum="0" * 64,
            sql=migrations[1].sql,
        )
        with pytest.raises(SchemaMigrationError, match="checksum mismatch"):
            await apply_schema_migrations(connection, [changed])
    finally:
        await transaction.rollback()
        await connection.close()


def test_advanced_repository_constraints_and_leases_against_postgres() -> None:
    asyncio.run(run_advanced_repository_scenario(database_url()))


def test_diff_service_tree_binding_against_postgres(tmp_path: Path) -> None:
    asyncio.run(run_diff_service_scenario(database_url(), tmp_path))


def test_import_graph_service_against_postgres(tmp_path: Path) -> None:
    asyncio.run(run_import_graph_service_scenario(database_url(), tmp_path))


def test_review_policy_lifecycle_against_postgres() -> None:
    asyncio.run(run_review_policy_lifecycle_scenario(database_url()))


def test_concurrent_review_policy_activation_against_postgres() -> None:
    asyncio.run(run_concurrent_review_policy_activation_scenario(database_url()))


def test_concurrent_auto_approve_is_atomic_against_postgres() -> None:
    asyncio.run(run_concurrent_auto_approve_scenario(database_url()))


def test_concurrent_comment_events_and_decision_are_atomic_against_postgres() -> None:
    asyncio.run(run_concurrent_comment_scenario(database_url()))


def test_category_precedence_and_concurrency_against_postgres() -> None:
    asyncio.run(run_category_precedence_scenario(database_url()))


def test_history_projection_and_emergency_revoke_against_postgres() -> None:
    asyncio.run(run_history_revoke_scenario(database_url()))


def test_review_observability_heartbeat_and_metrics_against_postgres() -> None:
    asyncio.run(run_review_observability_scenario(database_url()))


async def run_review_observability_scenario(url: str) -> None:
    connection, transaction = await begin_isolated_schema(url)
    try:
        await apply_schema_migrations(connection, discover_schema_migrations())
        await seed_market(connection)
        repository = PgArtifactRepository(RepositoryStore(connection))
        runtime_repository = PgRuntimeRunnerRepository(SingleConnectionPool(connection))
        artifact_heartbeat = await repository.upsert_review_worker_heartbeat(
            worker_kind="artifact_worker",
            worker_id="artifact-worker-1",
            components={
                "artifact_worker": {
                    "ready": True,
                    "reason": "",
                    "version": "artifact-worker-v1",
                    "data_updated_at": "",
                }
            },
            ttl_seconds=30,
            capacity=4,
            active_count=1,
        )
        runtime_heartbeat = await runtime_repository.upsert_review_worker_heartbeat(
            worker_kind="runtime_runner",
            worker_id="runtime-runner-1",
            components={
                "runtime": {
                    "ready": True,
                    "reason": "",
                    "version": "runtime-runner-v1",
                    "data_updated_at": "",
                }
            },
            ttl_seconds=30,
            capacity=2,
            active_count=0,
        )
        assert artifact_heartbeat["worker_kind"] == "artifact_worker"
        assert runtime_heartbeat["worker_kind"] == "runtime_runner"
        heartbeats = await repository.list_review_worker_heartbeats()
        assert [(item["worker_kind"], item["live"]) for item in heartbeats] == [
            ("artifact_worker", True),
            ("runtime_runner", True),
        ]

        artifact = await repository.create_artifact(artifact_payload("o"))
        await connection.execute(
            "UPDATE plugin_artifacts SET review_status = 'pending_review' WHERE id = $1",
            artifact["id"],
        )
        run = await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "static",
                "status": "running",
                "idempotency_key": "observability-run",
            }
        )
        await repository.complete_review_run(
            run["id"],
            {
                "status": "failed",
                "summary": "Static stage failed",
                "error_code": "static_scan_failed",
            },
        )
        await repository.enqueue_job(
            {
                "artifact_id": artifact["id"],
                "type": "static_scan",
                "payload": {},
                "idempotency_key": "observability-job",
            }
        )

        snapshot = await repository.get_review_observability_snapshot(
            datetime.now(UTC) - timedelta(hours=24)
        )
        assert snapshot["queue"] == [{"type": "static_scan", "status": "queued", "count": 1}]
        assert snapshot["stages"][0]["type"] == "static"
        assert snapshot["stages"][0]["failure_count"] == 1
        assert snapshot["manual_wait"]["waiting_count"] == 1
    finally:
        await transaction.rollback()
        await connection.close()


async def run_history_revoke_scenario(url: str) -> None:
    connection, transaction = await begin_isolated_schema(url)
    try:
        await apply_schema_migrations(connection)
        await seed_market(connection)
        await connection.execute(
            "UPDATE market_plugins SET repo_version = 'v1.0.0' WHERE id = 'plugin-1'"
        )
        repository = PgArtifactRepository(RepositoryStore(connection))
        stable = await repository.create_artifact(artifact_payload("8"))
        await repository.transition_publication_status(
            stable["id"], PublicationStatus.PUBLISHING.value
        )
        stable = await repository.publish_artifact(
            stable["id"],
            expected_repo_version="v1.0.0",
            published_key="owner-1/advanced/v1.0.0/plugin.zip",
            download_url="https://cdn.example.test/owner-1/advanced/v1.0.0/plugin.zip",
        )
        assert stable is not None

        candidate_payload = artifact_payload("9")
        candidate_payload["base_artifact_id"] = stable["id"]
        candidate = await repository.create_artifact(candidate_payload)
        run = await repository.create_review_run(
            {
                "artifact_id": candidate["id"],
                "type": "static",
                "status": "succeeded",
                "tool_name": "scanner",
                "tool_version": "1.0.0",
                "ruleset_version": "rules-v1",
                "idempotency_key": "postgres-history-run",
            }
        )
        finding = (
            await repository.replace_findings(
                candidate["id"],
                run["id"],
                [
                    {
                        "fingerprint": "postgres-critical",
                        "rule_id": "critical-rule",
                        "severity": "critical",
                        "message": "critical issue",
                        "source": "static",
                        "deterministic": True,
                    }
                ],
            )
        )[0]
        reviewer = {"id": "reviewer-1", "internal_username": "Reviewer"}
        correlation = {"stable_artifact_id": stable["id"], "kind": "fingerprint"}
        finding_link = {
            "expected_version": 1,
            "candidate_artifact_id": candidate["id"],
            "finding_id": finding["id"],
            "status": "open",
            "correlation": correlation,
            "affects_current_release": True,
            "actor_user_id": "reviewer-1",
            "actor_nickname": "Reviewer",
            "actor_source": "user",
            "reason": "Confirmed stable impact",
            "metadata": {"request_fingerprint": "a" * 64},
            "expected_finding": {
                "fingerprint": finding["fingerprint"],
                "run_id": finding["run_id"],
                "rule_id": finding["rule_id"],
                "source": finding["source"],
                "deterministic": finding["deterministic"],
                "severity": finding["severity"],
                "status": finding["status"],
                "file_path": finding.get("file_path"),
                "file_sha256": finding.get("file_sha256"),
                "correlation": dict(finding.get("correlation") or {}),
            },
            "idempotency_key": "postgres-finding-link",
        }
        metadata = {
            "emergency": True,
            "candidate_artifact_id": candidate["id"],
            "finding_id": finding["id"],
            "stable_risk": correlation,
        }
        notification = {
            "event_type": "artifact_stable_risk_revoking",
            "aggregate_type": "artifact",
            "aggregate_id": stable["id"],
            "recipient_user_id": "owner-1",
            "payload": {
                "artifact_id": stable["id"],
                "plugin_id": "plugin-1",
                "candidate_artifact_id": candidate["id"],
                "finding_id": finding["id"],
                "correlation_kind": "fingerprint",
                "emergency": True,
                "reason": "Confirmed stable impact",
            },
            "dedupe_key": "postgres-stable-risk-notification",
        }
        revoking = await repository.request_revoke_artifact(
            stable["id"],
            reason="Confirmed stable impact",
            reviewer=reviewer,
            idempotency_key="postgres-emergency-revoke",
            source="admin",
            input_fingerprints=[finding["fingerprint"]],
            metadata=metadata,
            finding_link=finding_link,
            notification=notification,
        )
        repeated = await repository.request_revoke_artifact(
            stable["id"],
            reason="Confirmed stable impact",
            reviewer=reviewer,
            idempotency_key="postgres-emergency-revoke",
            source="admin",
            input_fingerprints=[finding["fingerprint"]],
            metadata=metadata,
            finding_link=finding_link,
            notification=notification,
        )
        assert revoking and repeated
        assert revoking["publication_status"] == PublicationStatus.REVOKING.value
        with pytest.raises(ValueError, match=ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value):
            await repository.request_revoke_artifact(
                stable["id"],
                reason="Changed reason",
                reviewer=reviewer,
                idempotency_key="postgres-emergency-revoke",
                source="admin",
                input_fingerprints=[finding["fingerprint"]],
                metadata=metadata,
                finding_link=finding_link,
                notification=notification,
            )

        history_service = ReviewHistoryService(repository)
        first_page = await history_service.list(candidate, limit=2, cursor="")
        second_page = await history_service.list(
            candidate,
            limit=20,
            cursor=first_page["next_cursor"] or "",
        )
        history = first_page["items"] + second_page["items"]
        assert first_page["has_more"] is True
        assert {item["type"] for item in history} >= {
            "artifact_submitted",
            "run",
            "finding",
            "finding_event",
        }
        assert len({(item["type"], item["id"]) for item in history}) == len(history)

        plugin = await connection.fetchrow(
            "SELECT status, current_artifact_id FROM market_plugins WHERE id = 'plugin-1'"
        )
        job = await connection.fetchrow(
            "SELECT payload FROM artifact_jobs WHERE idempotency_key = 'postgres-emergency-revoke'"
        )
        decision = await connection.fetchrow(
            "SELECT metadata, input_fingerprints FROM review_decisions "
            "WHERE idempotency_key = 'postgres-emergency-revoke'"
        )
        linked = await connection.fetchrow(
            "SELECT version, affects_current_release, correlation FROM review_findings "
            "WHERE id = $1",
            finding["id"],
        )
        outbox = await connection.fetchrow(
            "SELECT * FROM outbox_events WHERE dedupe_key = 'postgres-stable-risk-notification'"
        )
        assert plugin and plugin["status"] == "unlisted"
        assert plugin["current_artifact_id"] == stable["id"]
        assert job and dict(job["payload"])["finding_id"] == finding["id"]
        assert decision and dict(decision["metadata"]) == metadata
        assert list(decision["input_fingerprints"]) == [finding["fingerprint"]]
        assert linked and linked["version"] == 2 and linked["affects_current_release"] is True
        assert dict(linked["correlation"]) == correlation
        assert outbox and outbox["event_type"] == "artifact_stable_risk_revoking"
    finally:
        await transaction.rollback()
        await connection.close()


async def run_category_precedence_scenario(url: str) -> None:
    schema = f"category_concurrency_{uuid.uuid4().hex}"
    control = await asyncpg.connect(url)
    pool: asyncpg.Pool | None = None
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await control.execute(f"SET search_path TO {schema}")
        await control.set_type_codec(
            "jsonb",
            schema="pg_catalog",
            encoder=json.dumps,
            decoder=json.loads,
        )
        await control.execute(SCHEMA_SQL)
        await apply_schema_migrations(control)
        await seed_market(control)
        await control.execute(
            """
            UPDATE market_plugins
               SET category = 'other',
                   category_source = 'user',
                   metadata = jsonb_set(metadata, '{category_explicit}', 'false'::jsonb)
             WHERE id = 'plugin-1'
            """
        )
        pool = await asyncpg.create_pool(
            url,
            min_size=2,
            max_size=4,
            server_settings={"search_path": schema},
            init=_configure_json_codec,
        )
        repository = PgArtifactRepository(PooledRepositoryStore(pool))
        artifact = await repository.create_artifact(artifact_payload("7"))
        await repository.transition_review_status(artifact["id"], "prechecking")
        artifact = await repository.transition_review_status(artifact["id"], "scanning")
        assert artifact is not None

        low = await repository.apply_category_suggestion(
            artifact["id"],
            suggested_category="entertainment",
            confidence=0.5,
            reason="Low confidence",
            minimum_confidence=0.8,
        )
        high = await repository.apply_category_suggestion(
            artifact["id"],
            suggested_category="utilities",
            confidence=0.95,
            reason="High confidence",
            minimum_confidence=0.8,
        )
        assert low and low["category_applied"] is False
        assert high and high["category_applied"] is True
        assert high["category"] == "utilities"
        assert high["category_source"] == "ai"

        async def ai_update(index: int) -> dict[str, Any] | None:
            return await repository.apply_category_suggestion(
                artifact["id"],
                suggested_category="integrations",
                confidence=0.99,
                reason=f"Concurrent AI suggestion {index}",
                minimum_confidence=0.8,
            )

        async def human_update(index: int) -> None:
            assert pool is not None
            async with pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE market_plugins
                       SET category = 'productivity',
                           category_source = 'user',
                           metadata = jsonb_set(
                               metadata,
                               '{category_explicit}',
                               'true'::jsonb
                           ),
                           updated_at = now()
                     WHERE id = 'plugin-1'
                    """
                )

        for index in range(12):
            await control.execute(
                """
                UPDATE market_plugins
                   SET category = 'other',
                       category_source = 'user',
                       metadata = jsonb_set(
                           metadata,
                           '{category_explicit}',
                           'false'::jsonb
                       )
                 WHERE id = 'plugin-1'
                """
            )
            await asyncio.gather(ai_update(index), human_update(index))
            current = await control.fetchrow(
                """
                SELECT category, category_source, suggested_category,
                       category_confidence, category_reason
                  FROM market_plugins
                 WHERE id = 'plugin-1'
                """
            )
            assert current
            assert current["category"] == "productivity"
            assert current["category_source"] == "user"
            assert current["suggested_category"] == "integrations"
            assert float(current["category_confidence"]) == 0.99

        await control.execute(
            """
            UPDATE market_plugins
               SET category = 'integrations',
                   category_source = 'reviewer',
                   metadata = jsonb_set(
                       metadata,
                       '{category_explicit}',
                       '"broken"'::jsonb
                   )
             WHERE id = 'plugin-1'
            """
        )
        state = await repository.get_artifact_category_state(artifact["id"])
        protected = await repository.apply_category_suggestion(
            artifact["id"],
            suggested_category="ai_tools",
            confidence=1.0,
            reason="Reviewer category must win",
            minimum_confidence=0.8,
        )
        assert state and state["category_explicit"] is True
        assert protected and protected["category_applied"] is False
        assert protected["category"] == "integrations"
        assert protected["category_source"] == "reviewer"
        stored_artifact = await repository.get_artifact(artifact["id"])
        assert stored_artifact and stored_artifact["suggested_category"] == "ai_tools"
    finally:
        if pool is not None:
            await pool.close()
        await control.execute("RESET search_path")
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


async def run_concurrent_review_policy_activation_scenario(url: str) -> None:
    schema = f"policy_concurrency_{uuid.uuid4().hex}"
    control = await asyncpg.connect(url)
    pool: asyncpg.Pool | None = None
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await control.execute(f"SET search_path TO {schema}")
        await control.set_type_codec(
            "jsonb",
            schema="pg_catalog",
            encoder=json.dumps,
            decoder=json.loads,
        )
        await control.execute(SCHEMA_SQL)
        await apply_schema_migrations(control)
        await seed_market(control)
        pool = await asyncpg.create_pool(
            url,
            min_size=2,
            max_size=4,
            server_settings={"search_path": schema},
            init=_configure_json_codec,
        )
        repository = PgArtifactRepository(PooledRepositoryStore(pool))
        service = ReviewPolicyService(repository)
        actor = {
            "id": "reviewer-1",
            "role": "core_admin",
            "github_login": "reviewer",
        }
        base = await service.create_draft(
            version="concurrent-base",
            policy=review_policy_payload("4.26.5"),
            actor=actor,
            request_id="concurrent-base-create",
            idempotency_key="concurrent-base-create",
        )
        base = await service.activate(
            base["id"],
            actor=actor,
            request_id="concurrent-base-activate",
            idempotency_key="concurrent-base-activate",
            reason="Concurrent activation base",
        )

        candidates: list[dict[str, Any]] = []
        for index, version in enumerate(("4.27.0", "4.28.0"), start=1):
            candidate = await service.create_draft(
                version=f"concurrent-candidate-{index}",
                policy=review_policy_payload(version),
                actor=actor,
                request_id=f"concurrent-candidate-create-{index}",
                idempotency_key=f"concurrent-candidate-create-{index}",
            )
            candidate = await service.validate_draft(
                candidate["id"],
                actor=actor,
                request_id=f"concurrent-candidate-validate-{index}",
                idempotency_key=f"concurrent-candidate-validate-{index}",
            )
            candidates.append(candidate)

        async def activate(candidate: dict[str, Any], index: int) -> dict[str, Any] | None:
            return await repository.transition_review_policy(
                candidate["id"],
                action="activate",
                expected_policy_sha256=candidate["policy_sha256"],
                expected_active_policy_id=base["id"],
                validation_summary=None,
                event={
                    "action": "activate",
                    "actor_user_id": actor["id"],
                    "actor_nickname": actor["github_login"],
                    "reason": "Concurrent PostgreSQL activation",
                    "request_id": f"concurrent-activate-{index}",
                    "base_version": base["version"],
                    "diff": {"redacted": True},
                    "idempotency_key": f"concurrent-activate-{index}",
                },
            )

        results = await asyncio.gather(
            *(activate(candidate, index) for index, candidate in enumerate(candidates)),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        conflicts = [result for result in results if isinstance(result, ValueError)]
        assert len(conflicts) == 1
        assert str(conflicts[0]) == ArtifactErrorCode.REVIEW_POLICY_ACTIVATION_CONFLICT.value
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM review_policies WHERE status = 'active' AND is_default"
                )
                == 1
            )
    finally:
        if pool is not None:
            await pool.close()
        await control.execute("RESET search_path")
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


async def run_concurrent_auto_approve_scenario(url: str) -> None:
    schema = f"auto_approve_concurrency_{uuid.uuid4().hex}"
    control = await asyncpg.connect(url)
    pool: asyncpg.Pool | None = None
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await control.execute(f"SET search_path TO {schema}")
        await control.set_type_codec(
            "jsonb",
            schema="pg_catalog",
            encoder=json.dumps,
            decoder=json.loads,
        )
        await control.execute(SCHEMA_SQL)
        await apply_schema_migrations(control)
        await seed_market(control)
        await control.execute(
            "UPDATE market_plugins SET repo_version = 'v1.0.0' WHERE id = 'plugin-1'"
        )
        pool = await asyncpg.create_pool(
            url,
            min_size=2,
            max_size=8,
            server_settings={"search_path": schema},
            init=_configure_json_codec,
        )
        repository = PgArtifactRepository(PooledRepositoryStore(pool))
        service = ReviewPolicyService(repository)
        actor = {
            "id": "reviewer-1",
            "role": "core_admin",
            "github_login": "reviewer",
        }
        policy_payload = review_policy_payload("4.26.5")
        policy_payload["routing"]["auto_approve"] = True
        policy = await service.create_draft(
            version="auto-approve-concurrency-v1",
            policy=policy_payload,
            actor=actor,
            request_id="auto-approve-policy-create",
            idempotency_key="auto-approve-policy-create",
        )
        policy = await service.activate(
            policy["id"],
            actor=actor,
            request_id="auto-approve-policy-activate",
            idempotency_key="auto-approve-policy-activate",
            reason="Enable concurrent auto approve test",
        )
        artifact = await repository.create_artifact(artifact_payload("8"))
        artifact = await repository.snapshot_active_review_policy(artifact["id"])
        assert artifact and artifact["policy_version_id"] == policy["id"]
        await repository.transition_review_status(artifact["id"], "prechecking")
        artifact = await repository.transition_review_status(artifact["id"], "scanning")
        assert artifact is not None

        runs = []
        for run_type in ("static", "runtime", "dependency"):
            runs.append(
                await repository.create_review_run(
                    {
                        "artifact_id": artifact["id"],
                        "type": run_type,
                        "status": "succeeded",
                        "tool_name": run_type,
                        "tool_version": "test-v1",
                        "policy_version_id": policy["id"],
                        "idempotency_key": f"auto-approve-{run_type}-run",
                        "coverage": {
                            "outcome": "completed",
                            "stage_name": run_type,
                            "complete": True,
                        },
                    }
                )
            )
        run_ids = [str(run["id"]) for run in runs]

        async def approve() -> dict[str, Any] | None:
            return await repository.auto_approve_artifact(
                artifact["id"],
                reason="All deterministic review gates passed",
                expected_repo_version="v1.0.0",
                expected_normalized_version="1.0.0",
                expected_version="v1.0.0",
                idempotency_key="postgres-auto-approve-once",
                policy_version_id=policy["id"],
                input_run_ids=run_ids,
                input_fingerprints=[],
                coverage_sha256="9" * 64,
                metadata={"routing": {"route": "auto_approve"}},
                risk_level="none",
            )

        results = await asyncio.gather(*(approve() for _ in range(12)))
        assert all(result and result["review_status"] == "approved" for result in results)
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM review_decisions WHERE artifact_id = $1",
                    artifact["id"],
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM artifact_jobs WHERE artifact_id = $1 AND type = 'publish'",
                    artifact["id"],
                )
                == 1
            )

        conflict_artifact = await repository.create_artifact(artifact_payload("9"))
        conflict_artifact = await repository.snapshot_active_review_policy(conflict_artifact["id"])
        assert conflict_artifact is not None
        await repository.transition_review_status(conflict_artifact["id"], "prechecking")
        conflict_artifact = await repository.transition_review_status(
            conflict_artifact["id"], "scanning"
        )
        assert conflict_artifact is not None
        conflict_run = await repository.create_review_run(
            {
                "artifact_id": conflict_artifact["id"],
                "type": "static",
                "status": "succeeded",
                "policy_version_id": policy["id"],
                "idempotency_key": "auto-approve-conflict-static-run",
            }
        )
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO artifact_jobs (
                    id, artifact_id, type, idempotency_key, policy_version_id
                )
                VALUES ($1, $2, 'static_scan', $3, $4)
                """,
                "conflicting-publish-job",
                conflict_artifact["id"],
                f"publish:{conflict_artifact['id']}",
                policy["id"],
            )
        with pytest.raises(ValueError, match=ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value):
            await repository.auto_approve_artifact(
                conflict_artifact["id"],
                reason="Conflicting publish job must roll back",
                expected_repo_version="v1.0.0",
                expected_normalized_version="1.0.0",
                expected_version="v1.0.0",
                idempotency_key="postgres-auto-approve-conflict",
                policy_version_id=policy["id"],
                input_run_ids=[str(conflict_run["id"])],
                input_fingerprints=[],
                coverage_sha256="8" * 64,
                metadata={"routing": {"route": "auto_approve"}},
                risk_level="none",
            )
        unchanged = await repository.get_artifact(conflict_artifact["id"])
        assert unchanged and unchanged["review_status"] == "scanning"
        assert await repository.list_review_decisions(conflict_artifact["id"]) == []
    finally:
        if pool is not None:
            await pool.close()
        await control.execute("RESET search_path")
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


async def run_concurrent_comment_scenario(url: str) -> None:
    schema = f"comment_concurrency_{uuid.uuid4().hex}"
    control = await asyncpg.connect(url)
    pool: asyncpg.Pool | None = None
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await control.execute(f"SET search_path TO {schema}")
        await control.set_type_codec(
            "jsonb",
            schema="pg_catalog",
            encoder=json.dumps,
            decoder=json.loads,
        )
        await control.execute(SCHEMA_SQL)
        await apply_schema_migrations(control)
        await seed_market(control)
        pool = await asyncpg.create_pool(
            url,
            min_size=2,
            max_size=6,
            server_settings={"search_path": schema},
            init=_configure_json_codec,
        )
        repository = PgArtifactRepository(PooledRepositoryStore(pool))
        artifact = await repository.create_artifact(artifact_payload("c"))
        await control.execute(
            """
            INSERT INTO artifact_files (
                id, artifact_id, path, language, mime_type, sha256,
                size_bytes, line_count, is_text, content_key
            )
            VALUES (
                'comment-file', $1, 'main.py', 'python', 'text/x-python',
                $2, 10, 1, true, 'artifacts/comment/content.txt'
            )
            """,
            artifact["id"],
            "d" * 64,
        )
        await repository.transition_review_status(artifact["id"], "prechecking")
        await repository.transition_review_status(artifact["id"], "scanning")
        await repository.transition_review_status(artifact["id"], "pending_review")
        thread = await repository.create_review_comment(
            {
                "artifact_id": artifact["id"],
                "file_id": "comment-file",
                "file_path": "main.py",
                "file_sha256": "d" * 64,
                "side": "current",
                "line_start": 1,
                "line_end": 1,
                "body": "Concurrent review",
                "reviewer_user_id": "reviewer-1",
                "reviewer_nickname": "Reviewer",
                "reviewer_role": "admin",
                "idempotency_key": "concurrent-comment-create",
            }
        )

        async def reply(marker: str) -> dict[str, Any] | None:
            return await repository.append_review_comment_event(
                thread["id"],
                {
                    "type": "reply",
                    "body": marker,
                    "actor_user_id": "owner-1",
                    "actor_nickname": "Alice",
                    "actor_role": "author",
                    "expected_version": 1,
                    "idempotency_key": f"concurrent-comment-{marker}",
                },
            )

        replies = await asyncio.gather(reply("a"), reply("b"), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in replies) == 1
        conflicts = [result for result in replies if isinstance(result, ValueError)]
        assert len(conflicts) == 1
        assert str(conflicts[0]) == ArtifactErrorCode.COMMENT_VERSION_CONFLICT.value

        async def create_late_comment() -> dict[str, Any]:
            return await repository.create_review_comment(
                {
                    "artifact_id": artifact["id"],
                    "file_id": "comment-file",
                    "file_path": "main.py",
                    "file_sha256": "d" * 64,
                    "side": "current",
                    "line_start": 1,
                    "line_end": 1,
                    "body": "Decision race",
                    "reviewer_user_id": "reviewer-1",
                    "reviewer_nickname": "Reviewer",
                    "reviewer_role": "admin",
                    "idempotency_key": "concurrent-comment-decision-race",
                }
            )

        async def decide() -> dict[str, Any] | None:
            return await repository.decide_artifact(
                artifact["id"],
                action="request_changes",
                target_status="changes_requested",
                reason="Concurrent decision",
                reviewer={"id": "reviewer-1", "internal_username": "reviewer"},
                idempotency_key="concurrent-comment-decision",
            )

        race = await asyncio.gather(create_late_comment(), decide(), return_exceptions=True)
        assert any(isinstance(result, dict) for result in race)
        stored = await repository.get_artifact(artifact["id"])
        assert stored and stored["review_status"] == "changes_requested"
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    """
                    SELECT count(*) FROM review_comments
                     WHERE artifact_id = $1 AND locked_at IS NULL
                    """,
                    artifact["id"],
                )
                == 0
            )
    finally:
        if pool is not None:
            await pool.close()
        await control.execute("RESET search_path")
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


async def _configure_json_codec(connection: asyncpg.Connection) -> None:
    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
    )


async def run_review_policy_lifecycle_scenario(url: str) -> None:
    connection, transaction = await begin_isolated_schema(url)
    try:
        await apply_schema_migrations(connection)
        await seed_market(connection)
        repository = PgArtifactRepository(RepositoryStore(connection))
        service = ReviewPolicyService(repository)
        actor = {
            "id": "reviewer-1",
            "role": "core_admin",
            "github_login": "reviewer",
        }

        first = await service.create_draft(
            version="policy-service-1",
            policy=review_policy_payload("4.26.5"),
            actor=actor,
            request_id="postgres-policy-create-1",
            idempotency_key="postgres-policy-create-1",
        )
        first = await service.activate(
            first["id"],
            actor=actor,
            request_id="postgres-policy-activate-1",
            idempotency_key="postgres-policy-activate-1",
            reason="Activate first PostgreSQL policy",
        )
        second = await service.create_draft(
            version="policy-service-2",
            policy=review_policy_payload("4.27.0"),
            actor=actor,
            request_id="postgres-policy-create-2",
            idempotency_key="postgres-policy-create-2",
        )
        second = await service.activate(
            second["id"],
            actor=actor,
            request_id="postgres-policy-activate-2",
            idempotency_key="postgres-policy-activate-2",
            reason="Replace first PostgreSQL policy",
        )

        first_retired = await repository.get_review_policy(first["id"])
        assert first_retired and first_retired["status"] == "retired"
        assert (await repository.get_active_review_policy())["id"] == second["id"]

        artifact = await repository.create_artifact(artifact_payload("d"))
        artifact = await repository.snapshot_active_review_policy(artifact["id"])
        assert artifact and artifact["policy_version_id"] == second["id"]
        old_run = await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "precheck",
                "status": "succeeded",
                "idempotency_key": "postgres-policy-old-run",
            }
        )
        migrated = await service.migrate_artifact_snapshot(
            artifact["id"],
            first["id"],
            actor=actor,
            request_id="postgres-artifact-policy-migrate",
            idempotency_key="postgres-artifact-policy-migrate",
            reason="Verify PostgreSQL artifact policy migration",
        )
        new_run = await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "static",
                "status": "running",
                "idempotency_key": "postgres-policy-new-run",
            }
        )
        assert old_run["policy_version_id"] == second["id"]
        assert migrated["policy_version_id"] == first["id"]
        assert new_run["policy_version_id"] == first["id"]
        migration_decision = next(
            decision
            for decision in await repository.list_review_decisions(artifact["id"])
            if decision["action"] == "policy_migrate"
        )
        assert migration_decision["policy_version_id"] == first["id"]
        assert (
            migration_decision["metadata"]["policy_migration"]["invalidates_automated_review"]
            is True
        )

        rolled_back = await service.rollback(
            first["id"],
            actor=actor,
            request_id="postgres-policy-rollback-1",
            idempotency_key="postgres-policy-rollback-1",
            reason="Rollback PostgreSQL policy",
        )
        assert rolled_back["status"] == "active"
        assert (await repository.get_review_policy(second["id"]))["status"] == "retired"
        active_count = await connection.fetchval(
            "SELECT count(*) FROM review_policies WHERE status = 'active' AND is_default"
        )
        assert active_count == 1

        events = await repository.list_review_policy_events(first["id"])
        assert {event["action"] for event in events} >= {
            "create",
            "validate",
            "activate",
            "retire",
            "rollback",
        }
        assert all(event["diff"].get("redacted") is True for event in events)
    finally:
        await transaction.rollback()
        await connection.close()


async def run_advanced_repository_scenario(url: str) -> None:
    connection, transaction = await begin_isolated_schema(url)
    try:
        await apply_schema_migrations(connection)
        await seed_market(connection)
        repository = PgArtifactRepository(RepositoryStore(connection))
        policy = await repository.create_review_policy(
            {
                "version": "policy-draft-1",
                "schema_version": "1",
                "policy": {"required_stages": ["precheck", "static", "runtime"]},
                "policy_sha256": "1" * 64,
                "created_by_user_id": "reviewer-1",
                "created_by_nickname": "Reviewer",
            }
        )
        activated_at = datetime.now(UTC).isoformat()
        active = await repository.create_review_policy(
            {
                "version": "policy-active-1",
                "schema_version": "1",
                "status": "active",
                "activated_at": activated_at,
                "policy": {"required_stages": ["precheck", "static"]},
                "policy_sha256": "2" * 64,
            }
        )
        assert active["status"] == "active"
        with pytest.raises(asyncpg.UniqueViolationError):
            async with connection.transaction():
                await repository.create_review_policy(
                    {
                        "version": "policy-active-2",
                        "schema_version": "1",
                        "status": "active",
                        "activated_at": activated_at,
                        "policy": {"required_stages": ["precheck"]},
                        "policy_sha256": "3" * 64,
                    }
                )

        first = await repository.create_artifact(artifact_payload("a"))
        second_payload = artifact_payload("b")
        second_payload["base_artifact_id"] = first["id"]
        second_payload["supersedes_artifact_id"] = first["id"]
        second = await repository.create_artifact(second_payload)
        await repository.bind_artifact_policy(first["id"], policy["id"])
        await repository.replace_artifact_files(
            first["id"],
            [
                {
                    "id": "file-main",
                    "path": "main.py",
                    "sha256": "4" * 64,
                    "size_bytes": 16,
                    "line_count": 2,
                    "is_text": True,
                    "is_entrypoint": True,
                }
            ],
            "5" * 64,
        )
        await repository.replace_artifact_files(
            second["id"],
            [
                {
                    "id": "file-main-next",
                    "path": "main.py",
                    "sha256": "b" * 64,
                    "size_bytes": 18,
                    "line_count": 3,
                    "is_text": True,
                    "is_entrypoint": True,
                }
            ],
            "c" * 64,
        )
        await repository.replace_artifact_diffs(
            second["id"],
            first["id"],
            current_tree_sha256="c" * 64,
            base_tree_sha256="5" * 64,
            diffs=[
                {
                    "base_file_id": "file-main",
                    "current_file_id": "file-main-next",
                    "path": "main.py",
                    "base_path": "main.py",
                    "change_type": "modified",
                    "base_sha256": "4" * 64,
                    "current_sha256": "b" * 64,
                }
            ],
        )
        run = await repository.create_review_run(
            {
                "artifact_id": first["id"],
                "type": "runtime",
                "status": "queued",
                "policy_version_id": policy["id"],
                "tool_name": "runtime-runner",
                "tool_version": "1",
                "input_sha256": "6" * 64,
                "idempotency_key": "runtime-run-once",
            }
        )
        repeated_run = await repository.create_review_run(
            {
                "artifact_id": first["id"],
                "type": "runtime",
                "status": "queued",
                "policy_version_id": policy["id"],
                "idempotency_key": "runtime-run-once",
            }
        )
        assert repeated_run["id"] == run["id"]
        with pytest.raises(ValueError, match=ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value):
            await repository.create_review_run(
                {
                    "artifact_id": second["id"],
                    "type": "runtime",
                    "status": "queued",
                    "idempotency_key": "runtime-run-once",
                }
            )

        malware_run = await repository.create_review_run(
            {
                "artifact_id": first["id"],
                "type": "clamav",
                "status": "running",
                "policy_version_id": policy["id"],
                "tool_name": "clamav",
                "tool_version": "clamd-instream-v1",
                "input_sha256": "d" * 64,
                "idempotency_key": "postgres-clamav-run",
            }
        )
        malware_run = await repository.complete_review_run(
            malware_run["id"],
            {
                "status": "succeeded",
                "summary": "ClamAV scan completed",
                "ruleset_version": "28000",
                "coverage": {
                    "outcome": "completed",
                    "stage_name": "clamav",
                    "scan_result": "clean",
                    "database_version": "28000",
                },
            },
        )
        assert malware_run and malware_run["ruleset_version"] == "28000"
        assert malware_run["coverage"]["database_version"] == "28000"

        dispatch_payload = {
            "artifact_id": first["id"],
            "run_id": run["id"],
            "request": {"schema_version": "1", "artifact_sha256": "a" * 64},
            "request_sha256": "7" * 64,
        }
        dispatch = await repository.create_runtime_dispatch(dispatch_payload)
        repeated_dispatch = await repository.create_runtime_dispatch(dispatch_payload)
        assert repeated_dispatch["id"] == dispatch["id"]
        with pytest.raises(
            ValueError,
            match=ArtifactErrorCode.RUNTIME_DISPATCH_CONFLICT.value,
        ):
            await repository.create_runtime_dispatch(
                {**dispatch_payload, "request_sha256": "0" * 64}
            )
        runner_repository = PgRuntimeRunnerRepository(SingleConnectionPool(connection))
        claimed = await runner_repository.claim_runtime_dispatches("runner-a", 1, 60)
        assert claimed[0]["attempts"] == 1
        assert await runner_repository.renew_runtime_dispatch_lease(dispatch["id"], "runner-a", 60)
        assert await runner_repository.claim_runtime_dispatches("runner-b", 1, 60) == []
        await connection.execute(
            """
            UPDATE runtime_dispatches
               SET lease_expires_at = now() - interval '1 second'
             WHERE id = $1
            """,
            dispatch["id"],
        )
        reclaimed = await runner_repository.claim_runtime_dispatches("runner-b", 1, 60)
        assert reclaimed[0]["attempts"] == 2
        completed = await runner_repository.complete_runtime_dispatch(
            dispatch["id"],
            "runner-b",
            {
                "status": "succeeded",
                "result_key": "private/runtime-result.json",
                "result_sha256": "8" * 64,
                "image_digest": f"sha256:{'9' * 64}",
            },
        )
        assert completed and completed["status"] == "succeeded"
        assert await repository.collect_runtime_dispatch(
            dispatch["id"],
            {
                "status": "succeeded",
                "summary": "runtime passed",
                "raw_result": {"dispatch_id": dispatch["id"]},
                "raw_result_key": "private/runtime-result.json",
                "output_sha256": "8" * 64,
                "coverage": {"outcome": "completed", "stage_name": "runtime"},
                "container_image_digest": f"sha256:{'9' * 64}",
                "worker_id": "runner-b",
            },
            [
                {
                    "fingerprint": "a" * 64,
                    "rule_id": "plugin_initialize_failed",
                    "severity": "high",
                    "category": "plugin_lifecycle",
                    "message": "initialize failed",
                    "source": "runtime",
                    "deterministic": True,
                    "metadata": {"target": "4.26.5"},
                }
            ],
            {
                "artifact_id": first["id"],
                "run_id": run["id"],
                "format": "cyclonedx-json",
                "document_sha256": "c" * 64,
                "object_key": "private/runtime-sbom-collect.json",
                "package_count": 2,
                "generator": "astrbot-runtime-install",
                "tool_version": "cyclonedx-canonical-v1",
            },
        )
        assert await repository.collect_runtime_dispatch(dispatch["id"]) is None
        runtime_runs = await repository.list_review_runs(first["id"])
        collected_run = next(item for item in runtime_runs if item["id"] == run["id"])
        assert collected_run["status"] == "succeeded"
        assert collected_run["coverage"]["outcome"] == "completed"
        runtime_findings = await repository.list_findings(first["id"])
        assert any(item["rule_id"] == "plugin_initialize_failed" for item in runtime_findings)
        runtime_sboms = await repository.list_artifact_sboms(first["id"])
        assert any(item["run_id"] == run["id"] for item in runtime_sboms)
        with pytest.raises(ValueError, match=ArtifactErrorCode.RUNTIME_RESULT_INVALID.value):
            await repository.create_artifact_sbom(
                {
                    "artifact_id": first["id"],
                    "run_id": run["id"],
                    "format": "cyclonedx-json",
                    "document_sha256": "c" * 64,
                    "object_key": "private/different-runtime-sbom.json",
                    "package_count": 2,
                    "generator": "astrbot-runtime-install",
                }
            )

        timeout_run = await repository.create_review_run(
            {
                "artifact_id": first["id"],
                "type": "runtime",
                "status": "queued",
                "idempotency_key": "runtime-timeout-run",
            }
        )
        timeout_dispatch = await repository.create_runtime_dispatch(
            {
                "artifact_id": first["id"],
                "run_id": timeout_run["id"],
                "request": {"schema_version": "1", "artifact_sha256": "a" * 64},
                "request_sha256": "6" * 64,
                "max_attempts": 1,
            }
        )
        assert await repository.claim_runtime_dispatches("runner-timeout", 1, 60)
        await connection.execute(
            """
            UPDATE runtime_dispatches
               SET lease_expires_at = now() - interval '1 second'
             WHERE id = $1
            """,
            timeout_dispatch["id"],
        )
        expired = await repository.expire_runtime_dispatches(10)
        assert [item["id"] for item in expired] == [timeout_dispatch["id"]]
        assert expired[0]["status"] == "timed_out"
        assert await repository.collect_runtime_dispatch(
            timeout_dispatch["id"],
            {
                "status": "timed_out",
                "summary": "runtime timed out",
                "error_code": "runtime_dispatch_timeout",
                "coverage": {"outcome": "failed", "stage_name": "runtime"},
            },
        )

        terminal_run = await repository.create_review_run(
            {
                "artifact_id": first["id"],
                "type": "runtime",
                "status": "running",
                "policy_version_id": policy["id"],
                "idempotency_key": "runtime-terminal-collect-guard",
            }
        )
        terminal_dispatch = await repository.create_runtime_dispatch(
            {
                "artifact_id": first["id"],
                "run_id": terminal_run["id"],
                "request": {"schema_version": "1", "artifact_sha256": "a" * 64},
                "request_sha256": "5" * 64,
            }
        )
        assert await repository.cancel_runtime_dispatch(
            terminal_dispatch["id"],
            error_code="runtime_cancelled",
            error_message="runtime was cancelled",
        )
        assert await repository.complete_review_run(
            terminal_run["id"],
            {
                "status": "failed",
                "summary": "runtime already failed",
                "error_code": "runtime_cancelled",
                "coverage": {"outcome": "failed", "stage_name": "runtime"},
            },
        )
        with pytest.raises(ValueError, match=ArtifactErrorCode.RUNTIME_RESULT_INVALID.value):
            await repository.collect_runtime_dispatch(
                terminal_dispatch["id"],
                {
                    "status": "failed",
                    "summary": "must not overwrite a terminal run",
                    "error_code": "runtime_cancelled",
                    "coverage": {"outcome": "failed", "stage_name": "runtime"},
                },
            )
        guarded_dispatch = await repository.get_runtime_dispatch(terminal_dispatch["id"])
        assert guarded_dispatch and guarded_dispatch["collected_at"] is None

        targeted_run = await repository.create_review_run(
            {
                "artifact_id": first["id"],
                "type": "runtime",
                "status": "running",
                "policy_version_id": policy["id"],
                "idempotency_key": "runtime-targeted-failure",
            }
        )
        sibling_run = await repository.create_review_run(
            {
                "artifact_id": first["id"],
                "type": "runtime",
                "status": "running",
                "policy_version_id": policy["id"],
                "idempotency_key": "runtime-sibling-stays-open",
            }
        )
        assert (
            await repository.fail_open_review_runs(
                first["id"],
                "runtime",
                error_code="runtime_collect_failed",
                summary="one target failed",
                run_id=targeted_run["id"],
            )
            == 1
        )
        targeted_runs = await repository.list_review_runs(first["id"])
        assert (
            next(item for item in targeted_runs if item["id"] == targeted_run["id"])["status"]
            == "failed"
        )
        assert (
            next(item for item in targeted_runs if item["id"] == sibling_run["id"])["status"]
            == "running"
        )

        thread = await repository.create_review_comment(
            {
                "artifact_id": first["id"],
                "file_id": "file-main",
                "file_path": "main.py",
                "file_sha256": "4" * 64,
                "side": "current",
                "line_start": 1,
                "line_end": 1,
                "body": "Review this line",
                "reviewer_user_id": "reviewer-1",
                "reviewer_nickname": "Reviewer",
                "reviewer_role": "admin",
                "idempotency_key": "comment-once",
            }
        )
        repeated_thread = await repository.create_review_comment(
            {
                "artifact_id": first["id"],
                "file_id": "file-main",
                "file_path": "main.py",
                "file_sha256": "4" * 64,
                "side": "current",
                "line_start": 1,
                "line_end": 1,
                "body": "Review this line",
                "reviewer_user_id": "reviewer-1",
                "reviewer_nickname": "Reviewer",
                "reviewer_role": "admin",
                "idempotency_key": "comment-once",
            }
        )
        assert repeated_thread["id"] == thread["id"]
        with pytest.raises(ValueError, match=ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value):
            await repository.create_review_comment(
                {
                    "artifact_id": first["id"],
                    "file_id": "file-main",
                    "file_path": "main.py",
                    "file_sha256": "4" * 64,
                    "side": "current",
                    "line_start": 1,
                    "line_end": 1,
                    "body": "Different body",
                    "reviewer_user_id": "reviewer-1",
                    "reviewer_nickname": "Reviewer",
                    "reviewer_role": "admin",
                    "idempotency_key": "comment-once",
                }
            )
        replied = await repository.append_review_comment_event(
            thread["id"],
            {
                "type": "reply",
                "body": "Addressed",
                "actor_user_id": "owner-1",
                "actor_nickname": "Alice",
                "actor_role": "author",
                "expected_version": 1,
                "idempotency_key": "comment-reply-once",
            },
        )
        assert replied and replied["version"] == 2
        replayed_reply = await repository.append_review_comment_event(
            thread["id"],
            {
                "type": "reply",
                "body": "Addressed",
                "actor_user_id": "owner-1",
                "actor_nickname": "Alice",
                "actor_role": "author",
                "expected_version": 1,
                "idempotency_key": "comment-reply-once",
            },
        )
        assert replayed_reply and replayed_reply["version"] == 2
        with pytest.raises(ValueError, match=ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value):
            await repository.append_review_comment_event(
                thread["id"],
                {
                    "type": "reply",
                    "body": "Different replay body",
                    "actor_user_id": "owner-1",
                    "actor_nickname": "Alice",
                    "actor_role": "author",
                    "expected_version": 1,
                    "idempotency_key": "comment-reply-once",
                },
            )
        with pytest.raises(ValueError, match=ArtifactErrorCode.COMMENT_VERSION_CONFLICT.value):
            await repository.append_review_comment_event(
                thread["id"],
                {
                    "type": "resolve",
                    "actor_user_id": "reviewer-1",
                    "actor_nickname": "Reviewer",
                    "actor_role": "admin",
                    "expected_version": 1,
                    "idempotency_key": "comment-stale-version",
                },
            )
        edited = await repository.append_review_comment_event(
            thread["id"],
            {
                "type": "edit",
                "body": "Updated review line",
                "actor_user_id": "reviewer-1",
                "actor_nickname": "Reviewer",
                "actor_role": "admin",
                "expected_version": 2,
                "idempotency_key": "comment-edit-once",
            },
        )
        assert edited and edited["body"] == "Updated review line"
        resolved = await repository.append_review_comment_event(
            thread["id"],
            {
                "type": "resolve",
                "actor_user_id": "reviewer-1",
                "actor_nickname": "Reviewer",
                "actor_role": "admin",
                "expected_version": 3,
                "idempotency_key": "comment-resolve-once",
            },
        )
        assert resolved and resolved["resolved"] is True
        reopened = await repository.append_review_comment_event(
            thread["id"],
            {
                "type": "reopen",
                "actor_user_id": "reviewer-1",
                "actor_nickname": "Reviewer",
                "actor_role": "admin",
                "expected_version": 4,
                "idempotency_key": "comment-reopen-once",
            },
        )
        assert reopened and reopened["resolved"] is False
        addressed = await repository.append_review_comment_event(
            thread["id"],
            {
                "type": "author_addressed",
                "actor_user_id": "owner-1",
                "actor_nickname": "Alice",
                "actor_role": "author",
                "expected_version": 5,
                "idempotency_key": "comment-addressed-once",
            },
        )
        assert addressed and addressed["version"] == 6 and addressed["resolved"] is False
        assert await repository.count_review_comments(first["id"]) == 1
        stored_thread = await repository.get_review_comment(first["id"], thread["id"])
        assert stored_thread and len(stored_thread["events"]) == 6
        assert await repository.list_review_comments(first["id"], limit=1, offset=0)

        findings = await repository.replace_findings(
            first["id"],
            run["id"],
            [
                {
                    "fingerprint": "runtime-import-failure",
                    "severity": "high",
                    "message": "import failed",
                    "source": "runtime",
                    "deterministic": True,
                }
            ],
        )
        finding = findings[0]
        await repository.update_finding_state(
            finding["id"],
            {
                "expected_version": 1,
                "status": "accepted",
                "correlation": {"same_sha": True},
                "actor_user_id": "reviewer-1",
                "actor_nickname": "Reviewer",
                "actor_source": "user",
                "idempotency_key": "finding-state-once",
            },
        )
        replaced_findings = await repository.replace_findings(
            first["id"],
            run["id"],
            [
                {
                    "fingerprint": "runtime-import-failure",
                    "severity": "critical",
                    "message": "import still fails",
                    "source": "runtime",
                    "deterministic": True,
                }
            ],
        )
        assert replaced_findings[0]["id"] == finding["id"]
        assert replaced_findings[0]["status"] == "accepted"
        assert replaced_findings[0]["correlation"] == {"same_sha": True}
        assert len(await repository.list_finding_events(first["id"])) == 1

        await repository.transition_review_status(first["id"], "prechecking")
        await repository.transition_review_status(first["id"], "scanning")
        await repository.transition_review_status(first["id"], "pending_review")
        decided = await repository.decide_artifact(
            first["id"],
            action="request_changes",
            target_status="changes_requested",
            reason="Changes required",
            reviewer={"id": "reviewer-1", "internal_username": "reviewer"},
            idempotency_key="decision-once",
            policy_version_id=policy["id"],
        )
        repeated_decision = await repository.decide_artifact(
            first["id"],
            action="request_changes",
            target_status="changes_requested",
            reason="Changes required",
            reviewer={"id": "reviewer-1", "internal_username": "reviewer"},
            idempotency_key="decision-once",
            policy_version_id=policy["id"],
        )
        assert decided and repeated_decision
        assert decided["review_status"] == "changes_requested"
        with pytest.raises(ValueError, match=ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value):
            await repository.decide_artifact(
                second["id"],
                action="reject",
                target_status="rejected",
                reason="Unsafe",
                reviewer={"id": "reviewer-1", "internal_username": "reviewer"},
                idempotency_key="decision-once",
            )

        sbom = await repository.create_artifact_sbom(
            {
                "artifact_id": first["id"],
                "run_id": run["id"],
                "format": "cyclonedx-json",
                "document_sha256": "9" * 64,
                "object_key": "private/sbom.json",
                "package_count": 2,
                "generator": "runtime-runner",
            }
        )
        assert sbom["package_count"] == 2

        with pytest.raises((asyncpg.RestrictViolationError, asyncpg.ForeignKeyViolationError)):
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM review_policies WHERE id = $1",
                    policy["id"],
                )

        await connection.execute("DELETE FROM market_users WHERE id = 'reviewer-1'")
        policy_after_user_delete = await repository.get_review_policy(policy["id"])
        assert policy_after_user_delete
        assert policy_after_user_delete["created_by_user_id"] is None
        comment_after_user_delete = await repository.get_review_comment(first["id"], thread["id"])
        assert comment_after_user_delete
        assert comment_after_user_delete["reviewer_user_id"] is None
        assert comment_after_user_delete["reviewer_nickname"] == "Reviewer"
        reviewer_events = [
            event
            for event in comment_after_user_delete["events"]
            if event["actor_nickname"] == "Reviewer"
        ]
        assert reviewer_events and all(event["actor_user_id"] is None for event in reviewer_events)

        await connection.execute("DELETE FROM plugin_artifacts WHERE id = $1", first["id"])
        second_after_delete = await repository.get_artifact(second["id"])
        assert second_after_delete
        assert second_after_delete["base_artifact_id"] is None
        assert second_after_delete["supersedes_artifact_id"] is None
        diffs_after_delete = await repository.list_artifact_diffs(second["id"])
        assert diffs_after_delete[0]["base_artifact_id"] is None
        assert diffs_after_delete[0]["base_file_id"] is None
        assert diffs_after_delete[0]["base_sha256"] == "4" * 64
        assert await repository.get_runtime_dispatch(dispatch["id"]) is None
        assert await repository.list_artifact_sboms(first["id"]) == []
    finally:
        await transaction.rollback()
        await connection.close()


async def run_diff_service_scenario(url: str, root: Path) -> None:
    connection, transaction = await begin_isolated_schema(url)
    try:
        await apply_schema_migrations(connection)
        await seed_market(connection)
        repository = PgArtifactRepository(RepositoryStore(connection))
        storage = LocalArtifactStorage(root, "https://cdn.example.test")
        base = await repository.create_artifact(artifact_payload("4"))
        current_payload = artifact_payload("5")
        current_payload["base_artifact_id"] = base["id"]
        current = await repository.create_artifact(current_payload)

        base_content = b"value = 1\n"
        current_content = b"value = 2\n"
        base_file_id = "file-pg-base-main"
        current_file_id = "file-pg-current-main"
        base_key = build_content_key(base["id"], base_file_id)
        current_key = build_content_key(current["id"], current_file_id)
        await storage.put_text_content(base_key, base_content)
        await storage.put_text_content(current_key, current_content)
        base_manifest = [
            {
                "id": base_file_id,
                "path": "main.py",
                "language": "python",
                "mime_type": "text/x-python",
                "sha256": hashlib.sha256(base_content).hexdigest(),
                "size_bytes": len(base_content),
                "line_count": 1,
                "is_text": True,
                "content_key": base_key,
                "is_entrypoint": True,
            }
        ]
        current_manifest = [
            {
                "id": current_file_id,
                "path": "main.py",
                "language": "python",
                "mime_type": "text/x-python",
                "sha256": hashlib.sha256(current_content).hexdigest(),
                "size_bytes": len(current_content),
                "line_count": 1,
                "is_text": True,
                "content_key": current_key,
                "is_entrypoint": True,
            }
        ]
        base_tree = manifest_tree_sha256(base_manifest)
        current_tree = manifest_tree_sha256(current_manifest)
        await repository.replace_artifact_files(base["id"], base_manifest, base_tree)
        await repository.replace_artifact_files(current["id"], current_manifest, current_tree)
        current = await repository.get_artifact(current["id"])
        assert current is not None

        service = ArtifactDiffService()
        first = await service.build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        second = await service.build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        diffs = await repository.list_artifact_diffs(current["id"])
        assert first.input_sha256 == second.input_sha256
        assert first.output_sha256 == second.output_sha256
        assert len(diffs) == 1
        assert diffs[0]["base_tree_sha256"] == base_tree
        assert diffs[0]["current_tree_sha256"] == current_tree
        assert diffs[0]["hunks_key"]
        assert await repository.get_artifact_file(current["id"], current_file_id) is not None
        assert await repository.get_artifact_file(current["id"], base_file_id) is None
        assert await repository.get_artifact_diff(current["id"], diffs[0]["id"]) is not None
        assert await repository.get_artifact_diff(base["id"], diffs[0]["id"]) is None
        content_service = ArtifactContentService(repository, storage)
        file_page = await content_service.list_files(current, limit=20, offset=0)
        file_content = await content_service.read_file(
            current,
            current_file_id,
            start_line=1,
            line_limit=20,
        )
        diff_page = await content_service.list_diffs(current, limit=20, offset=0)
        diff_content = await content_service.read_diff(current, diffs[0]["id"])
        assert file_page["items"][0]["id"] == current_file_id
        assert file_content["lines"] == [{"number": 1, "text": "value = 2"}]
        assert diff_page["items"][0]["id"] == diffs[0]["id"]
        assert diff_content["hunks_available"] is True
        assert diff_content["hunks"]

        await connection.execute(
            "UPDATE plugin_artifacts SET tree_sha256 = $2 WHERE id = $1",
            current["id"],
            "0" * 64,
        )
        with pytest.raises(DiffBuildError) as caught:
            await service.build(
                artifact=current,
                repository=repository,
                storage=storage,
            )
        assert caught.value.code == ArtifactErrorCode.DIFF_TREE_CHANGED.value
        assert caught.value.retryable is True
    finally:
        await transaction.rollback()
        await connection.close()


async def run_import_graph_service_scenario(url: str, root: Path) -> None:
    connection, transaction = await begin_isolated_schema(url)
    try:
        await apply_schema_migrations(connection)
        await seed_market(connection)
        repository = PgArtifactRepository(RepositoryStore(connection))
        storage = LocalArtifactStorage(root, "https://cdn.example.test")
        artifact = await repository.create_artifact(artifact_payload("6"))
        payloads = {
            "helper.py": b"VALUE = 1\n",
            "main.py": b"from . import helper\n",
        }
        manifests = []
        for path, content in sorted(payloads.items()):
            file_id = f"file-pg-graph-{path.removesuffix('.py')}"
            content_key = build_content_key(artifact["id"], file_id)
            await storage.put_text_content(content_key, content)
            manifests.append(
                {
                    "id": file_id,
                    "path": path,
                    "language": "python",
                    "mime_type": "text/x-python",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "line_count": 1,
                    "is_text": True,
                    "content_key": content_key,
                }
            )
        tree_sha256 = manifest_tree_sha256(manifests)
        await repository.replace_artifact_files(artifact["id"], manifests, tree_sha256)
        artifact = await repository.get_artifact(artifact["id"])
        assert artifact is not None

        service = ImportGraphService()
        first = await service.build(
            artifact=artifact,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
        )
        second = await service.build(
            artifact=artifact,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
        )
        files = await repository.list_artifact_files(artifact["id"])
        edges = await repository.list_dependency_edges(artifact["id"])
        refreshed = await repository.get_artifact(artifact["id"])
        assert refreshed is not None
        assert first.input_sha256 == second.input_sha256
        assert first.output_sha256 == second.output_sha256
        assert len(edges) == 1
        assert edges[0]["source_path"] == "main.py"
        assert edges[0]["target_path"] == "helper.py"
        assert all(item["graph_status"] == "complete" for item in files)
        assert refreshed["review_coverage"]["import_graph"]["output_sha256"] == (
            first.output_sha256
        )

        unrelated_base = await repository.create_artifact(artifact_payload("7"))
        with pytest.raises(ValueError, match=ArtifactErrorCode.DIFF_BASE_INVALID.value):
            await repository.replace_artifact_graph(
                artifact["id"],
                tree_sha256=tree_sha256,
                files=[
                    {
                        "file_id": item["id"],
                        "is_entrypoint": item["is_entrypoint"],
                        "is_reachable": item["is_reachable"],
                        "graph_status": item["graph_status"],
                        "scan_summary": item["scan_summary"],
                    }
                    for item in files
                ],
                edges=[],
                coverage={"complete": False},
                base_artifact_id=unrelated_base["id"],
                base_tree_sha256=str(unrelated_base["tree_sha256"]),
            )

        with pytest.raises(ValueError, match=ArtifactErrorCode.DIFF_BASE_INVALID.value):
            await repository.replace_artifact_graph(
                artifact["id"],
                tree_sha256=tree_sha256,
                files=[
                    {
                        "file_id": item["id"],
                        "is_entrypoint": item["is_entrypoint"],
                        "is_reachable": item["is_reachable"],
                        "graph_status": item["graph_status"],
                        "scan_summary": item["scan_summary"],
                    }
                    for item in files
                ],
                edges=[],
                coverage={"complete": False},
                base_artifact_id="artifact-missing",
                base_tree_sha256="1" * 64,
            )
        assert len(await repository.list_dependency_edges(artifact["id"])) == 1

        await connection.execute(
            "UPDATE plugin_artifacts SET tree_sha256 = $2 WHERE id = $1",
            artifact["id"],
            "0" * 64,
        )
        with pytest.raises(ImportGraphBuildError) as caught:
            await service.build(
                artifact=artifact,
                repository=repository,
                storage=storage,
                entrypoint_paths={"main.py"},
            )
        assert caught.value.code == ArtifactErrorCode.DIFF_TREE_CHANGED.value
        assert caught.value.retryable is True
    finally:
        await transaction.rollback()
        await connection.close()


async def begin_isolated_schema(
    url: str,
) -> tuple[asyncpg.Connection, asyncpg.Transaction]:
    connection = await asyncpg.connect(url)
    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
    )
    transaction = connection.transaction()
    await transaction.start()
    await connection.execute("SET LOCAL search_path TO pg_temp")
    await connection.execute(SCHEMA_SQL)
    return connection, transaction


async def seed_market(connection: asyncpg.Connection) -> None:
    await connection.execute(
        """
        INSERT INTO market_users (
            id, github_id, github_login, github_name, role
        )
        VALUES
            ('owner-1', '100', 'alice', 'Alice', 'user'),
            ('reviewer-1', '200', 'reviewer', 'Reviewer', 'admin')
        """
    )
    await connection.execute(
        """
        INSERT INTO market_plugins (
            id, name, display_name, desc_text, author, repo,
            owner_user_id, owner_github_login, status
        )
        VALUES (
            'plugin-1', 'astrbot_plugin_advanced', 'Advanced',
            'Advanced review fixture', 'Alice',
            'https://github.com/alice/astrbot_plugin_advanced',
            'owner-1', 'alice', 'pending'
        )
        """
    )


def artifact_payload(digest: str) -> dict[str, Any]:
    return {
        "plugin_id": "plugin-1",
        "version": "v1.0.0",
        "normalized_version": "1.0.0",
        "source_type": "upload",
        "source_repo": "https://github.com/alice/astrbot_plugin_advanced",
        "archive_sha256": digest * 64,
        "size_bytes": 128,
        "quarantine_key": f"artifacts/{digest * 8}/source.zip",
        "submitted_by": "owner-1",
        "submitted_by_snapshot": {"github_login": "alice"},
    }


def review_policy_payload(astrbot_version: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "required_stages": ["static", "runtime", "dependency"],
        "runtime_targets": [{"astrbot": astrbot_version, "python": "3.12"}],
        "limits": {
            "cpu": 1,
            "memory_mb": 768,
            "pids": 128,
            "timeout_seconds": 120,
        },
        "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
        "llm": {"enabled": False},
        "malware": {"clamav": False},
        "dependency": {"enabled": True, "max_severity": "high"},
        "routing": {"auto_approve": False, "manual_review_at": "low"},
    }
