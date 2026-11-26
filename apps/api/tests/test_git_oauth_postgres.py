from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.git_sources.credentials import CredentialStore
from app.git_sources.models import OAuthTokenSet
from app.schema_migrations import apply_schema_migrations
from app.store import SCHEMA_SQL, PgRedisMarketStore
from test_advanced_artifact_postgres import database_url, seed_market


TEST_MASTER_KEY = "test-oauth-postgres-master-key-32bytes"


@asynccontextmanager
async def oauth_schema() -> AsyncIterator[tuple[PgRedisMarketStore, asyncpg.Connection]]:
    url = database_url()
    schema = "oauth_test_" + uuid.uuid4().hex
    connection = await asyncpg.connect(url)
    store = PgRedisMarketStore(url, "redis://unused", 60)
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"SET search_path TO {schema}")
        await connection.execute(SCHEMA_SQL)
        await apply_schema_migrations(connection)
        await seed_market(connection)
        store.pool = await asyncpg.create_pool(
            url,
            min_size=1,
            max_size=2,
            server_settings={"search_path": schema},
            init=store._init_connection,
        )
        yield store, connection
    finally:
        if store.pool:
            await store.pool.close()
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


def test_gitlab_oauth_timestamps_against_postgres() -> None:
    async def scenario() -> None:
        async with oauth_schema() as (store, connection):
            expires_at = datetime.now(UTC).replace(microsecond=972883) + timedelta(hours=2)
            expires_text = expires_at.isoformat().replace("+00:00", "Z")
            credentials = CredentialStore(store, TEST_MASTER_KEY)
            credential_ref = await credentials.save_token_set(
                user_id="owner-1",
                token_set=OAuthTokenSet(
                    provider="gitlab",
                    access_token="access-token",
                    refresh_token="refresh-token",
                    scope="read_user read_api read_repository",
                    expires_at=expires_text,
                    provider_user_id="gitlab-100",
                    provider_login="alice-gitlab",
                ),
            )

            credential = await connection.fetchrow(
                "SELECT expires_at FROM market_oauth_credentials WHERE credential_ref = $1",
                credential_ref,
            )
            assert credential is not None
            assert credential["expires_at"] == expires_at

            for hours in (0, 1):
                identity_expires = expires_at + timedelta(hours=hours)
                identity_expires_text = identity_expires.isoformat().replace("+00:00", "Z")
                identity = await store.upsert_oauth_identity(
                    "owner-1",
                    provider="gitlab",
                    instance_url="https://gitlab.com",
                    provider_user_id="gitlab-100",
                    provider_login="alice-gitlab",
                    credential_ref=credential_ref,
                    expires_at=identity_expires_text,
                )
                assert identity["expires_at"] == identity_expires_text

            rotated_at = datetime.now(UTC).replace(microsecond=123456)
            rotated_expires = expires_at + timedelta(hours=2)
            rotated = await store.update_oauth_identity_rotation(
                identity["id"],
                credential_ref=credential_ref,
                expires_at=rotated_expires.isoformat().replace("+00:00", "Z"),
                rotation_status="rotated",
                token_refreshed_at=rotated_at.isoformat().replace("+00:00", "Z"),
            )
            assert rotated is not None
            assert rotated["expires_at"] == rotated_expires.isoformat().replace("+00:00", "Z")
            assert rotated["token_refreshed_at"] == rotated_at.isoformat().replace("+00:00", "Z")

            with pytest.raises(ValueError, match="Invalid OAuth timestamp"):
                await store.upsert_oauth_credential(
                    "cred_invalid",
                    user_id="owner-1",
                    provider="gitlab",
                    ciphertext="invalid",
                    expires_at="not-a-timestamp",
                )

    asyncio.run(scenario())
