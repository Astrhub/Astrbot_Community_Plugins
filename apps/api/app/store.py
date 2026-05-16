from __future__ import annotations

import secrets
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import asyncpg
from redis import asyncio as redis_asyncio

from .auth import Role, normalize_role


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_value(item) for key, item in value.items()}
    return value


class InMemoryMarketStore:
    """Development store. Production will persist data in PostgreSQL and Redis."""

    def __init__(self, seed: dict[str, Any] | None = None) -> None:
        state = deepcopy(seed or {})
        self.state: dict[str, Any] = {
            "users": state.get("users", []),
            "plugins": state.get("plugins", []),
            "submissions": state.get("submissions", []),
            "comments": state.get("comments", []),
            "announcements": state.get("announcements", []),
            "apiKeys": state.get("apiKeys", []),
            "sessions": state.get("sessions", []),
            "nextNumericId": state.get("nextNumericId", 1),
        }
        self.state["users"] = [self._normalize_user(user) for user in self.state["users"]]
        self.state["plugins"] = [self._normalize_plugin(plugin) for plugin in self.state["plugins"]]

    def upsert_github_user(self, profile: dict[str, Any]) -> dict[str, Any]:
        login = profile.get("login") or profile.get("github_login")
        if not login:
            raise ValueError("GitHub profile login is required")

        existing = self.get_user_by_github_login(login)
        if existing:
            existing.update(self._normalize_user({**existing, **profile, "github_login": login}))
            existing["updated_at"] = utc_now()
            return deepcopy(existing)

        user = self._normalize_user(
            {
                "id": str(profile.get("id") or self._next_id("user")),
                "github_id": str(profile.get("id")) if profile.get("id") else None,
                "github_login": login,
                "github_name": profile.get("name") or login,
                "avatar_url": profile.get("avatar_url") or "",
                "role": Role.CORE_ADMIN if not self.state["users"] else Role.USER,
                "muted_until": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.state["users"].append(user)
        return deepcopy(user)

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self._find("users", "id", user_id)

    def get_user_by_github_login(self, login: str) -> dict[str, Any] | None:
        return next(
            (
                user
                for user in self.state["users"]
                if user["github_login"].lower() == str(login).lower()
            ),
            None,
        )

    def list_public_plugins(self) -> list[dict[str, Any]]:
        return [deepcopy(plugin) for plugin in self.state["plugins"] if plugin["status"] == "listed"]

    def list_users(self) -> list[dict[str, Any]]:
        return deepcopy(self.state["users"])

    def list_plugins(self) -> list[dict[str, Any]]:
        return deepcopy(self.state["plugins"])

    def list_submissions(self) -> list[dict[str, Any]]:
        return deepcopy(self.state["submissions"])

    def list_api_keys(self) -> list[dict[str, Any]]:
        return deepcopy(self.state["apiKeys"])

    def summary(self) -> dict[str, int]:
        return {
            "users": len(self.state["users"]),
            "plugins": len(self.state["plugins"]),
            "submissions": len(self.state["submissions"]),
            "announcements": len(self.state["announcements"]),
        }

    def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        return self._find("plugins", "id", plugin_id)

    def submit_plugin(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        plugin = self._normalize_plugin(
            {
                "id": payload.get("id") or payload["name"],
                "name": payload["name"],
                "display_name": payload.get("display_name") or payload["name"],
                "desc": payload["desc"],
                "author": payload["author"],
                "repo": payload["repo"],
                "tags": payload.get("tags", []),
                "social_link": payload.get("social_link", ""),
                "owner_user_id": user["id"],
                "owner_github_login": user["github_login"],
                "status": "pending",
                "stars": 0,
                "likes": 0,
                "comments_count": 0,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.state["submissions"].append(
            {
                "id": self._next_id("submission"),
                "plugin_id": plugin["id"],
                "user_id": user["id"],
                "payload": deepcopy(payload),
                "status": "pending",
                "created_at": utc_now(),
            }
        )
        self._upsert_plugin(plugin)
        return deepcopy(plugin)

    def update_plugin_status(
        self,
        plugin_id: str,
        status: str,
        by_user_id: str | None,
    ) -> dict[str, Any] | None:
        plugin = self.get_plugin(plugin_id)
        if not plugin:
            return None
        plugin["status"] = status
        plugin["moderated_by"] = by_user_id
        plugin["updated_at"] = utc_now()
        return deepcopy(plugin)

    def update_plugin_metadata(
        self,
        plugin_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        plugin = self.get_plugin(plugin_id)
        if not plugin:
            return None
        plugin.update({key: value for key, value in patch.items() if value is not None})
        plugin["updated_at"] = utc_now()
        return deepcopy(plugin)

    def add_comment(
        self,
        plugin_id: str,
        user_id: str,
        body: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        comment = {
            "id": self._next_id("comment"),
            "plugin_id": plugin_id,
            "user_id": user_id,
            "parent_id": parent_id,
            "body": body,
            "muted": False,
            "deleted": False,
            "created_at": utc_now(),
        }
        self.state["comments"].append(comment)
        plugin = self.get_plugin(plugin_id)
        if plugin:
            plugin["comments_count"] = len(self.list_comments(plugin_id))
        return deepcopy(comment)

    def delete_comment(self, comment_id: str, by_user_id: str) -> dict[str, Any] | None:
        comment = self._find("comments", "id", comment_id)
        if not comment:
            return None
        comment["deleted"] = True
        comment["deleted_by"] = by_user_id
        comment["deleted_at"] = utc_now()
        return deepcopy(comment)

    def mute_user(
        self,
        user_id: str,
        muted_until: str,
        by_user_id: str,
    ) -> dict[str, Any] | None:
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        user["muted_until"] = muted_until
        user["muted_by"] = by_user_id
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def update_user_role(self, user_id: str, role: str) -> dict[str, Any] | None:
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        user["role"] = normalize_role(role).value
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def publish_announcement(
        self,
        title: str,
        body: str,
        author_user_id: str,
    ) -> dict[str, Any]:
        announcement = {
            "id": self._next_id("announcement"),
            "title": title,
            "body": body,
            "author_user_id": author_user_id,
            "created_at": utc_now(),
        }
        self.state["announcements"].insert(0, announcement)
        return deepcopy(announcement)

    def list_announcements(self) -> list[dict[str, Any]]:
        return deepcopy(self.state["announcements"])

    def issue_api_key(
        self,
        name: str,
        user_id: str,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        api_key = {
            "id": self._next_id("apikey"),
            "name": name,
            "user_id": user_id,
            "scopes": scopes or ["market:read"],
            "key": f"mk_{secrets.token_urlsafe(24)}",
            "created_at": utc_now(),
        }
        self.state["apiKeys"].append(api_key)
        return deepcopy(api_key)

    def create_session(self, user_id: str, token: str | None = None) -> dict[str, Any]:
        session = {
            "token": token or f"sess_{secrets.token_urlsafe(24)}",
            "user_id": user_id,
            "created_at": utc_now(),
            "last_seen_at": utc_now(),
        }
        self.state["sessions"].append(session)
        return deepcopy(session)

    def get_user_by_session(self, token: str) -> dict[str, Any] | None:
        session = self._find("sessions", "token", token)
        if not session:
            return None
        session["last_seen_at"] = utc_now()
        return self.get_user_by_id(session["user_id"])

    def revoke_session(self, token: str) -> bool:
        sessions = self.state["sessions"]
        index = next((i for i, item in enumerate(sessions) if item["token"] == token), -1)
        if index == -1:
            return False
        sessions.pop(index)
        return True

    def list_comments(self, plugin_id: str) -> list[dict[str, Any]]:
        return [
            deepcopy(comment)
            for comment in self.state["comments"]
            if comment["plugin_id"] == plugin_id and not comment.get("deleted")
        ]

    def _next_id(self, prefix: str) -> str:
        next_id = f"{prefix}_{self.state['nextNumericId']}"
        self.state["nextNumericId"] += 1
        return next_id

    def _find(self, collection: str, key: str, value: str) -> dict[str, Any] | None:
        return next((item for item in self.state[collection] if item.get(key) == value), None)

    def _upsert_plugin(self, plugin: dict[str, Any]) -> None:
        existing = self.get_plugin(plugin["id"])
        if existing:
            existing.update(plugin)
            return
        self.state["plugins"].append(plugin)

    def _normalize_user(self, user: dict[str, Any]) -> dict[str, Any]:
        return {
            **user,
            "role": normalize_role(user.get("role")).value,
            "github_login": user.get("github_login") or user.get("login") or "",
            "github_name": user.get("github_name")
            or user.get("name")
            or user.get("github_login")
            or user.get("login")
            or "",
            "muted_until": user.get("muted_until") or None,
        }

    def _normalize_plugin(self, plugin: dict[str, Any]) -> dict[str, Any]:
        return {
            **plugin,
            "tags": plugin.get("tags") if isinstance(plugin.get("tags"), list) else [],
            "stars": int(plugin.get("stars") or 0),
            "likes": int(plugin.get("likes") or 0),
            "comments_count": int(plugin.get("comments_count") or 0),
            "status": plugin.get("status") or "pending",
        }


PLUGIN_COLUMN_KEYS = {
    "name",
    "display_name",
    "desc",
    "author",
    "repo",
    "tags",
    "social_link",
    "owner_user_id",
    "owner_github_login",
    "status",
    "stars",
    "likes",
    "comments_count",
    "moderated_by",
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_users (
    id text PRIMARY KEY,
    github_id text UNIQUE,
    github_login text NOT NULL,
    github_name text NOT NULL DEFAULT '',
    avatar_url text NOT NULL DEFAULT '',
    role text NOT NULL CHECK (role IN ('core_admin', 'admin', 'user')),
    muted_until timestamptz,
    muted_by text REFERENCES market_users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS market_users_github_login_lower_idx
    ON market_users (lower(github_login));

CREATE TABLE IF NOT EXISTS market_plugins (
    id text PRIMARY KEY,
    name text NOT NULL UNIQUE,
    display_name text NOT NULL,
    desc_text text NOT NULL,
    author text NOT NULL,
    repo text NOT NULL UNIQUE,
    tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    social_link text NOT NULL DEFAULT '',
    owner_user_id text NOT NULL REFERENCES market_users(id) ON DELETE RESTRICT,
    owner_github_login text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'listed', 'unlisted')),
    stars integer NOT NULL DEFAULT 0 CHECK (stars >= 0),
    likes integer NOT NULL DEFAULT 0 CHECK (likes >= 0),
    comments_count integer NOT NULL DEFAULT 0 CHECK (comments_count >= 0),
    moderated_by text REFERENCES market_users(id) ON DELETE SET NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_plugins_status_idx ON market_plugins(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS market_plugins_tags_gin_idx ON market_plugins USING GIN (tags);

CREATE TABLE IF NOT EXISTS market_submissions (
    id text PRIMARY KEY,
    plugin_id text NOT NULL REFERENCES market_plugins(id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_submissions_status_idx
    ON market_submissions(status, created_at DESC);

CREATE TABLE IF NOT EXISTS market_comments (
    id text PRIMARY KEY,
    plugin_id text NOT NULL REFERENCES market_plugins(id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    parent_id text REFERENCES market_comments(id) ON DELETE SET NULL,
    body text NOT NULL,
    muted boolean NOT NULL DEFAULT false,
    deleted boolean NOT NULL DEFAULT false,
    deleted_by text REFERENCES market_users(id) ON DELETE SET NULL,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_comments_plugin_idx
    ON market_comments(plugin_id, created_at ASC) WHERE deleted = false;

CREATE TABLE IF NOT EXISTS market_announcements (
    id text PRIMARY KEY,
    title text NOT NULL,
    body text NOT NULL,
    author_user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_announcements_created_idx
    ON market_announcements(created_at DESC);

CREATE TABLE IF NOT EXISTS market_api_keys (
    id text PRIMARY KEY,
    name text NOT NULL,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    scopes jsonb NOT NULL DEFAULT '["market:read"]'::jsonb,
    key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


class PgRedisMarketStore(InMemoryMarketStore):
    """Production store backed by PostgreSQL for durable data and Redis for sessions."""

    def __init__(self, database_url: str, redis_url: str, session_ttl_seconds: int) -> None:
        super().__init__()
        self.database_url = database_url
        self.redis_url = redis_url
        self.session_ttl_seconds = session_ttl_seconds
        self.pool: asyncpg.Pool | None = None
        self.redis: redis_asyncio.Redis | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            self.database_url,
            min_size=1,
            max_size=10,
            init=self._init_connection,
        )
        self.redis = redis_asyncio.from_url(self.redis_url, decode_responses=True)
        await self._ensure_schema()
        await self.redis.ping()

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()
        if self.pool:
            await self.pool.close()

    async def _init_connection(self, connection: asyncpg.Connection) -> None:
        for type_name in ("json", "jsonb"):
            await connection.set_type_codec(
                type_name,
                encoder=serialize_json,
                decoder=parse_json,
                schema="pg_catalog",
            )

    async def _ensure_schema(self) -> None:
        async with self._pool().acquire() as connection:
            await connection.execute(SCHEMA_SQL)

    async def upsert_github_user(self, profile: dict[str, Any]) -> dict[str, Any]:
        login = profile.get("login") or profile.get("github_login")
        if not login:
            raise ValueError("GitHub profile login is required")

        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute("LOCK TABLE market_users IN EXCLUSIVE MODE")
                existing = await connection.fetchrow(
                    "SELECT * FROM market_users WHERE lower(github_login) = lower($1)",
                    str(login),
                )
                if existing:
                    row = await connection.fetchrow(
                        """
                        UPDATE market_users
                           SET github_id = COALESCE($2, github_id),
                               github_login = $3,
                               github_name = $4,
                               avatar_url = $5,
                               updated_at = now()
                         WHERE id = $1
                     RETURNING *
                        """,
                        existing["id"],
                        str(profile.get("id")) if profile.get("id") else None,
                        str(login),
                        profile.get("name") or login,
                        profile.get("avatar_url") or "",
                    )
                    return self._user_from_record(row)

                role = Role.CORE_ADMIN if await self._user_count(connection) == 0 else Role.USER
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_users (
                        id, github_id, github_login, github_name, avatar_url, role
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING *
                    """,
                    str(profile.get("id") or new_id("user")),
                    str(profile.get("id")) if profile.get("id") else None,
                    str(login),
                    profile.get("name") or login,
                    profile.get("avatar_url") or "",
                    role.value,
                )
                return self._user_from_record(row)

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow("SELECT * FROM market_users WHERE id = $1", user_id)
        return self._user_from_record(row) if row else None

    async def get_user_by_github_login(self, login: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_users WHERE lower(github_login) = lower($1)",
            login,
        )
        return self._user_from_record(row) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch("SELECT * FROM market_users ORDER BY created_at ASC")
        return [self._user_from_record(row) for row in rows]

    async def list_public_plugins(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM market_plugins WHERE status = 'listed' ORDER BY updated_at DESC"
        )
        return [self._plugin_from_record(row) for row in rows]

    async def list_plugins(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch("SELECT * FROM market_plugins ORDER BY updated_at DESC")
        return [self._plugin_from_record(row) for row in rows]

    async def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow("SELECT * FROM market_plugins WHERE id = $1", plugin_id)
        return self._plugin_from_record(row) if row else None

    async def submit_plugin(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        plugin_id = payload.get("id") or payload["name"]
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_plugins (
                        id, name, display_name, desc_text, author, repo, tags, social_link,
                        owner_user_id, owner_github_login, status, stars, likes, comments_count
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10,
                            'pending', 0, 0, 0)
                    ON CONFLICT (id) DO UPDATE
                       SET name = EXCLUDED.name,
                           display_name = EXCLUDED.display_name,
                           desc_text = EXCLUDED.desc_text,
                           author = EXCLUDED.author,
                           repo = EXCLUDED.repo,
                           tags = EXCLUDED.tags,
                           social_link = EXCLUDED.social_link,
                           owner_user_id = EXCLUDED.owner_user_id,
                           owner_github_login = EXCLUDED.owner_github_login,
                           status = 'pending',
                           updated_at = now()
                    RETURNING *
                    """,
                    plugin_id,
                    payload["name"],
                    payload.get("display_name") or payload["name"],
                    payload["desc"],
                    payload["author"],
                    payload["repo"],
                    payload.get("tags", []),
                    payload.get("social_link", ""),
                    user["id"],
                    user["github_login"],
                )
                await connection.execute(
                    """
                    INSERT INTO market_submissions (id, plugin_id, user_id, payload, status)
                    VALUES ($1, $2, $3, $4::jsonb, 'pending')
                    """,
                    new_id("submission"),
                    plugin_id,
                    user["id"],
                    payload,
                )
                return self._plugin_from_record(row)

    async def list_submissions(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch("SELECT * FROM market_submissions ORDER BY created_at DESC")
        return [self._submission_from_record(row) for row in rows]

    async def update_plugin_status(
        self,
        plugin_id: str,
        status: str,
        by_user_id: str | None,
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE market_plugins
               SET status = $2, moderated_by = $3, updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            plugin_id,
            status,
            by_user_id,
        )
        return self._plugin_from_record(row) if row else None

    async def update_plugin_metadata(
        self,
        plugin_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        clean_patch = {key: value for key, value in patch.items() if value is not None}
        current = await self.get_plugin(plugin_id)
        if not current:
            return None
        updated = {**current, **clean_patch, "updated_at": utc_now()}
        metadata = {
            key: value
            for key, value in updated.items()
            if key not in PLUGIN_COLUMN_KEYS
            and key not in {"id", "created_at", "updated_at"}
            and value is not None
        }
        row = await self._pool().fetchrow(
            """
            UPDATE market_plugins
               SET name = $2,
                   display_name = $3,
                   desc_text = $4,
                   author = $5,
                   repo = $6,
                   tags = $7::jsonb,
                   social_link = $8,
                   owner_user_id = $9,
                   owner_github_login = $10,
                   status = $11,
                   stars = $12,
                   likes = $13,
                   comments_count = $14,
                   moderated_by = $15,
                   metadata = $16::jsonb,
                   updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            plugin_id,
            updated["name"],
            updated["display_name"],
            updated["desc"],
            updated["author"],
            updated["repo"],
            updated["tags"],
            updated.get("social_link", ""),
            updated["owner_user_id"],
            updated["owner_github_login"],
            updated["status"],
            int(updated.get("stars") or 0),
            int(updated.get("likes") or 0),
            int(updated.get("comments_count") or 0),
            updated.get("moderated_by"),
            metadata,
        )
        return self._plugin_from_record(row) if row else None

    async def add_comment(
        self,
        plugin_id: str,
        user_id: str,
        body: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_comments (id, plugin_id, user_id, parent_id, body)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING *
                    """,
                    new_id("comment"),
                    plugin_id,
                    user_id,
                    parent_id,
                    body,
                )
                await self._refresh_comments_count(connection, plugin_id)
                return self._comment_from_record(row)

    async def delete_comment(self, comment_id: str, by_user_id: str) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE market_comments
                       SET deleted = true, deleted_by = $2, deleted_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    comment_id,
                    by_user_id,
                )
                if row:
                    await self._refresh_comments_count(connection, row["plugin_id"])
                return self._comment_from_record(row) if row else None

    async def mute_user(
        self,
        user_id: str,
        muted_until: str,
        by_user_id: str,
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE market_users
               SET muted_until = $2::timestamptz, muted_by = $3, updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            user_id,
            muted_until,
            by_user_id,
        )
        return self._user_from_record(row) if row else None

    async def update_user_role(self, user_id: str, role: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE market_users
               SET role = $2, updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            user_id,
            normalize_role(role).value,
        )
        return self._user_from_record(row) if row else None

    async def publish_announcement(
        self,
        title: str,
        body: str,
        author_user_id: str,
    ) -> dict[str, Any]:
        row = await self._pool().fetchrow(
            """
            INSERT INTO market_announcements (id, title, body, author_user_id)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            new_id("announcement"),
            title,
            body,
            author_user_id,
        )
        return self._announcement_from_record(row)

    async def list_announcements(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM market_announcements ORDER BY created_at DESC"
        )
        return [self._announcement_from_record(row) for row in rows]

    async def issue_api_key(
        self,
        name: str,
        user_id: str,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        row = await self._pool().fetchrow(
            """
            INSERT INTO market_api_keys (id, name, user_id, scopes, key)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            RETURNING *
            """,
            new_id("apikey"),
            name,
            user_id,
            scopes or ["market:read"],
            f"mk_{secrets.token_urlsafe(24)}",
        )
        return self._api_key_from_record(row)

    async def list_api_keys(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch("SELECT * FROM market_api_keys ORDER BY created_at DESC")
        return [self._api_key_from_record(row) for row in rows]

    async def create_session(self, user_id: str, token: str | None = None) -> dict[str, Any]:
        session = {
            "token": token or f"sess_{secrets.token_urlsafe(24)}",
            "user_id": user_id,
            "created_at": utc_now(),
            "last_seen_at": utc_now(),
        }
        await self._redis().set(
            self._session_key(session["token"]),
            serialize_json(session),
            ex=self.session_ttl_seconds,
        )
        return deepcopy(session)

    async def get_user_by_session(self, token: str) -> dict[str, Any] | None:
        payload = await self._redis().get(self._session_key(token))
        if not payload:
            return None
        session = parse_json(payload)
        session["last_seen_at"] = utc_now()
        await self._redis().set(
            self._session_key(token),
            serialize_json(session),
            ex=self.session_ttl_seconds,
        )
        return await self.get_user_by_id(session["user_id"])

    async def revoke_session(self, token: str) -> bool:
        return bool(await self._redis().delete(self._session_key(token)))

    async def list_comments(self, plugin_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT * FROM market_comments
             WHERE plugin_id = $1 AND deleted = false
             ORDER BY created_at ASC
            """,
            plugin_id,
        )
        return [self._comment_from_record(row) for row in rows]

    async def summary(self) -> dict[str, int]:
        row = await self._pool().fetchrow(
            """
            SELECT
                (SELECT count(*) FROM market_users) AS users,
                (SELECT count(*) FROM market_plugins) AS plugins,
                (SELECT count(*) FROM market_submissions) AS submissions,
                (SELECT count(*) FROM market_announcements) AS announcements
            """
        )
        return {key: int(row[key]) for key in row.keys()}

    async def _user_count(self, connection: asyncpg.Connection) -> int:
        return int(await connection.fetchval("SELECT count(*) FROM market_users"))

    async def _refresh_comments_count(self, connection: asyncpg.Connection, plugin_id: str) -> None:
        await connection.execute(
            """
            UPDATE market_plugins
               SET comments_count = (
                   SELECT count(*) FROM market_comments
                    WHERE plugin_id = $1 AND deleted = false
               ),
                   updated_at = now()
             WHERE id = $1
            """,
            plugin_id,
        )

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("PostgreSQL store is not connected")
        return self.pool

    def _redis(self) -> redis_asyncio.Redis:
        if not self.redis:
            raise RuntimeError("Redis store is not connected")
        return self.redis

    def _session_key(self, token: str) -> str:
        return f"astrbot_market:session:{token}"

    def _user_from_record(self, row: asyncpg.Record) -> dict[str, Any]:
        return self._normalize_user(serialize_value(dict(row)))

    def _plugin_from_record(self, row: asyncpg.Record) -> dict[str, Any]:
        data = serialize_value(dict(row))
        metadata = data.pop("metadata") or {}
        data["desc"] = data.pop("desc_text")
        data.update(metadata)
        return self._normalize_plugin(data)

    def _submission_from_record(self, row: asyncpg.Record) -> dict[str, Any]:
        return serialize_value(dict(row))

    def _comment_from_record(self, row: asyncpg.Record) -> dict[str, Any]:
        return serialize_value(dict(row))

    def _announcement_from_record(self, row: asyncpg.Record) -> dict[str, Any]:
        return serialize_value(dict(row))

    def _api_key_from_record(self, row: asyncpg.Record) -> dict[str, Any]:
        return serialize_value(dict(row))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def serialize_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_json(value: str) -> Any:
    import json

    return json.loads(value)
