from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any, Iterable

MIGRATION_FILENAME_PATTERN = re.compile(r"^\d{8}_\d{3}_[a-z0-9_]+\.sql$")
MIGRATION_ADVISORY_LOCK_KEY = 0x415354524D4B54


class SchemaMigrationError(RuntimeError):
    """Raised when versioned schema migrations cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class SqlMigration:
    version: str
    checksum: str
    sql: str


def discover_schema_migrations(package: str = "app.migrations") -> list[SqlMigration]:
    """Load packaged SQL migrations in deterministic filename order."""
    migrations: list[SqlMigration] = []
    root = resources.files(package)
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_file() or not MIGRATION_FILENAME_PATTERN.fullmatch(entry.name):
            continue
        payload = entry.read_bytes()
        migrations.append(
            SqlMigration(
                version=entry.name.removesuffix(".sql"),
                checksum=hashlib.sha256(payload).hexdigest(),
                sql=payload.decode("utf-8"),
            )
        )
    return migrations


async def apply_schema_migrations(
    connection: Any,
    migrations: Iterable[SqlMigration] | None = None,
) -> list[str]:
    """Apply missing migrations and reject modified migration history."""
    ordered = sorted(
        list(migrations) if migrations is not None else discover_schema_migrations(),
        key=lambda migration: migration.version,
    )
    _validate_unique_versions(ordered)

    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS market_schema_migrations (
            version text PRIMARY KEY,
            checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    await connection.execute("SELECT pg_advisory_lock($1)", MIGRATION_ADVISORY_LOCK_KEY)
    try:
        applied_rows = await connection.fetch(
            "SELECT version, checksum FROM market_schema_migrations ORDER BY version"
        )
        applied = {str(row["version"]): str(row["checksum"]) for row in applied_rows}
        _validate_applied_checksums(ordered, applied)

        completed: list[str] = []
        for migration in ordered:
            if migration.version in applied:
                continue
            async with connection.transaction():
                await connection.execute(migration.sql)
                await connection.execute(
                    """
                    INSERT INTO market_schema_migrations (version, checksum)
                    VALUES ($1, $2)
                    """,
                    migration.version,
                    migration.checksum,
                )
            completed.append(migration.version)
        return completed
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", MIGRATION_ADVISORY_LOCK_KEY)


def _validate_unique_versions(migrations: list[SqlMigration]) -> None:
    versions = [migration.version for migration in migrations]
    duplicates = sorted({version for version in versions if versions.count(version) > 1})
    if duplicates:
        raise SchemaMigrationError("Duplicate schema migration versions: " + ", ".join(duplicates))


def _validate_applied_checksums(
    migrations: list[SqlMigration],
    applied: dict[str, str],
) -> None:
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None and recorded != migration.checksum:
            raise SchemaMigrationError(f"Schema migration checksum mismatch: {migration.version}")
