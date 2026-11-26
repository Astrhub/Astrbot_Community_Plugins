from __future__ import annotations

import asyncio
from pathlib import Path

from app.schema_migrations import apply_schema_migrations, discover_schema_migrations
from app.store import PgRedisMarketStore
from test_advanced_artifact_postgres import (
    SingleConnectionPool,
    begin_isolated_schema,
    database_url,
    seed_market,
)


def test_download_migration_is_idempotent_and_counters_are_atomic() -> None:
    async def scenario() -> None:
        connection, transaction = await begin_isolated_schema(database_url())
        try:
            await seed_market(connection)
            migration = discover_schema_migrations("app.migrations")
            selected = [item for item in migration if item.version.endswith("_download_tracking")]
            assert len(selected) == 1
            assert await apply_schema_migrations(connection, selected) == [selected[0].version]
            assert await apply_schema_migrations(connection, selected) == []

            store = PgRedisMarketStore("postgresql://unused", "redis://unused", 60)
            store.pool = SingleConnectionPool(connection)
            assert await store.increment_plugin_downloads("plugin-1") is True
            assert await store.increment_plugin_downloads("plugin-1") is True
            plugin = await store.get_plugin("plugin-1")
            assert plugin is not None
            assert plugin["download_count"] == 2
            assert await store.increment_plugin_downloads("missing") is False
        finally:
            await transaction.rollback()
            await connection.close()

    asyncio.run(scenario())


def test_download_migration_clears_only_registered_external_artifact_urls() -> None:
    sql = Path("apps/api/app/migrations/20261126_015_download_tracking.sql").read_text()
    assert "owner_type = 'artifact'" in sql
    assert "o.purpose = 'artifact-published'" in sql
    assert "o.status = 'active'" in sql
