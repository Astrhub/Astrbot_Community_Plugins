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

            verified_at = datetime.now(UTC).replace(microsecond=654321)
            verified_text = verified_at.isoformat()
            inserted = await store.upsert_git_repository_binding(
                "owner-1",
                normalized_repo="github.com/alice/astrbot_plugin_demo",
                repo_url="https://github.com/alice/astrbot_plugin_demo",
                provider="github",
                instance_url="https://github.com",
                repo_external_id="21",
                owner_id="10",
                permission_level="write",
                verification_method="oauth",
                credential_ref=credential_ref,
                verified_at=verified_text,
            )
            assert inserted["verified_at"] == verified_at.isoformat().replace("+00:00", "Z")

            updated_verified = verified_at + timedelta(minutes=5)
            updated = await store.upsert_git_repository_binding(
                "owner-1",
                normalized_repo="github.com/alice/astrbot_plugin_demo",
                repo_url="https://github.com/alice/astrbot_plugin_demo",
                provider="github",
                instance_url="https://github.com",
                repo_external_id="21",
                owner_id="10",
                permission_level="write",
                verification_method="oauth",
                credential_ref=credential_ref,
                verified_at=updated_verified.isoformat(),
            )
            assert updated["verified_at"] == updated_verified.isoformat().replace("+00:00", "Z")

            email_verified = datetime.now(UTC).replace(microsecond=111111)
            created_user = await store.create_email_user(
                username="pg_email_user",
                email="pg_email@example.com",
                password_hash="hash-1",
                email_verified_at=email_verified.isoformat().replace("+00:00", "Z"),
            )
            assert created_user["email_verified_at"] == email_verified.isoformat().replace(
                "+00:00", "Z"
            )

            email_verified_2 = email_verified + timedelta(minutes=1)
            updated_email = await store.update_user_email(
                created_user["id"],
                "pg_email_2@example.com",
                email_verified_at=email_verified_2.isoformat().replace("+00:00", "Z"),
            )
            assert updated_email is not None
            assert updated_email["email_verified_at"] == email_verified_2.isoformat().replace(
                "+00:00", "Z"
            )

            mute_until = email_verified + timedelta(days=1)
            muted = await store.mute_user(
                created_user["id"],
                mute_until.isoformat(),
                "owner-1",
                "spam",
            )
            assert muted is not None
            assert muted["muted_until"] == mute_until.isoformat().replace("+00:00", "Z")

            stored_cred = await store.get_oauth_credential(credential_ref)
            assert stored_cred is not None
            assert (
                await store.invalidate_oauth_credential(
                    identity["id"],
                    credential_ref,
                    expected_ciphertext="stale-ciphertext",
                )
                is False
            )
            assert await store.get_oauth_credential(credential_ref) is not None
            assert (
                await store.invalidate_oauth_credential(
                    identity["id"],
                    credential_ref,
                    expected_ciphertext=str(stored_cred["ciphertext"]),
                )
                is True
            )
            assert await store.get_oauth_credential(credential_ref) is None
            identities_after = await store.list_oauth_identities("owner-1")
            updated_identity = next(
                item for item in identities_after if item["id"] == identity["id"]
            )
            assert updated_identity["rotation_status"] == "reauth_required"

    asyncio.run(scenario())


def test_pg_store_converts_iso_timestamps_for_asyncpg() -> None:
    """Verify PgRedisMarketStore passes datetime instances (not str) to ::timestamptz queries."""

    class FakeTransaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeConnection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []
            self.active_binding: dict[str, object] | None = None

        def transaction(self) -> FakeTransaction:
            return FakeTransaction()

        async def execute(self, query: str, *args: object) -> str:
            self.calls.append((query, args))
            return "OK"

        async def fetchval(self, query: str, *args: object) -> object:
            self.calls.append((query, args))
            return 0

        async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
            self.calls.append((query, args))
            if "FROM market_git_repository_bindings" in query and "FOR UPDATE" in query:
                return self.active_binding
            if "INTO market_git_repository_bindings" in query:
                self.active_binding = {
                    "id": args[0],
                    "user_id": args[1],
                    "normalized_repo": args[2],
                    "repo_url": args[3],
                    "provider": args[4],
                    "instance_url": args[5],
                    "repo_external_id": args[6],
                    "owner_id": args[7],
                    "permission_level": args[8],
                    "verification_method": args[9],
                    "credential_ref": args[10],
                    "binding_generation": args[11],
                    "verified_at": args[12],
                }
                return self.active_binding
            if "UPDATE market_git_repository_bindings" in query:
                assert self.active_binding is not None
                self.active_binding = {
                    **self.active_binding,
                    "binding_generation": args[9],
                    "verified_at": args[10],
                }
                return self.active_binding
            return {
                "id": "user-1",
                "github_id": None,
                "github_login": "",
                "internal_username": "alice",
                "public_username": "alice",
                "email": "alice@example.com",
                "email_verified_at": args[3] if "INSERT INTO market_users" in query else args[2],
                "session_version": 1,
                "password_hash": "hash",
                "auth_source": "email",
                "github_name": "alice",
                "avatar_url": "",
                "role": "user",
                "muted_until": args[1] if "SET muted_until" in query else None,
            }

    class FakeAcquire:
        def __init__(self, conn: FakeConnection) -> None:
            self._conn = conn

        async def __aenter__(self) -> FakeConnection:
            return self._conn

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakePool:
        def __init__(self, conn: FakeConnection) -> None:
            self.conn = conn

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.conn)

        async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
            return await self.conn.fetchrow(query, *args)

    class FakeRedis:
        async def delete(self, key: str) -> int:
            return 1

    async def scenario() -> None:
        conn = FakeConnection()
        store = PgRedisMarketStore("postgresql://unused", "redis://unused", 60)
        store.pool = FakePool(conn)
        store.redis = FakeRedis()

        verified_dt = datetime(2026, 11, 26, 23, 2, 7, 153498, tzinfo=UTC)
        await store.upsert_git_repository_binding(
            "user-1",
            normalized_repo="github.com/piexian/astrbot_plugin_demo",
            repo_url="https://github.com/piexian/astrbot_plugin_demo",
            provider="github",
            instance_url="https://github.com",
            repo_external_id="21",
            owner_id="10",
            verified_at="2026-11-26T23:02:07.153498+00:00",
        )
        insert_call = next(
            args
            for query, args in conn.calls
            if "INSERT INTO market_git_repository_bindings" in query
        )
        assert isinstance(insert_call[12], datetime)
        assert insert_call[12] == verified_dt

        await store.upsert_git_repository_binding(
            "user-1",
            normalized_repo="github.com/piexian/astrbot_plugin_demo",
            repo_url="https://github.com/piexian/astrbot_plugin_demo",
            provider="github",
            instance_url="https://github.com",
            repo_external_id="21",
            owner_id="10",
            verified_at="2026-11-26T23:05:00Z",
        )
        update_call = next(
            args for query, args in conn.calls if "UPDATE market_git_repository_bindings" in query
        )
        assert isinstance(update_call[10], datetime)
        assert update_call[10] == datetime(2026, 11, 26, 23, 5, 0, tzinfo=UTC)

        await store.create_email_user(
            username="alice",
            email="alice@example.com",
            password_hash="hash",
            email_verified_at="2026-11-26T23:10:00Z",
        )
        create_user_call = next(
            args for query, args in conn.calls if "INSERT INTO market_users" in query
        )
        assert isinstance(create_user_call[3], datetime)
        assert create_user_call[3] == datetime(2026, 11, 26, 23, 10, 0, tzinfo=UTC)

        await store.set_user_password(
            "user-1",
            "hash-2",
            verify_email_at="2026-11-27T07:11:00+08:00",
        )
        set_pw_call = next(args for query, args in conn.calls if "SET password_hash = $2" in query)
        assert isinstance(set_pw_call[3], datetime)
        assert set_pw_call[3] == datetime(2026, 11, 26, 23, 11, 0, tzinfo=UTC)

        await store.update_user_email(
            "user-1",
            "alice2@example.com",
            email_verified_at="2026-11-26T23:12:00Z",
        )
        update_email_call = next(args for query, args in conn.calls if "SET email = $2" in query)
        assert isinstance(update_email_call[2], datetime)
        assert update_email_call[2] == datetime(2026, 11, 26, 23, 12, 0, tzinfo=UTC)

        await store.mute_user(
            "user-1",
            "2026-11-27T23:12:00+00:00",
            "admin-1",
            "spam",
        )
        mute_call = next(args for query, args in conn.calls if "SET muted_until" in query)
        assert isinstance(mute_call[1], datetime)
        assert mute_call[1] == datetime(2026, 11, 27, 23, 12, 0, tzinfo=UTC)

        with pytest.raises(ValueError, match="Invalid OAuth timestamp"):
            await store.upsert_git_repository_binding(
                "user-1",
                normalized_repo="github.com/piexian/astrbot_plugin_demo",
                repo_url="https://github.com/piexian/astrbot_plugin_demo",
                provider="github",
                verified_at="not-a-timestamp",
            )

    asyncio.run(scenario())


def test_pg_store_invalidate_oauth_credential_is_atomic_and_checks_ciphertext() -> None:
    class FakeTransaction:
        def __init__(self, conn: FakeConnection) -> None:
            self._conn = conn

        async def __aenter__(self) -> None:
            self._conn.tx_depth += 1

        async def __aexit__(self, *args: object) -> None:
            self._conn.tx_depth -= 1

    class FakeConnection:
        def __init__(self, current_ciphertext: str) -> None:
            self.current_ciphertext = current_ciphertext
            self.tx_depth = 0
            self.calls: list[tuple[int, str, tuple[object, ...]]] = []

        def transaction(self) -> FakeTransaction:
            return FakeTransaction(self)

        async def fetchval(self, query: str, *args: object) -> object:
            self.calls.append((self.tx_depth, query, args))
            if "DELETE FROM market_oauth_credentials" in query:
                ref, expected = args
                if self.current_ciphertext == expected:
                    self.current_ciphertext = ""
                    return ref
                return None
            return None

        async def execute(self, query: str, *args: object) -> str:
            self.calls.append((self.tx_depth, query, args))
            return "UPDATE 1"

    class FakeAcquire:
        def __init__(self, conn: FakeConnection) -> None:
            self._conn = conn

        async def __aenter__(self) -> FakeConnection:
            return self._conn

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakePool:
        def __init__(self, conn: FakeConnection) -> None:
            self.conn = conn

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.conn)

    async def scenario() -> None:
        conn = FakeConnection("cipher-v2")
        store = PgRedisMarketStore("postgresql://unused", "redis://unused", 60)
        store.pool = FakePool(conn)

        # Mismatched ciphertext leaves credential untouched and skips status update.
        assert (
            await store.invalidate_oauth_credential(
                "oauth-1",
                "cred-1",
                expected_ciphertext="cipher-v1",
            )
            is False
        )
        assert conn.current_ciphertext == "cipher-v2"
        assert len(conn.calls) == 1
        assert conn.calls[0][0] == 1
        assert "DELETE FROM market_oauth_credentials" in conn.calls[0][1]

        # Matching ciphertext deletes and updates rotation_status inside the same transaction.
        assert (
            await store.invalidate_oauth_credential(
                "oauth-1",
                "cred-1",
                expected_ciphertext="cipher-v2",
            )
            is True
        )
        assert conn.current_ciphertext == ""
        assert len(conn.calls) == 3
        assert conn.calls[1][0] == 1
        assert "DELETE FROM market_oauth_credentials" in conn.calls[1][1]
        assert conn.calls[2][0] == 1
        assert "UPDATE market_oauth_identities" in conn.calls[2][1]
        assert conn.calls[2][2] == ("oauth-1", "reauth_required", "cred-1")

    asyncio.run(scenario())
