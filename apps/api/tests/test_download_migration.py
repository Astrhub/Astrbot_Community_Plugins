from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import asyncpg

from app.schema_migrations import apply_schema_migrations, discover_schema_migrations
from app.store import SCHEMA_SQL, PgRedisMarketStore
from test_advanced_artifact_postgres import database_url, seed_market


@asynccontextmanager
async def download_schema():
    url = database_url()
    schema = "download_test_" + uuid.uuid4().hex
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"SET search_path TO {schema}")
        await connection.execute(SCHEMA_SQL)
        await seed_market(connection)
        migrations = discover_schema_migrations("app.migrations")
        selected = next(item for item in migrations if item.version.endswith("_download_tracking"))
        prerequisites = [item for item in migrations if item.version < selected.version]
        await apply_schema_migrations(connection, prerequisites)
        yield connection, schema, selected, url
    finally:
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


def test_download_migration_is_idempotent_and_counters_are_atomic() -> None:
    async def scenario() -> None:
        async with download_schema() as (connection, schema, migration, url):
            assert await apply_schema_migrations(connection, [migration]) == [migration.version]
            assert await apply_schema_migrations(connection, [migration]) == []

            store = PgRedisMarketStore(url, "redis://unused", 60)
            try:
                store.pool = await asyncpg.create_pool(
                    url,
                    min_size=2,
                    max_size=8,
                    server_settings={"search_path": schema},
                    init=store._init_connection,
                )
                same_requester = await asyncio.gather(
                    *(store.record_plugin_download("plugin-1", "requester-1") for _ in range(8))
                )
                assert sum(same_requester) == 1
                assert await store.record_plugin_download("plugin-1", "requester-1") is False

                distinct_requesters = await asyncio.gather(
                    *(
                        store.record_plugin_download("plugin-1", f"requester-{index}")
                        for index in range(2, 14)
                    )
                )
                assert all(distinct_requesters)
                plugin = await store.get_plugin("plugin-1")
                assert plugin is not None
                assert plugin["download_count"] == 13

                await connection.execute(
                    """
                    UPDATE market_plugin_download_windows
                       SET window_started_at = now() - interval '61 seconds'
                     WHERE plugin_id = 'plugin-1' AND requester_hash = 'requester-1'
                    """
                )
                assert await store.record_plugin_download("plugin-1", "requester-1") is True
                await store.update_plugin_metadata("plugin-1", {"display_name": "Updated"})
                plugin = await store.get_plugin("plugin-1")
                assert plugin is not None
                assert plugin["download_count"] == 14
                metadata = await connection.fetchval(
                    "SELECT metadata FROM market_plugins WHERE id = 'plugin-1'"
                )
                assert "download_count" not in metadata
                assert await store.record_plugin_download("missing", "requester-1") is False
            finally:
                if store.pool:
                    await store.pool.close()

    asyncio.run(scenario())


def test_download_migration_clears_only_registered_external_artifact_urls() -> None:
    async def scenario() -> None:
        async with download_schema() as (connection, _, migration, _):
            await connection.execute(
                """
                INSERT INTO plugin_artifacts (
                    id, plugin_id, source_type, source_repo, archive_sha256,
                    size_bytes, quarantine_key, path_suffix, published_key, download_url
                ) VALUES
                    (
                        'artifact-registered', 'plugin-1', 'github',
                        'https://github.com/alice/astrbot_plugin_advanced', $1,
                        128, 'quarantine/registered.zip', 'abcdef1234',
                        'published/registered.zip',
                        'https://imgbed.example.test/registered.zip'
                    ),
                    (
                        'artifact-unregistered', 'plugin-1', 'github',
                        'https://github.com/alice/astrbot_plugin_advanced', $2,
                        128, 'quarantine/unregistered.zip', 'fedcba4321',
                        'published/unregistered.zip',
                        'https://imgbed.example.test/unregistered.zip'
                    ),
                    (
                        'artifact-legacy', 'plugin-1', 'github',
                        'https://github.com/alice/astrbot_plugin_advanced', $3,
                        128, 'quarantine/legacy.zip', 'aabbcc1234',
                        'published/legacy.zip',
                        'https://imgbed.example.test/legacy.zip'
                    )
                """,
                "a" * 64,
                "b" * 64,
                "c" * 64,
            )
            await connection.execute(
                """
                INSERT INTO market_storage_profiles (id, name, backend)
                VALUES ('profile-1', 'Test', 'imgbed')
                """
            )
            await connection.execute(
                """
                INSERT INTO market_storage_objects (
                    id, purpose, profile_id, backend, object_key, status,
                    owner_type, owner_id
                ) VALUES
                    (
                        'object-registered', 'artifact-published', 'profile-1', 'imgbed',
                        'published/registered.zip', 'active', 'artifact', 'artifact-registered'
                    ),
                    (
                        'object-legacy', 'artifact-published', 'profile-1', 'imgbed',
                        'published/legacy.zip', 'active', 'legacy_backfill', ''
                    )
                """
            )

            assert await apply_schema_migrations(connection, [migration]) == [migration.version]
            rows = await connection.fetch(
                """
                SELECT id, download_url
                  FROM plugin_artifacts
                 WHERE id IN ('artifact-registered', 'artifact-unregistered', 'artifact-legacy')
                """
            )
            urls = {row["id"]: row["download_url"] for row in rows}
            assert urls == {
                "artifact-registered": None,
                "artifact-unregistered": "https://imgbed.example.test/unregistered.zip",
                "artifact-legacy": None,
            }

    asyncio.run(scenario())
