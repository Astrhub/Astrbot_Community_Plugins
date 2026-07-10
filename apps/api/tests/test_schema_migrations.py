from __future__ import annotations

import asyncio
import os
from contextlib import AbstractAsyncContextManager

import asyncpg
import pytest

from app.schema_migrations import (
    SchemaMigrationError,
    SqlMigration,
    apply_schema_migrations,
    discover_schema_migrations,
)
from app.store import SCHEMA_SQL


class FakeTransaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.applied: dict[str, str] = {}
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args: object) -> str:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, args))
        if normalized.startswith("INSERT INTO market_schema_migrations"):
            self.applied[str(args[0])] = str(args[1])
        return "OK"

    async def fetch(self, _: str) -> list[dict[str, str]]:
        return [
            {"version": version, "checksum": checksum}
            for version, checksum in sorted(self.applied.items())
        ]

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()


def migration(version: str, sql: str) -> SqlMigration:
    return SqlMigration(version=version, checksum=f"sha-{version}", sql=sql)


def test_schema_migrations_apply_in_version_order_and_are_idempotent() -> None:
    connection = FakeConnection()
    migrations = [
        migration("20260710_002_second", "SELECT 'second'"),
        migration("20260710_001_first", "SELECT 'first'"),
    ]

    first = asyncio.run(apply_schema_migrations(connection, migrations))
    second = asyncio.run(apply_schema_migrations(connection, migrations))

    assert first == ["20260710_001_first", "20260710_002_second"]
    assert second == []
    assert connection.applied == {
        "20260710_001_first": "sha-20260710_001_first",
        "20260710_002_second": "sha-20260710_002_second",
    }
    migration_sql = [sql for sql, _ in connection.executed if sql.startswith("SELECT '")]
    assert migration_sql == ["SELECT 'first'", "SELECT 'second'"]


def test_schema_migrations_reject_changed_checksum() -> None:
    connection = FakeConnection()
    connection.applied["20260710_001_first"] = "old-checksum"

    with pytest.raises(SchemaMigrationError, match="checksum mismatch"):
        asyncio.run(
            apply_schema_migrations(
                connection,
                [migration("20260710_001_first", "SELECT 'changed'")],
            )
        )

    assert any(sql.startswith("SELECT pg_advisory_unlock") for sql, _ in connection.executed)


def test_schema_migrations_reject_duplicate_versions() -> None:
    with pytest.raises(SchemaMigrationError, match="Duplicate schema migration versions"):
        asyncio.run(
            apply_schema_migrations(
                FakeConnection(),
                [
                    migration("20260710_001_same", "SELECT 1"),
                    migration("20260710_001_same", "SELECT 2"),
                ],
            )
        )


def test_artifact_foundation_migration_declares_required_schema() -> None:
    migrations = discover_schema_migrations()

    assert [item.version for item in migrations] == ["20260710_001_artifact_foundation"]
    sql = migrations[0].sql
    for table in (
        "plugin_artifacts",
        "artifact_files",
        "review_runs",
        "review_findings",
        "review_decisions",
        "artifact_jobs",
        "outbox_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    for column in ("repo_version", "current_artifact_id", "category", "category_source"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql
    assert "plugin_artifacts_published_version_idx" in sql
    assert "market_plugins_current_artifact_fk" in sql


def test_artifact_foundation_migration_against_postgres() -> None:
    database_url = os.getenv("ASTRBOT_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("Set ASTRBOT_TEST_DATABASE_URL to run migration integration test")

    asyncio.run(run_artifact_foundation_migration(database_url))


async def run_artifact_foundation_migration(database_url: str) -> None:
    connection = await asyncpg.connect(database_url)
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("SET LOCAL search_path TO pg_temp")
        await connection.execute(SCHEMA_SQL)

        first = await apply_schema_migrations(connection)
        second = await apply_schema_migrations(connection)

        assert first == ["20260710_001_artifact_foundation"]
        assert second == []
        table_names = await connection.fetch(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = current_schema()
            """
        )
        tables = {str(row["table_name"]) for row in table_names}
        assert {
            "market_plugins",
            "plugin_artifacts",
            "artifact_files",
            "review_runs",
            "review_findings",
            "review_decisions",
            "artifact_jobs",
            "outbox_events",
            "market_schema_migrations",
        } <= tables
        plugin_columns = await connection.fetch(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'market_plugins'
            """
        )
        columns = {str(row["column_name"]) for row in plugin_columns}
        assert {"repo_version", "current_artifact_id", "category", "category_source"} <= columns
    finally:
        await transaction.rollback()
        await connection.close()
