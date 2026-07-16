from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from app.artifacts.models import ArtifactErrorCode, PublicationStatus, ReviewStatus
from app.artifacts.policy_service import ReviewPolicyService
from app.artifacts.repository import PgArtifactRepository
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
            "20260715_003_review_policy_snapshot"
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


def test_review_policy_lifecycle_against_postgres() -> None:
    asyncio.run(run_review_policy_lifecycle_scenario(database_url()))


def test_concurrent_review_policy_activation_against_postgres() -> None:
    asyncio.run(run_concurrent_review_policy_activation_scenario(database_url()))


def test_category_precedence_and_concurrency_against_postgres() -> None:
    asyncio.run(run_category_precedence_scenario(database_url()))


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
        )
        assert await repository.collect_runtime_dispatch(dispatch["id"]) is None
        runtime_runs = await repository.list_review_runs(first["id"])
        collected_run = next(item for item in runtime_runs if item["id"] == run["id"])
        assert collected_run["status"] == "succeeded"
        assert collected_run["coverage"]["outcome"] == "completed"
        runtime_findings = await repository.list_findings(first["id"])
        assert any(item["rule_id"] == "plugin_initialize_failed" for item in runtime_findings)

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

        with pytest.raises(
            (asyncpg.RestrictViolationError, asyncpg.ForeignKeyViolationError)
        ):
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM review_policies WHERE id = $1",
                    policy["id"],
                )

        await connection.execute("DELETE FROM market_users WHERE id = 'reviewer-1'")
        policy_after_user_delete = await repository.get_review_policy(policy["id"])
        assert policy_after_user_delete
        assert policy_after_user_delete["created_by_user_id"] is None

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
