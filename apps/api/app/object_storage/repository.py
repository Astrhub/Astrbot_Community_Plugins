"""Persistent storage repositories for profiles, bindings, objects, aliases, and migrations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Protocol

from .models import (
    StorageBackend,
    StorageBinding,
    StorageMigration,
    StorageMigrationItem,
    StorageMigrationItemStatus,
    StorageMigrationStatus,
    StorageObjectRecord,
    StorageObjectStatus,
    StorageProfile,
    StorageProfileStatus,
    StoragePurpose,
    StorageUrlAlias,
    utc_now_iso,
)


class ObjectStorageRepository(Protocol):
    async def save_secret(self, secret_ref: str, *, profile_id: str, ciphertext: str) -> None: ...

    async def get_secret(self, secret_ref: str) -> tuple[str, str] | None: ...

    async def delete_secret(self, secret_ref: str) -> bool: ...

    async def upsert_profile(self, profile: StorageProfile) -> StorageProfile: ...

    async def get_profile(self, profile_id: str) -> StorageProfile | None: ...

    async def list_profiles(self, *, include_removed: bool = True) -> list[StorageProfile]: ...

    async def upsert_binding(self, binding: StorageBinding) -> StorageBinding: ...

    async def get_binding(self, purpose: StoragePurpose) -> StorageBinding | None: ...

    async def list_bindings(self) -> list[StorageBinding]: ...

    async def upsert_object(self, record: StorageObjectRecord) -> StorageObjectRecord: ...

    async def get_object(self, object_id: str) -> StorageObjectRecord | None: ...

    async def delete_object_record(self, object_id: str) -> None: ...

    async def find_active_object(
        self,
        *,
        profile_id: str,
        namespace: str,
        key: str,
    ) -> StorageObjectRecord | None: ...

    async def find_active_object_by_purpose_and_key(
        self,
        *,
        purpose: StoragePurpose,
        key: str,
    ) -> StorageObjectRecord | None: ...

    async def list_objects(
        self,
        *,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        status: StorageObjectStatus | None = None,
    ) -> list[StorageObjectRecord]: ...

    async def upsert_url_alias(self, alias: StorageUrlAlias) -> StorageUrlAlias: ...

    async def get_url_alias(self, alias_path: str) -> StorageUrlAlias | None: ...

    async def list_url_aliases(self, *, object_id: str = "") -> list[StorageUrlAlias]: ...

    async def create_migration(
        self,
        migration: StorageMigration,
        items: list[StorageMigrationItem],
    ) -> StorageMigration: ...

    async def update_migration(self, migration: StorageMigration) -> StorageMigration: ...

    async def get_migration(self, migration_id: str) -> StorageMigration | None: ...

    async def list_migrations(
        self,
        *,
        status: StorageMigrationStatus | None = None,
        limit: int = 100,
    ) -> list[StorageMigration]: ...

    async def list_migration_items(
        self,
        migration_id: str,
        *,
        status: StorageMigrationItemStatus | None = None,
    ) -> list[StorageMigrationItem]: ...

    async def update_migration_item(self, item: StorageMigrationItem) -> StorageMigrationItem: ...

    async def reset_failed_migration_items(self, migration_id: str) -> int: ...


class LocalObjectStorageRepository:
    """Repository persisting object storage state in InMemoryMarketStore / LocalFileMarketStore."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._ensure_bucket()

    def _ensure_bucket(self) -> dict[str, Any]:
        bucket = self.store.state.setdefault("objectStorage", {})
        bucket.setdefault("secrets", {})
        bucket.setdefault("profiles", {})
        bucket.setdefault("bindings", {})
        bucket.setdefault("objects", {})
        bucket.setdefault("urlAliases", {})
        bucket.setdefault("migrations", {})
        bucket.setdefault("migrationItems", {})
        return bucket

    def _persist(self) -> None:
        persist = getattr(self.store, "persist_object_storage", None)
        if callable(persist):
            persist()

    async def save_secret(self, secret_ref: str, *, profile_id: str, ciphertext: str) -> None:
        bucket = self._ensure_bucket()
        now = utc_now_iso()
        existing = bucket["secrets"].get(secret_ref) or {}
        bucket["secrets"][secret_ref] = {
            "secret_ref": secret_ref,
            "profile_id": profile_id,
            "ciphertext": ciphertext,
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
        }
        self._persist()

    async def get_secret(self, secret_ref: str) -> tuple[str, str] | None:
        row = self._ensure_bucket()["secrets"].get(secret_ref)
        if not row:
            return None
        return str(row["profile_id"]), str(row["ciphertext"])

    async def delete_secret(self, secret_ref: str) -> bool:
        removed = self._ensure_bucket()["secrets"].pop(secret_ref, None) is not None
        if removed:
            self._persist()
        return removed

    async def upsert_profile(self, profile: StorageProfile) -> StorageProfile:
        bucket = self._ensure_bucket()
        now = utc_now_iso()
        existing = bucket["profiles"].get(profile.id) or {}
        stored = StorageProfile(
            id=profile.id,
            name=profile.name,
            backend=profile.backend,
            status=profile.status,
            config=deepcopy(dict(profile.config)),
            secret_ref=profile.secret_ref,
            legacy=profile.legacy,
            health_status=profile.health_status,
            health_message=profile.health_message,
            last_checked_at=profile.last_checked_at,
            created_by=profile.created_by or str(existing.get("created_by") or ""),
            updated_by=profile.updated_by,
            created_at=profile.created_at or str(existing.get("created_at") or now),
            updated_at=profile.updated_at or now,
        )
        bucket["profiles"][stored.id] = _profile_to_raw(stored)
        self._persist()
        return stored

    async def get_profile(self, profile_id: str) -> StorageProfile | None:
        raw = self._ensure_bucket()["profiles"].get(profile_id)
        return _raw_to_profile(raw) if raw else None

    async def list_profiles(self, *, include_removed: bool = True) -> list[StorageProfile]:
        profiles = [_raw_to_profile(raw) for raw in self._ensure_bucket()["profiles"].values()]
        if not include_removed:
            profiles = [p for p in profiles if p.status != StorageProfileStatus.REMOVED]
        profiles.sort(key=lambda item: (item.backend.value, item.created_at, item.id))
        return profiles

    async def upsert_binding(self, binding: StorageBinding) -> StorageBinding:
        bucket = self._ensure_bucket()
        stored = StorageBinding(
            purpose=binding.purpose,
            profile_id=binding.profile_id,
            generation=binding.generation,
            updated_by=binding.updated_by,
            updated_at=binding.updated_at or utc_now_iso(),
        )
        bucket["bindings"][stored.purpose.value] = {
            "purpose": stored.purpose.value,
            "profile_id": stored.profile_id,
            "generation": stored.generation,
            "updated_by": stored.updated_by,
            "updated_at": stored.updated_at,
        }
        self._persist()
        return stored

    async def get_binding(self, purpose: StoragePurpose) -> StorageBinding | None:
        raw = self._ensure_bucket()["bindings"].get(purpose.value)
        return _raw_to_binding(raw) if raw else None

    async def list_bindings(self) -> list[StorageBinding]:
        bindings = [_raw_to_binding(raw) for raw in self._ensure_bucket()["bindings"].values()]
        bindings.sort(key=lambda item: item.purpose.value)
        return bindings

    async def upsert_object(self, record: StorageObjectRecord) -> StorageObjectRecord:
        bucket = self._ensure_bucket()
        now = utc_now_iso()
        existing = bucket["objects"].get(record.id) or {}
        stored = StorageObjectRecord(
            id=record.id,
            purpose=record.purpose,
            profile_id=record.profile_id,
            backend=record.backend,
            namespace=record.namespace,
            key=record.key,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            content_type=record.content_type,
            status=record.status,
            public_url=record.public_url,
            owner_type=record.owner_type,
            owner_id=record.owner_id,
            created_at=record.created_at or str(existing.get("created_at") or now),
            updated_at=record.updated_at or now,
        )
        bucket["objects"][stored.id] = _object_to_raw(stored)
        self._persist()
        return stored

    async def get_object(self, object_id: str) -> StorageObjectRecord | None:
        raw = self._ensure_bucket()["objects"].get(object_id)
        return _raw_to_object(raw) if raw else None

    async def delete_object_record(self, object_id: str) -> None:
        if self._ensure_bucket()["objects"].pop(object_id, None) is not None:
            self._persist()

    async def find_active_object(
        self,
        *,
        profile_id: str,
        namespace: str,
        key: str,
    ) -> StorageObjectRecord | None:
        for raw in self._ensure_bucket()["objects"].values():
            if (
                raw.get("status") != StorageObjectStatus.DELETED.value
                and raw.get("profile_id") == profile_id
                and raw.get("namespace") == namespace
                and raw.get("object_key") == key
            ):
                return _raw_to_object(raw)
        return None

    async def find_active_object_by_purpose_and_key(
        self,
        *,
        purpose: StoragePurpose,
        key: str,
    ) -> StorageObjectRecord | None:
        candidates = [
            _raw_to_object(raw)
            for raw in self._ensure_bucket()["objects"].values()
            if raw.get("status") == StorageObjectStatus.ACTIVE.value
            and raw.get("purpose") == purpose.value
            and raw.get("object_key") == key
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.updated_at, reverse=True)
        return candidates[0]

    async def list_objects(
        self,
        *,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        status: StorageObjectStatus | None = None,
    ) -> list[StorageObjectRecord]:
        results: list[StorageObjectRecord] = []
        for raw in self._ensure_bucket()["objects"].values():
            if purpose is not None and raw.get("purpose") != purpose.value:
                continue
            if profile_id and raw.get("profile_id") != profile_id:
                continue
            if status is not None and raw.get("status") != status.value:
                continue
            results.append(_raw_to_object(raw))
        results.sort(key=lambda item: (item.created_at, item.id))
        return results

    async def upsert_url_alias(self, alias: StorageUrlAlias) -> StorageUrlAlias:
        bucket = self._ensure_bucket()
        stored = StorageUrlAlias(
            id=alias.id,
            alias_path=alias.alias_path,
            object_id=alias.object_id,
            purpose=alias.purpose,
            created_at=alias.created_at or utc_now_iso(),
        )
        bucket["urlAliases"][stored.alias_path] = {
            "id": stored.id,
            "alias_path": stored.alias_path,
            "object_id": stored.object_id,
            "purpose": stored.purpose.value,
            "created_at": stored.created_at,
        }
        self._persist()
        return stored

    async def get_url_alias(self, alias_path: str) -> StorageUrlAlias | None:
        raw = self._ensure_bucket()["urlAliases"].get(alias_path)
        return _raw_to_alias(raw) if raw else None

    async def list_url_aliases(self, *, object_id: str = "") -> list[StorageUrlAlias]:
        aliases = [
            _raw_to_alias(raw)
            for raw in self._ensure_bucket()["urlAliases"].values()
            if not object_id or raw.get("object_id") == object_id
        ]
        aliases.sort(key=lambda item: item.created_at)
        return aliases

    async def create_migration(
        self,
        migration: StorageMigration,
        items: list[StorageMigrationItem],
    ) -> StorageMigration:
        bucket = self._ensure_bucket()
        bucket["migrations"][migration.id] = _migration_to_raw(migration)
        for item in items:
            bucket["migrationItems"][item.id] = _migration_item_to_raw(item)
        self._persist()
        return migration

    async def update_migration(self, migration: StorageMigration) -> StorageMigration:
        bucket = self._ensure_bucket()
        bucket["migrations"][migration.id] = _migration_to_raw(migration)
        self._persist()
        return migration

    async def get_migration(self, migration_id: str) -> StorageMigration | None:
        raw = self._ensure_bucket()["migrations"].get(migration_id)
        return _raw_to_migration(raw) if raw else None

    async def list_migrations(
        self,
        *,
        status: StorageMigrationStatus | None = None,
        limit: int = 100,
    ) -> list[StorageMigration]:
        items = [
            _raw_to_migration(raw)
            for raw in self._ensure_bucket()["migrations"].values()
            if status is None or raw.get("status") == status.value
        ]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[: max(1, limit)]

    async def list_migration_items(
        self,
        migration_id: str,
        *,
        status: StorageMigrationItemStatus | None = None,
    ) -> list[StorageMigrationItem]:
        items = [
            _raw_to_migration_item(raw)
            for raw in self._ensure_bucket()["migrationItems"].values()
            if raw.get("migration_id") == migration_id
            and (status is None or raw.get("status") == status.value)
        ]
        items.sort(key=lambda item: (item.created_at, item.id))
        return items

    async def update_migration_item(self, item: StorageMigrationItem) -> StorageMigrationItem:
        bucket = self._ensure_bucket()
        bucket["migrationItems"][item.id] = _migration_item_to_raw(item)
        self._persist()
        return item

    async def reset_failed_migration_items(self, migration_id: str) -> int:
        bucket = self._ensure_bucket()
        now = utc_now_iso()
        reset_count = 0
        for raw in bucket["migrationItems"].values():
            if (
                raw.get("migration_id") == migration_id
                and raw.get("status") == StorageMigrationItemStatus.FAILED.value
            ):
                raw["status"] = StorageMigrationItemStatus.PENDING.value
                raw["last_error"] = ""
                raw["updated_at"] = now
                reset_count += 1
        if reset_count:
            self._persist()
        return reset_count


class PgObjectStorageRepository:
    """PostgreSQL repository backed by migration 20261125_013_object_storage.sql."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def _pool(self) -> Any:
        return self.store._pool()

    async def save_secret(self, secret_ref: str, *, profile_id: str, ciphertext: str) -> None:
        await self._pool().execute(
            """
            INSERT INTO market_storage_secrets (secret_ref, profile_id, ciphertext)
            VALUES ($1, $2, $3)
            ON CONFLICT (secret_ref)
            DO UPDATE SET profile_id = EXCLUDED.profile_id,
                          ciphertext = EXCLUDED.ciphertext,
                          updated_at = now()
            """,
            secret_ref,
            profile_id,
            ciphertext,
        )

    async def get_secret(self, secret_ref: str) -> tuple[str, str] | None:
        row = await self._pool().fetchrow(
            "SELECT profile_id, ciphertext FROM market_storage_secrets WHERE secret_ref = $1",
            secret_ref,
        )
        if row is None:
            return None
        return str(row["profile_id"]), str(row["ciphertext"])

    async def delete_secret(self, secret_ref: str) -> bool:
        result = await self._pool().execute(
            "DELETE FROM market_storage_secrets WHERE secret_ref = $1",
            secret_ref,
        )
        return not str(result).endswith(" 0")

    async def upsert_profile(self, profile: StorageProfile) -> StorageProfile:
        row = await self._pool().fetchrow(
            """
            INSERT INTO market_storage_profiles (
                id, name, backend, status, config, secret_ref, legacy,
                health_status, health_message, last_checked_at, created_by, updated_by
            ) VALUES (
                $1, $2, $3, $4, $5::jsonb, $6, $7,
                $8, $9, NULLIF($10, '')::timestamptz, $11, $12
            )
            ON CONFLICT (id)
            DO UPDATE SET name = EXCLUDED.name,
                          backend = EXCLUDED.backend,
                          status = EXCLUDED.status,
                          config = EXCLUDED.config,
                          secret_ref = EXCLUDED.secret_ref,
                          legacy = EXCLUDED.legacy,
                          health_status = EXCLUDED.health_status,
                          health_message = EXCLUDED.health_message,
                          last_checked_at = EXCLUDED.last_checked_at,
                          updated_by = EXCLUDED.updated_by,
                          updated_at = now()
            RETURNING *
            """,
            profile.id,
            profile.name,
            profile.backend.value,
            profile.status.value,
            json.dumps(dict(profile.config)),
            profile.secret_ref,
            profile.legacy,
            profile.health_status,
            profile.health_message,
            profile.last_checked_at,
            profile.created_by,
            profile.updated_by,
        )
        return _raw_to_profile(dict(row))

    async def get_profile(self, profile_id: str) -> StorageProfile | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_storage_profiles WHERE id = $1",
            profile_id,
        )
        return _raw_to_profile(dict(row)) if row is not None else None

    async def list_profiles(self, *, include_removed: bool = True) -> list[StorageProfile]:
        sql = "SELECT * FROM market_storage_profiles"
        if not include_removed:
            sql += " WHERE status <> 'removed'"
        sql += " ORDER BY backend ASC, created_at ASC, id ASC"
        rows = await self._pool().fetch(sql)
        return [_raw_to_profile(dict(row)) for row in rows]

    async def upsert_binding(self, binding: StorageBinding) -> StorageBinding:
        row = await self._pool().fetchrow(
            """
            INSERT INTO market_storage_bindings (purpose, profile_id, generation, updated_by)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (purpose)
            DO UPDATE SET profile_id = EXCLUDED.profile_id,
                          generation = EXCLUDED.generation,
                          updated_by = EXCLUDED.updated_by,
                          updated_at = now()
            RETURNING *
            """,
            binding.purpose.value,
            binding.profile_id,
            binding.generation,
            binding.updated_by,
        )
        return _raw_to_binding(dict(row))

    async def get_binding(self, purpose: StoragePurpose) -> StorageBinding | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_storage_bindings WHERE purpose = $1",
            purpose.value,
        )
        return _raw_to_binding(dict(row)) if row is not None else None

    async def list_bindings(self) -> list[StorageBinding]:
        rows = await self._pool().fetch(
            "SELECT * FROM market_storage_bindings ORDER BY purpose ASC"
        )
        return [_raw_to_binding(dict(row)) for row in rows]

    async def upsert_object(self, record: StorageObjectRecord) -> StorageObjectRecord:
        row = await self._pool().fetchrow(
            """
            INSERT INTO market_storage_objects (
                id, purpose, profile_id, backend, namespace, object_key,
                size_bytes, sha256, content_type, status, public_url, owner_type, owner_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (id)
            DO UPDATE SET purpose = EXCLUDED.purpose,
                          profile_id = EXCLUDED.profile_id,
                          backend = EXCLUDED.backend,
                          namespace = EXCLUDED.namespace,
                          object_key = EXCLUDED.object_key,
                          size_bytes = EXCLUDED.size_bytes,
                          sha256 = EXCLUDED.sha256,
                          content_type = EXCLUDED.content_type,
                          status = EXCLUDED.status,
                          public_url = EXCLUDED.public_url,
                          owner_type = EXCLUDED.owner_type,
                          owner_id = EXCLUDED.owner_id,
                          updated_at = now()
            RETURNING *
            """,
            record.id,
            record.purpose.value,
            record.profile_id,
            record.backend,
            record.namespace,
            record.key,
            record.size_bytes,
            record.sha256,
            record.content_type,
            record.status.value,
            record.public_url,
            record.owner_type,
            record.owner_id,
        )
        return _raw_to_object(dict(row))

    async def get_object(self, object_id: str) -> StorageObjectRecord | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_storage_objects WHERE id = $1",
            object_id,
        )
        return _raw_to_object(dict(row)) if row is not None else None

    async def delete_object_record(self, object_id: str) -> None:
        await self._pool().execute("DELETE FROM market_storage_objects WHERE id = $1", object_id)

    async def find_active_object(
        self,
        *,
        profile_id: str,
        namespace: str,
        key: str,
    ) -> StorageObjectRecord | None:
        row = await self._pool().fetchrow(
            """
            SELECT * FROM market_storage_objects
             WHERE profile_id = $1
               AND namespace = $2
               AND object_key = $3
               AND status <> 'deleted'
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            profile_id,
            namespace,
            key,
        )
        return _raw_to_object(dict(row)) if row is not None else None

    async def find_active_object_by_purpose_and_key(
        self,
        *,
        purpose: StoragePurpose,
        key: str,
    ) -> StorageObjectRecord | None:
        row = await self._pool().fetchrow(
            """
            SELECT * FROM market_storage_objects
             WHERE purpose = $1 AND object_key = $2 AND status = 'active'
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            purpose.value,
            key,
        )
        return _raw_to_object(dict(row)) if row is not None else None

    async def list_objects(
        self,
        *,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        status: StorageObjectStatus | None = None,
    ) -> list[StorageObjectRecord]:
        clauses: list[str] = []
        args: list[Any] = []
        if purpose is not None:
            args.append(purpose.value)
            clauses.append(f"purpose = ${len(args)}")
        if profile_id:
            args.append(profile_id)
            clauses.append(f"profile_id = ${len(args)}")
        if status is not None:
            args.append(status.value)
            clauses.append(f"status = ${len(args)}")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._pool().fetch(
            f"SELECT * FROM market_storage_objects{where} ORDER BY created_at ASC, id ASC",
            *args,
        )
        return [_raw_to_object(dict(row)) for row in rows]

    async def upsert_url_alias(self, alias: StorageUrlAlias) -> StorageUrlAlias:
        row = await self._pool().fetchrow(
            """
            INSERT INTO market_storage_url_aliases (id, alias_path, object_id, purpose)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (alias_path)
            DO UPDATE SET object_id = EXCLUDED.object_id,
                          purpose = EXCLUDED.purpose
            RETURNING *
            """,
            alias.id,
            alias.alias_path,
            alias.object_id,
            alias.purpose.value,
        )
        return _raw_to_alias(dict(row))

    async def get_url_alias(self, alias_path: str) -> StorageUrlAlias | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_storage_url_aliases WHERE alias_path = $1",
            alias_path,
        )
        return _raw_to_alias(dict(row)) if row is not None else None

    async def list_url_aliases(self, *, object_id: str = "") -> list[StorageUrlAlias]:
        if object_id:
            rows = await self._pool().fetch(
                "SELECT * FROM market_storage_url_aliases WHERE object_id = $1 ORDER BY created_at ASC",
                object_id,
            )
        else:
            rows = await self._pool().fetch(
                "SELECT * FROM market_storage_url_aliases ORDER BY created_at ASC"
            )
        return [_raw_to_alias(dict(row)) for row in rows]

    async def create_migration(
        self,
        migration: StorageMigration,
        items: list[StorageMigrationItem],
    ) -> StorageMigration:
        async with self._pool().acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO market_storage_migrations (
                        id, purpose, source_profile_id, target_profile_id, binding_generation,
                        status, total_count, completed_count, failed_count, skipped_count,
                        unknown_objects, last_error, created_by
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13
                    )
                    RETURNING *
                    """,
                    migration.id,
                    migration.purpose.value,
                    migration.source_profile_id,
                    migration.target_profile_id,
                    migration.binding_generation,
                    migration.status.value,
                    migration.total_count,
                    migration.completed_count,
                    migration.failed_count,
                    migration.skipped_count,
                    json.dumps([dict(entry) for entry in migration.unknown_objects]),
                    migration.last_error,
                    migration.created_by,
                )
                for item in items:
                    await conn.execute(
                        """
                        INSERT INTO market_storage_migration_items (
                            id, migration_id, object_id, source_profile_id, source_namespace,
                            source_key, source_public_url, target_profile_id, target_namespace,
                            target_key, target_public_url, expected_sha256, status,
                            attempts, last_error
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
                        )
                        """,
                        item.id,
                        item.migration_id,
                        item.object_id,
                        item.source_profile_id,
                        item.source_namespace,
                        item.source_key,
                        item.source_public_url,
                        item.target_profile_id,
                        item.target_namespace,
                        item.target_key,
                        item.target_public_url,
                        item.expected_sha256,
                        item.status.value,
                        item.attempts,
                        item.last_error,
                    )
        return _raw_to_migration(dict(row))

    async def update_migration(self, migration: StorageMigration) -> StorageMigration:
        row = await self._pool().fetchrow(
            """
            UPDATE market_storage_migrations
               SET status = $2,
                   total_count = $3,
                   completed_count = $4,
                   failed_count = $5,
                   skipped_count = $6,
                   unknown_objects = $7::jsonb,
                   last_error = $8,
                   completed_at = NULLIF($9, '')::timestamptz,
                   updated_at = now()
             WHERE id = $1
            RETURNING *
            """,
            migration.id,
            migration.status.value,
            migration.total_count,
            migration.completed_count,
            migration.failed_count,
            migration.skipped_count,
            json.dumps([dict(entry) for entry in migration.unknown_objects]),
            migration.last_error,
            migration.completed_at,
        )
        return _raw_to_migration(dict(row))

    async def get_migration(self, migration_id: str) -> StorageMigration | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_storage_migrations WHERE id = $1",
            migration_id,
        )
        return _raw_to_migration(dict(row)) if row is not None else None

    async def list_migrations(
        self,
        *,
        status: StorageMigrationStatus | None = None,
        limit: int = 100,
    ) -> list[StorageMigration]:
        if status is not None:
            rows = await self._pool().fetch(
                """
                SELECT * FROM market_storage_migrations
                 WHERE status = $1
                 ORDER BY created_at DESC
                 LIMIT $2
                """,
                status.value,
                max(1, limit),
            )
        else:
            rows = await self._pool().fetch(
                "SELECT * FROM market_storage_migrations ORDER BY created_at DESC LIMIT $1",
                max(1, limit),
            )
        return [_raw_to_migration(dict(row)) for row in rows]

    async def list_migration_items(
        self,
        migration_id: str,
        *,
        status: StorageMigrationItemStatus | None = None,
    ) -> list[StorageMigrationItem]:
        if status is not None:
            rows = await self._pool().fetch(
                """
                SELECT * FROM market_storage_migration_items
                 WHERE migration_id = $1 AND status = $2
                 ORDER BY created_at ASC, id ASC
                """,
                migration_id,
                status.value,
            )
        else:
            rows = await self._pool().fetch(
                """
                SELECT * FROM market_storage_migration_items
                 WHERE migration_id = $1
                 ORDER BY created_at ASC, id ASC
                """,
                migration_id,
            )
        return [_raw_to_migration_item(dict(row)) for row in rows]

    async def update_migration_item(self, item: StorageMigrationItem) -> StorageMigrationItem:
        row = await self._pool().fetchrow(
            """
            UPDATE market_storage_migration_items
               SET target_key = $2,
                   target_public_url = $3,
                   status = $4,
                   attempts = $5,
                   last_error = $6,
                   updated_at = now()
             WHERE id = $1
            RETURNING *
            """,
            item.id,
            item.target_key,
            item.target_public_url,
            item.status.value,
            item.attempts,
            item.last_error,
        )
        return _raw_to_migration_item(dict(row))

    async def reset_failed_migration_items(self, migration_id: str) -> int:
        rows = await self._pool().fetch(
            """
            UPDATE market_storage_migration_items
               SET status = 'pending',
                   last_error = '',
                   updated_at = now()
             WHERE migration_id = $1 AND status = 'failed'
            RETURNING id
            """,
            migration_id,
        )
        return len(rows)


def create_object_storage_repository(store: Any) -> ObjectStorageRepository:
    if hasattr(store, "_pool"):
        return PgObjectStorageRepository(store)
    return LocalObjectStorageRepository(store)


def _iso_str(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return str(value.isoformat()).replace("+00:00", "Z")
    return str(value)


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value or json.dumps(default))
    return value


def _profile_to_raw(profile: StorageProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "name": profile.name,
        "backend": profile.backend.value,
        "status": profile.status.value,
        "config": deepcopy(dict(profile.config)),
        "secret_ref": profile.secret_ref,
        "legacy": profile.legacy,
        "health_status": profile.health_status,
        "health_message": profile.health_message,
        "last_checked_at": profile.last_checked_at,
        "created_by": profile.created_by,
        "updated_by": profile.updated_by,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def _raw_to_profile(raw: Mapping[str, Any]) -> StorageProfile:
    return StorageProfile(
        id=str(raw["id"]),
        name=str(raw["name"]),
        backend=StorageBackend(str(raw["backend"])),
        status=StorageProfileStatus(str(raw.get("status") or "active")),
        config=dict(_json_value(raw.get("config"), {})),
        secret_ref=str(raw.get("secret_ref") or ""),
        legacy=bool(raw.get("legacy", False)),
        health_status=str(raw.get("health_status") or "unknown"),
        health_message=str(raw.get("health_message") or ""),
        last_checked_at=_iso_str(raw.get("last_checked_at")),
        created_by=str(raw.get("created_by") or ""),
        updated_by=str(raw.get("updated_by") or ""),
        created_at=_iso_str(raw.get("created_at")),
        updated_at=_iso_str(raw.get("updated_at")),
    )


def _raw_to_binding(raw: Mapping[str, Any]) -> StorageBinding:
    return StorageBinding(
        purpose=StoragePurpose(str(raw["purpose"])),
        profile_id=str(raw["profile_id"]),
        generation=int(raw.get("generation") or 1),
        updated_by=str(raw.get("updated_by") or ""),
        updated_at=_iso_str(raw.get("updated_at")),
    )


def _object_to_raw(record: StorageObjectRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "purpose": record.purpose.value,
        "profile_id": record.profile_id,
        "backend": record.backend,
        "namespace": record.namespace,
        "object_key": record.key,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "content_type": record.content_type,
        "status": record.status.value,
        "public_url": record.public_url,
        "owner_type": record.owner_type,
        "owner_id": record.owner_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _raw_to_object(raw: Mapping[str, Any]) -> StorageObjectRecord:
    return StorageObjectRecord(
        id=str(raw["id"]),
        purpose=StoragePurpose(str(raw["purpose"])),
        profile_id=str(raw["profile_id"]),
        backend=str(raw["backend"]),
        namespace=str(raw.get("namespace") or ""),
        key=str(raw.get("object_key") or raw.get("key") or ""),
        size_bytes=int(raw.get("size_bytes") or 0),
        sha256=str(raw.get("sha256") or ""),
        content_type=str(raw.get("content_type") or "application/octet-stream"),
        status=StorageObjectStatus(str(raw.get("status") or "pending")),
        public_url=str(raw.get("public_url") or ""),
        owner_type=str(raw.get("owner_type") or ""),
        owner_id=str(raw.get("owner_id") or ""),
        created_at=_iso_str(raw.get("created_at")),
        updated_at=_iso_str(raw.get("updated_at")),
    )


def _raw_to_alias(raw: Mapping[str, Any]) -> StorageUrlAlias:
    return StorageUrlAlias(
        id=str(raw["id"]),
        alias_path=str(raw["alias_path"]),
        object_id=str(raw["object_id"]),
        purpose=StoragePurpose(str(raw["purpose"])),
        created_at=_iso_str(raw.get("created_at")),
    )


def _migration_to_raw(migration: StorageMigration) -> dict[str, Any]:
    return migration.to_dict()


def _raw_to_migration(raw: Mapping[str, Any]) -> StorageMigration:
    unknown = _json_value(raw.get("unknown_objects"), [])
    return StorageMigration(
        id=str(raw["id"]),
        purpose=StoragePurpose(str(raw["purpose"])),
        source_profile_id=str(raw["source_profile_id"]),
        target_profile_id=str(raw["target_profile_id"]),
        binding_generation=int(raw.get("binding_generation") or 1),
        status=StorageMigrationStatus(str(raw.get("status") or "queued")),
        total_count=int(raw.get("total_count") or 0),
        completed_count=int(raw.get("completed_count") or 0),
        failed_count=int(raw.get("failed_count") or 0),
        skipped_count=int(raw.get("skipped_count") or 0),
        unknown_objects=tuple(
            dict(entry) for entry in (unknown or []) if isinstance(entry, Mapping)
        ),
        last_error=str(raw.get("last_error") or ""),
        created_by=str(raw.get("created_by") or ""),
        created_at=_iso_str(raw.get("created_at")),
        updated_at=_iso_str(raw.get("updated_at")),
        completed_at=_iso_str(raw.get("completed_at")),
    )


def _migration_item_to_raw(item: StorageMigrationItem) -> dict[str, Any]:
    return item.to_dict()


def _raw_to_migration_item(raw: Mapping[str, Any]) -> StorageMigrationItem:
    return StorageMigrationItem(
        id=str(raw["id"]),
        migration_id=str(raw["migration_id"]),
        object_id=str(raw["object_id"]),
        source_profile_id=str(raw["source_profile_id"]),
        source_namespace=str(raw.get("source_namespace") or ""),
        source_key=str(raw["source_key"]),
        source_public_url=str(raw.get("source_public_url") or ""),
        target_profile_id=str(raw["target_profile_id"]),
        target_namespace=str(raw.get("target_namespace") or ""),
        target_key=str(raw["target_key"]),
        target_public_url=str(raw.get("target_public_url") or ""),
        expected_sha256=str(raw.get("expected_sha256") or ""),
        status=StorageMigrationItemStatus(str(raw.get("status") or "pending")),
        attempts=int(raw.get("attempts") or 0),
        last_error=str(raw.get("last_error") or ""),
        created_at=_iso_str(raw.get("created_at")),
        updated_at=_iso_str(raw.get("updated_at")),
    )
