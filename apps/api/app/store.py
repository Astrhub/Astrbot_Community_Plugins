from __future__ import annotations

import secrets
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from .auth import Role, normalize_role


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
