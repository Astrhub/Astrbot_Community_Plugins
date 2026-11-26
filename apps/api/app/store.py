from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any

import asyncpg
from redis import asyncio as redis_asyncio

from .auth import Role, normalize_role
from .git_sources.models import GitSourceError, parse_repository_ref
from .schema_migrations import apply_schema_migrations
from .plugin_identity import PluginIdentityConflict


class AnnouncementConflict(ValueError):
    pass


class GithubIdentityConflict(ValueError):
    pass


class UserIdentityConflict(ValueError):
    def __init__(self, message: str, code: str = "user_identity_conflict") -> None:
        super().__init__(message)
        self.code = code


def _canonical_repo_fields(
    repo: str,
    provider: str = "",
    external_id: str = "",
) -> tuple[str, str, str]:
    raw_repo = str(repo or "").strip()
    if raw_repo:
        try:
            ref = parse_repository_ref(raw_repo)
            return (ref.provider, str(external_id or "").strip(), ref.normalized_repo)
        except GitSourceError:
            pass
    fallback_key = raw_repo.lower().removesuffix(".git").rstrip("/")
    return (str(provider or "").strip().lower(), str(external_id or "").strip(), fallback_key)


def plugin_identity_write(method: Any) -> Any:
    @wraps(method)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await method(*args, **kwargs)
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "market_plugins_author_name_key":
                raise PluginIdentityConflict("已存在相同作者与插件名组合的插件") from exc
            if exc.constraint_name in {
                "market_plugins_owner_name_key",
                "market_plugins_namespace_key",
                "market_plugins_owner_username_name_key",
                "market_plugins_repo_key",
                "market_plugins_normalized_repo_key",
                "market_git_repository_bindings_active_repo_idx",
            }:
                raise PluginIdentityConflict("该用户名下已存在同名插件，或仓库已被登记") from exc
            raise

    return wrapped


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def generate_access_key() -> str:
    return f"sk-ah-{secrets.token_hex(24)}"


def serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_value(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    return value


def coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


USER_PROFILE_FIELDS = (
    "github_id",
    "github_login",
    "github_name",
    "github_email",
    "email",
    "email_verified_at",
    "public_username",
    "session_version",
    "avatar_url",
    "auth_source",
    "github_token",
    "github_refresh_interval_seconds",
    "notification_email",
    "notify_plugin_review",
    "notify_comments",
    "notify_replies",
    "notify_likes",
    "notify_unlist",
    "email_notify_plugin_review",
    "email_notify_pending_review",
    "email_notify_comments",
    "email_notify_replies",
    "email_notify_likes",
    "email_notify_unlist",
)


class InMemoryMarketStore:
    """Development store. Production will persist data in PostgreSQL and Redis."""

    def __init__(self, seed: dict[str, Any] | None = None) -> None:
        state = deepcopy(seed or {})
        self.state: dict[str, Any] = {
            "users": state.get("users", []),
            "plugins": state.get("plugins", []),
            "submissions": state.get("submissions", []),
            "comments": state.get("comments", []),
            "pluginLikes": state.get("pluginLikes", []),
            "commentLikes": state.get("commentLikes", []),
            "announcements": state.get("announcements", []),
            "notifications": state.get("notifications", []),
            "apiKeys": state.get("apiKeys", []),
            "oauthIdentities": state.get("oauthIdentities", []),
            "oauthCredentials": dict(state.get("oauthCredentials", {})),
            "gitBindings": list(state.get("gitBindings", [])),
            "emailChallenges": state.get("emailChallenges", {}),
            "rateLimits": state.get("rateLimits", {}),
            "cooldowns": state.get("cooldowns", {}),
            "options": state.get("options", {}),
            "objectStorage": deepcopy(state.get("objectStorage", {})),
            "sessions": state.get("sessions", []),
            "nextNumericId": state.get("nextNumericId", 1),
        }
        self.state["users"] = [self._normalize_user(user) for user in self.state["users"]]
        self.state["plugins"] = [self._normalize_plugin(plugin) for plugin in self.state["plugins"]]
        self._backfill_seeded_identities()

    def persist_object_storage(self) -> None:
        return None

    def _backfill_seeded_identities(self) -> None:
        seen_public: set[str] = set()
        for user in self.state["users"]:
            base = (
                str(user.get("public_username") or "").strip()
                or str(user.get("github_login") or "").strip()
                or str(user.get("internal_username") or "").strip()
                or str(user.get("id") or "")
            )
            candidate = base
            reserved = {
                str(other.get(field) or "").strip().lower()
                for other in self.state["users"]
                if other["id"] != user["id"]
                for field in ("public_username", "internal_username", "github_login")
                if str(other.get(field) or "").strip()
            }
            suffix = 0
            while candidate.lower() in seen_public or candidate.lower() in reserved:
                suffix += 1
                digest = hashlib.md5(str(user["id"]).encode("utf-8")).hexdigest()[:6]
                candidate = f"{base}_{digest}"
                if suffix > 1:
                    candidate = f"{candidate}_{suffix}"
            user["public_username"] = candidate
            seen_public.add(candidate.lower())

            if user.get("github_id") or user.get("github_login"):
                provider_uid = str(user.get("github_id") or user["id"])
                existing = next(
                    (
                        item
                        for item in self.state["oauthIdentities"]
                        if item.get("provider") == "github"
                        and item.get("revoked_at") is None
                        and (
                            item.get("user_id") == user["id"]
                            or str(item.get("provider_user_id")) == provider_uid
                        )
                    ),
                    None,
                )
                if not existing:
                    self.state["oauthIdentities"].append(
                        {
                            "id": f"oauth_github_{user['id']}",
                            "user_id": user["id"],
                            "provider": "github",
                            "instance_url": "https://github.com",
                            "provider_user_id": provider_uid,
                            "provider_login": str(user.get("github_login") or ""),
                            "provider_email": str(user.get("github_email") or ""),
                            "avatar_url": str(user.get("avatar_url") or ""),
                            "scopes": "",
                            "credential_ref": "",
                            "expires_at": None,
                            "revoked_at": None,
                            "created_at": user.get("created_at") or utc_now(),
                            "updated_at": user.get("updated_at") or utc_now(),
                        }
                    )

        for plugin in self.state["plugins"]:
            if not str(plugin.get("owner_username") or "").strip():
                owner = self.get_user_by_id(str(plugin.get("owner_user_id") or "")) or {}
                plugin["owner_username"] = (
                    str(owner.get("public_username") or "").strip()
                    or str(plugin.get("owner_github_login") or "").strip()
                )

    def _allocate_public_username(
        self,
        preferred: str,
        *,
        exclude_user_id: str = "",
        suffix_tag: str = "gh",
    ) -> str:
        base = str(preferred or "").strip() or f"user_{secrets.token_hex(4)}"
        taken = {
            str(u.get(field) or "").strip().lower()
            for u in self.state["users"]
            if u["id"] != exclude_user_id
            for field in ("public_username", "internal_username", "github_login")
            if str(u.get(field) or "").strip()
        }
        if base.lower() not in taken:
            return base
        candidate = f"{base}_{suffix_tag}"
        if candidate.lower() not in taken:
            return candidate
        for index in range(1, 1000):
            candidate = f"{base}_{index}"
            if candidate.lower() not in taken:
                return candidate
        return f"{base}_{secrets.token_hex(3)}"

    def create_oauth_user(
        self,
        *,
        provider: str,
        provider_login: str,
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        norm_provider = str(provider or "oauth").strip().lower()
        clean_login = str(provider_login or "").strip() or f"{norm_provider}_{secrets.token_hex(3)}"
        public_username = self._allocate_public_username(clean_login, suffix_tag=norm_provider)
        user = self._normalize_user(
            {
                "id": self._next_id("user"),
                "github_id": None,
                "github_login": "",
                "internal_username": "",
                "public_username": public_username,
                "email": "",
                "email_verified_at": None,
                "session_version": 1,
                "password_hash": "",
                "auth_source": "oauth",
                "github_name": str(display_name or clean_login).strip(),
                "github_email": "",
                "avatar_url": str(avatar_url or "").strip(),
                "role": Role.USER,
                "muted_until": None,
                "muted_reason": "",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.state["users"].append(user)
        return deepcopy(user)

    def upsert_github_user(self, profile: dict[str, Any]) -> dict[str, Any]:
        login = profile.get("login") or profile.get("github_login")
        if not login:
            raise ValueError("GitHub profile login is required")

        github_id = str(profile.get("id") or "")
        existing = self.get_user_by_github_id(github_id) if github_id else None
        login_owner = self.get_user_by_github_login(login)
        if (
            github_id
            and login_owner
            and str(login_owner.get("github_id") or login_owner["id"]) != github_id
        ):
            raise GithubIdentityConflict("GitHub 用户名已关联其他身份，请联系管理员核对")
        existing = existing or login_owner
        github_email = str(profile.get("github_email") or profile.get("email") or "")
        avatar_url = str(profile.get("avatar_url") or "")
        if existing:
            is_github_origin = existing.get("auth_source") == "github"
            next_name = (
                profile.get("name") or login
                if is_github_origin or not existing.get("github_name")
                else existing["github_name"]
            )
            next_avatar = (
                avatar_url or existing.get("avatar_url", "")
                if is_github_origin or not existing.get("avatar_url")
                else existing["avatar_url"]
            )
            existing.update(
                self._normalize_user(
                    {
                        **existing,
                        "github_id": github_id or existing.get("github_id"),
                        "github_login": login,
                        "public_username": existing.get("public_username")
                        or self._allocate_public_username(login, exclude_user_id=existing["id"]),
                        "github_name": next_name,
                        "avatar_url": next_avatar,
                        "github_email": github_email or existing.get("github_email", ""),
                    }
                )
            )
            existing["updated_at"] = utc_now()
            self.upsert_oauth_identity(
                existing["id"],
                provider="github",
                instance_url="https://github.com",
                provider_user_id=str(existing.get("github_id") or existing["id"]),
                provider_login=str(login),
                provider_email=github_email,
                avatar_url=avatar_url,
            )
            return deepcopy(existing)

        user_id = str(profile.get("id") or self._next_id("user"))
        public_username = self._allocate_public_username(str(login))
        user = self._normalize_user(
            {
                "id": user_id,
                "github_id": str(profile.get("id")) if profile.get("id") else None,
                "github_login": login,
                "internal_username": "",
                "public_username": public_username,
                "email": "",
                "email_verified_at": None,
                "session_version": 1,
                "password_hash": "",
                "auth_source": "github",
                "github_name": profile.get("name") or login,
                "github_email": github_email,
                "avatar_url": avatar_url,
                "role": Role.USER,
                "muted_until": None,
                "muted_reason": "",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.state["users"].append(user)
        self.upsert_oauth_identity(
            user["id"],
            provider="github",
            instance_url="https://github.com",
            provider_user_id=str(user.get("github_id") or user["id"]),
            provider_login=str(login),
            provider_email=github_email,
            avatar_url=avatar_url,
        )
        return deepcopy(user)

    def create_internal_admin(self, username: str, password_hash: str) -> dict[str, Any]:
        existing = self.get_user_by_internal_username(username)
        if existing:
            existing["password_hash"] = password_hash
            existing["role"] = Role.CORE_ADMIN.value
            if not existing.get("public_username"):
                existing["public_username"] = self._allocate_public_username(
                    username, exclude_user_id=existing["id"]
                )
            existing["updated_at"] = utc_now()
            return deepcopy(existing)

        user = self._normalize_user(
            {
                "id": self._next_id("user"),
                "github_id": None,
                "github_login": "",
                "internal_username": username,
                "public_username": self._allocate_public_username(username),
                "email": "",
                "email_verified_at": None,
                "session_version": 1,
                "password_hash": password_hash,
                "auth_source": "internal",
                "github_name": username,
                "avatar_url": "",
                "role": Role.CORE_ADMIN,
                "muted_until": None,
                "muted_reason": "",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.state["users"].append(user)
        return deepcopy(user)

    def create_internal_user(
        self,
        username: str,
        password_hash: str,
        role: str = Role.USER.value,
        email: str = "",
    ) -> dict[str, Any]:
        clean_username = str(username or "").strip()
        clean_email = str(email or "").strip()
        if self.get_user_by_internal_username(clean_username) or self.get_user_by_public_username(
            clean_username
        ):
            raise UserIdentityConflict("用户名已被占用", "username_already_exists")
        if clean_email and self.get_user_by_email(clean_email):
            raise UserIdentityConflict("邮箱已被注册", "email_already_exists")
        user = self._normalize_user(
            {
                "id": self._next_id("user"),
                "github_id": None,
                "github_login": "",
                "internal_username": clean_username,
                "public_username": clean_username,
                "email": clean_email,
                "email_verified_at": None,
                "session_version": 1,
                "password_hash": password_hash,
                "auth_source": "internal",
                "github_name": clean_username,
                "avatar_url": "",
                "role": normalize_role(role),
                "muted_until": None,
                "muted_reason": "",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.state["users"].append(user)
        return deepcopy(user)

    def create_email_user(
        self,
        *,
        username: str,
        email: str,
        password_hash: str,
        email_verified_at: str | None = None,
        role: str = Role.USER.value,
    ) -> dict[str, Any]:
        clean_username = str(username or "").strip()
        clean_email = str(email or "").strip()
        if self.get_user_by_internal_username(clean_username) or self.get_user_by_public_username(
            clean_username
        ):
            raise UserIdentityConflict("用户名已被占用", "username_already_exists")
        if clean_email and self.get_user_by_email(clean_email):
            raise UserIdentityConflict("邮箱已被注册", "email_already_exists")
        user = self._normalize_user(
            {
                "id": self._next_id("user"),
                "github_id": None,
                "github_login": "",
                "internal_username": clean_username,
                "public_username": clean_username,
                "email": clean_email,
                "email_verified_at": email_verified_at,
                "session_version": 1,
                "password_hash": password_hash,
                "auth_source": "email",
                "github_name": clean_username,
                "avatar_url": "",
                "role": normalize_role(role),
                "muted_until": None,
                "muted_reason": "",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.state["users"].append(user)
        return deepcopy(user)

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self._find("users", "id", user_id)

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        target = str(email or "").strip().lower()
        if not target:
            return None
        return next(
            (
                deepcopy(user)
                for user in self.state["users"]
                if str(user.get("email") or "").strip().lower() == target
            ),
            None,
        )

    def get_user_by_public_username(self, username: str) -> dict[str, Any] | None:
        target = str(username or "").strip().lower()
        if not target:
            return None
        return next(
            (
                deepcopy(user)
                for user in self.state["users"]
                if str(user.get("public_username") or "").strip().lower() == target
            ),
            None,
        )

    def get_user_by_login_identifier(self, identifier: str) -> dict[str, Any] | None:
        clean = str(identifier or "").strip()
        if not clean:
            return None
        if "@" in clean:
            return self.get_user_by_email(clean)
        internal = self.get_user_by_internal_username(clean)
        public = self.get_user_by_public_username(clean)
        if internal and public and internal["id"] != public["id"]:
            raise UserIdentityConflict(
                "登录标识符存在歧义，请联系管理员处理", "login_identifier_ambiguous"
            )
        return internal or public

    def get_user_by_github_id(self, github_id: str) -> dict[str, Any] | None:
        target = str(github_id or "").strip()
        if not target:
            return None
        identity = self.get_oauth_identity_by_provider_user("github", "https://github.com", target)
        if identity:
            user = self.get_user_by_id(identity["user_id"])
            if user:
                return deepcopy(user)
        return next(
            (
                deepcopy(user)
                for user in self.state["users"]
                if str(user.get("github_id") or "") == target
                or (
                    not user.get("github_id")
                    and user.get("auth_source") == "github"
                    and str(user["id"]) == target
                )
            ),
            None,
        )

    def get_user_by_github_login(self, login: str) -> dict[str, Any] | None:
        target = str(login or "").strip().lower()
        if not target:
            return None
        return next(
            (
                deepcopy(user)
                for user in self.state["users"]
                if str(user.get("github_login") or "").lower() == target
            ),
            None,
        )

    def get_user_by_internal_username(self, username: str) -> dict[str, Any] | None:
        target = str(username or "").strip().lower()
        if not target:
            return None
        return next(
            (
                deepcopy(user)
                for user in self.state["users"]
                if str(user.get("internal_username") or "").lower() == target
            ),
            None,
        )

    def set_user_password(
        self,
        user_id: str,
        password_hash: str,
        *,
        bump_session_version: bool = True,
        verify_email_at: str | None = None,
        email: str | None = None,
        expected_email: str | None = None,
    ) -> dict[str, Any] | None:
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        if (
            expected_email is not None
            and str(user.get("email") or "").strip().lower() != str(expected_email).strip().lower()
        ):
            return None
        if email is not None:
            clean_email = str(email).strip()
            if clean_email:
                owner = self.get_user_by_email(clean_email)
                if owner and owner["id"] != user_id:
                    raise UserIdentityConflict("该邮箱已被其他账号使用", "email_already_exists")
                user["email"] = clean_email
        user["password_hash"] = password_hash
        if verify_email_at:
            user["email_verified_at"] = verify_email_at
        if bump_session_version:
            user["session_version"] = int(user.get("session_version") or 1) + 1
            self._revoke_sessions_for_user_locked(user_id)
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def update_user_email(
        self,
        user_id: str,
        new_email: str,
        *,
        email_verified_at: str | None,
    ) -> dict[str, Any] | None:
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        clean_email = str(new_email or "").strip()
        owner = self.get_user_by_email(clean_email)
        if owner and owner["id"] != user_id:
            raise UserIdentityConflict("该邮箱已被其他账号使用", "email_already_exists")
        user["email"] = clean_email
        user["email_verified_at"] = email_verified_at
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def list_oauth_identities(
        self,
        user_id: str,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            deepcopy(item)
            for item in self.state["oauthIdentities"]
            if item.get("user_id") == user_id
            and (include_revoked or item.get("revoked_at") is None)
        ]

    def get_oauth_identity_by_provider_user(
        self,
        provider: str,
        instance_url: str,
        provider_user_id: str,
    ) -> dict[str, Any] | None:
        norm_provider = str(provider or "").strip().lower()
        norm_instance = str(instance_url or "").strip().lower()
        uid = str(provider_user_id or "").strip()
        return next(
            (
                deepcopy(item)
                for item in self.state["oauthIdentities"]
                if item.get("revoked_at") is None
                and str(item.get("provider") or "").lower() == norm_provider
                and str(item.get("instance_url") or "").lower() == norm_instance
                and str(item.get("provider_user_id") or "") == uid
            ),
            None,
        )

    def upsert_oauth_identity(
        self,
        user_id: str,
        *,
        provider: str,
        instance_url: str,
        provider_user_id: str,
        provider_login: str,
        provider_email: str = "",
        avatar_url: str = "",
        scopes: str = "",
        credential_ref: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        norm_provider = str(provider or "").strip().lower()
        norm_instance = str(instance_url or "").strip()
        uid = str(provider_user_id or "").strip()
        active_conflict = next(
            (
                item
                for item in self.state["oauthIdentities"]
                if item.get("revoked_at") is None
                and str(item.get("provider") or "").lower() == norm_provider
                and str(item.get("instance_url") or "").lower() == norm_instance.lower()
                and str(item.get("provider_user_id") or "") == uid
            ),
            None,
        )
        if active_conflict:
            if active_conflict["user_id"] != user_id:
                raise GithubIdentityConflict("该 OAuth 账号已绑定到其他市场账号")
            active_conflict.update(
                {
                    "provider_login": str(provider_login or ""),
                    "provider_email": str(provider_email or "")
                    or active_conflict.get("provider_email", ""),
                    "avatar_url": str(avatar_url or "") or active_conflict.get("avatar_url", ""),
                    "scopes": str(scopes or "") or active_conflict.get("scopes", ""),
                    "credential_ref": str(credential_ref or "")
                    or active_conflict.get("credential_ref", ""),
                    "expires_at": expires_at
                    if expires_at is not None
                    else active_conflict.get("expires_at"),
                    "updated_at": utc_now(),
                }
            )
            return deepcopy(active_conflict)

        record = {
            "id": self._next_id("oauth"),
            "user_id": user_id,
            "provider": norm_provider,
            "instance_url": norm_instance,
            "provider_user_id": uid,
            "provider_login": str(provider_login or ""),
            "provider_email": str(provider_email or ""),
            "avatar_url": str(avatar_url or ""),
            "scopes": str(scopes or ""),
            "credential_ref": str(credential_ref or ""),
            "expires_at": expires_at,
            "revoked_at": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self.state["oauthIdentities"].append(record)
        return deepcopy(record)

    def revoke_oauth_identity(self, user_id: str, identity_id: str) -> dict[str, Any] | None:
        target = next(
            (
                item
                for item in self.state["oauthIdentities"]
                if item.get("id") == identity_id
                and item.get("user_id") == user_id
                and item.get("revoked_at") is None
            ),
            None,
        )
        if not target:
            return None
        now = utc_now()
        target["revoked_at"] = now
        target["updated_at"] = now
        cred_ref = str(target.get("credential_ref") or "").strip()
        if cred_ref:
            self.state.setdefault("oauthCredentials", {}).pop(cred_ref, None)
        if target.get("provider") == "github":
            user = self.get_user_by_id(user_id)
            remaining = [
                item
                for item in self.list_oauth_identities(user_id)
                if item.get("provider") == "github"
            ]
            latest = max(
                remaining,
                key=lambda item: (
                    str(item.get("updated_at") or ""),
                    str(item.get("created_at") or ""),
                    str(item.get("id") or ""),
                ),
                default=None,
            )
            if user:
                user["github_id"] = str(latest["provider_user_id"]) if latest else None
                user["github_login"] = str(latest.get("provider_login") or "") if latest else ""
                user["github_email"] = str(latest.get("provider_email") or "") if latest else ""
                user["updated_at"] = now
        return deepcopy(target)

    def update_oauth_identity_rotation(
        self,
        identity_id: str,
        *,
        credential_ref: str = "",
        expires_at: str | None = None,
        rotation_status: str = "",
        token_refreshed_at: str | None = None,
    ) -> dict[str, Any] | None:
        target = next(
            (
                item
                for item in self.state["oauthIdentities"]
                if item.get("id") == identity_id and item.get("revoked_at") is None
            ),
            None,
        )
        if not target:
            return None
        if credential_ref:
            target["credential_ref"] = str(credential_ref)
        if expires_at is not None:
            target["expires_at"] = expires_at
        if rotation_status:
            target["rotation_status"] = str(rotation_status)
        if token_refreshed_at is not None:
            target["token_refreshed_at"] = token_refreshed_at
        target["updated_at"] = utc_now()
        return deepcopy(target)

    def upsert_oauth_credential(
        self,
        credential_ref: str,
        *,
        user_id: str,
        provider: str,
        ciphertext: str,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        creds = self.state.setdefault("oauthCredentials", {})
        existing = creds.get(credential_ref) or {}
        now = utc_now()
        record = {
            "credential_ref": str(credential_ref),
            "user_id": str(user_id),
            "provider": str(provider or "").strip().lower(),
            "ciphertext": str(ciphertext),
            "expires_at": expires_at,
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
        }
        creds[str(credential_ref)] = record
        return deepcopy(record)

    def get_oauth_credential(self, credential_ref: str) -> dict[str, Any] | None:
        record = self.state.setdefault("oauthCredentials", {}).get(str(credential_ref))
        return deepcopy(record) if record else None

    def list_oauth_credentials_by_user(self, user_id: str) -> list[dict[str, Any]]:
        uid = str(user_id)
        return [
            deepcopy(record)
            for record in self.state.setdefault("oauthCredentials", {}).values()
            if str(record.get("user_id") or "") == uid
        ]

    def delete_oauth_credential(self, credential_ref: str) -> bool:
        return (
            self.state.setdefault("oauthCredentials", {}).pop(str(credential_ref), None) is not None
        )

    def upsert_git_repository_binding(
        self,
        user_id: str,
        *,
        normalized_repo: str,
        repo_url: str,
        provider: str,
        instance_url: str = "",
        repo_external_id: str = "",
        owner_id: str = "",
        permission_level: str = "",
        verification_method: str = "oauth",
        credential_ref: str = "",
        verified_at: str | None = None,
    ) -> dict[str, Any]:
        bindings = self.state.setdefault("gitBindings", [])
        norm_key = str(normalized_repo or "").strip().lower()
        now = utc_now()
        active = next(
            (
                item
                for item in bindings
                if item.get("revoked_at") is None
                and str(item.get("normalized_repo") or "").lower() == norm_key
            ),
            None,
        )
        if active:
            if str(active.get("user_id")) != str(user_id):
                raise PluginIdentityConflict("该仓库已绑定到其他市场账号，请联系管理员核对")
            gen = int(active.get("binding_generation") or 1)
            identity_changed = (
                bool(active.get("repo_external_id"))
                and bool(repo_external_id)
                and str(active.get("repo_external_id")) != str(repo_external_id)
            ) or (
                bool(active.get("owner_id"))
                and bool(owner_id)
                and str(active.get("owner_id")) != str(owner_id)
            )
            if identity_changed:
                gen += 1
            active.update(
                {
                    "repo_url": str(repo_url),
                    "provider": str(provider or "").strip().lower(),
                    "instance_url": str(instance_url or active.get("instance_url") or ""),
                    "repo_external_id": str(
                        repo_external_id or active.get("repo_external_id") or ""
                    ),
                    "owner_id": str(owner_id or active.get("owner_id") or ""),
                    "permission_level": str(
                        permission_level or active.get("permission_level") or ""
                    ),
                    "verification_method": str(verification_method or "oauth"),
                    "credential_ref": str(credential_ref or active.get("credential_ref") or ""),
                    "binding_generation": gen,
                    "verified_at": verified_at or now,
                    "updated_at": now,
                }
            )
            return deepcopy(active)

        prior_max = max(
            (
                int(item.get("binding_generation") or 0)
                for item in bindings
                if str(item.get("normalized_repo") or "").lower() == norm_key
            ),
            default=0,
        )
        record = {
            "id": self._next_id("gitbind"),
            "user_id": str(user_id),
            "normalized_repo": norm_key,
            "repo_url": str(repo_url),
            "provider": str(provider or "").strip().lower(),
            "instance_url": str(instance_url or ""),
            "repo_external_id": str(repo_external_id or ""),
            "owner_id": str(owner_id or ""),
            "permission_level": str(permission_level or ""),
            "verification_method": str(verification_method or "oauth"),
            "credential_ref": str(credential_ref or ""),
            "binding_generation": prior_max + 1,
            "verified_at": verified_at or now,
            "revoked_at": None,
            "revoked_by": None,
            "revoke_reason": "",
            "created_at": now,
            "updated_at": now,
        }
        bindings.append(record)
        return deepcopy(record)

    def get_active_git_repository_binding(self, normalized_repo: str) -> dict[str, Any] | None:
        norm_key = str(normalized_repo or "").strip().lower()
        return next(
            (
                deepcopy(item)
                for item in self.state.setdefault("gitBindings", [])
                if item.get("revoked_at") is None
                and str(item.get("normalized_repo") or "").lower() == norm_key
            ),
            None,
        )

    def list_git_repository_bindings(self, include_revoked: bool = False) -> list[dict[str, Any]]:
        return [
            deepcopy(item)
            for item in reversed(self.state.setdefault("gitBindings", []))
            if include_revoked or item.get("revoked_at") is None
        ]

    def revoke_git_repository_binding(
        self,
        binding_id: str,
        *,
        revoked_by: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        target = next(
            (
                item
                for item in self.state.setdefault("gitBindings", [])
                if item.get("id") == binding_id and item.get("revoked_at") is None
            ),
            None,
        )
        if not target:
            return None
        now = utc_now()
        target["revoked_at"] = now
        target["revoked_by"] = str(revoked_by)
        target["revoke_reason"] = str(reason or "")
        target["updated_at"] = now
        return deepcopy(target)

    def transfer_git_repository_binding(
        self,
        binding_id: str,
        *,
        target_user_id: str,
        actor_user_id: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        bindings = self.state.setdefault("gitBindings", [])
        old = next(
            (
                item
                for item in bindings
                if item.get("id") == binding_id and item.get("revoked_at") is None
            ),
            None,
        )
        if not old:
            return None
        now = utc_now()
        old["revoked_at"] = now
        old["revoked_by"] = str(actor_user_id)
        old["revoke_reason"] = str(reason or "管理员转移绑定")
        old["updated_at"] = now
        new_record = {
            "id": self._next_id("gitbind"),
            "user_id": str(target_user_id),
            "normalized_repo": old["normalized_repo"],
            "repo_url": old["repo_url"],
            "provider": old["provider"],
            "instance_url": old.get("instance_url") or "",
            "repo_external_id": old.get("repo_external_id") or "",
            "owner_id": old.get("owner_id") or "",
            "permission_level": old.get("permission_level") or "",
            "verification_method": old.get("verification_method") or "oauth",
            "credential_ref": "",
            "binding_generation": int(old.get("binding_generation") or 1) + 1,
            "verified_at": now,
            "revoked_at": None,
            "revoked_by": None,
            "revoke_reason": "",
            "created_at": now,
            "updated_at": now,
        }
        bindings.append(new_record)
        return deepcopy(new_record)

    def list_github_authorization_references(self, user_id: str) -> list[str]:
        references = [
            hashlib.sha256(str(session["token"]).encode()).hexdigest()
            for session in self.state["sessions"]
            if session.get("user_id") == user_id
        ]
        latest = self.get_latest_github_authorization(user_id)
        if latest:
            references.append(latest)
        return list(dict.fromkeys(references))

    def clear_latest_github_authorization(self, user_id: str) -> None:
        getattr(self, "_github_authorization_references", {}).pop(user_id, None)

    def list_public_plugins(self) -> list[dict[str, Any]]:
        return [
            deepcopy(plugin) for plugin in self.state["plugins"] if plugin["status"] == "listed"
        ]

    def list_due_github_sync_plugins(self, limit: int) -> list[dict[str, Any]]:
        now = utc_now()
        due = [
            plugin
            for plugin in self.state["plugins"]
            if plugin["status"] == "listed" and str(plugin.get("github_next_sync_at") or "") <= now
        ]
        due.sort(key=lambda plugin: str(plugin.get("github_next_sync_at") or ""))
        return deepcopy(due[:limit])

    def list_users(self) -> list[dict[str, Any]]:
        return deepcopy(self.state["users"])

    def list_plugins(self) -> list[dict[str, Any]]:
        return deepcopy(self.state["plugins"])

    def list_author_plugins(self, username: str) -> list[dict[str, Any]]:
        target = str(username or "").strip().lower()
        return [
            deepcopy(plugin)
            for plugin in self.state["plugins"]
            if str(plugin.get("owner_username") or plugin.get("owner_github_login") or "").lower()
            == target
            and plugin.get("status") == "listed"
        ]

    def has_active_plugins(self, user_id: str) -> bool:
        return any(
            plugin.get("owner_user_id") == user_id and plugin.get("status") in {"listed", "pending"}
            for plugin in self.state["plugins"]
        )

    def list_user_plugins(self, user_id: str, github_login: str = "") -> list[dict[str, Any]]:
        return [
            deepcopy(plugin)
            for plugin in self.state["plugins"]
            if plugin.get("owner_user_id") == user_id
        ]

    def list_submissions(self) -> list[dict[str, Any]]:
        return [
            deepcopy(submission)
            for submission in self.state["submissions"]
            if submission.get("status") == "pending"
        ]

    def list_api_keys(self) -> list[dict[str, Any]]:
        return deepcopy(self.state["apiKeys"])

    def list_api_keys_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [
            deepcopy(api_key)
            for api_key in reversed(self.state["apiKeys"])
            if api_key.get("user_id") == user_id
        ]

    def list_options(self) -> dict[str, str]:
        return deepcopy(self.state["options"])

    def upsert_options(self, values: dict[str, str]) -> dict[str, str]:
        self.state["options"].update({key: str(value) for key, value in values.items()})
        return self.list_options()

    def summary(self) -> dict[str, int]:
        return {
            "users": len(self.state["users"]),
            "plugins": len(self.state["plugins"]),
            "submissions": len(self.state["submissions"]),
            "announcements": len(self.state["announcements"]),
        }

    def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        return self._find("plugins", "id", plugin_id)

    def get_plugin_by_author(self, username: str, name: str) -> dict[str, Any] | None:
        target = str(username or "").strip().lower()
        return next(
            (
                p
                for p in self.state["plugins"]
                if str(p.get("owner_username") or p.get("owner_github_login") or "").lower()
                == target
                and p["name"].lower() == name.lower()
            ),
            None,
        )

    def get_owned_plugin_by_name(self, user_id: str, name: str) -> dict[str, Any] | None:
        return next(
            (
                p
                for p in self.state["plugins"]
                if p.get("owner_user_id") == user_id and p["name"].lower() == name.lower()
            ),
            None,
        )

    def _plugin_registration_identity(
        self, user: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        existing = (
            self.get_plugin(str(payload["id"]))
            if payload.get("id")
            else self.get_owned_plugin_by_name(user["id"], payload["name"])
        )
        if existing and existing.get("owner_user_id") != user["id"]:
            raise PermissionError("plugin_owner_mismatch")
        if existing:
            _, _, existing_repo_key = _canonical_repo_fields(
                str(existing.get("normalized_repo") or existing.get("repo") or "")
            )
            _, _, payload_repo_key = _canonical_repo_fields(
                str(payload.get("normalized_repo") or payload.get("repo") or "")
            )
            if existing_repo_key != payload_repo_key:
                raise PluginIdentityConflict("该用户名下的插件名已关联其他仓库")
        plugin_id = existing["id"] if existing else (payload.get("id") or payload["name"])
        if not existing and self.get_plugin(plugin_id):
            plugin_id = new_id("astrbot_plugin")
        owner_username = (
            (existing or {}).get("owner_username")
            or (existing or {}).get("owner_github_login")
            or user.get("public_username")
            or user.get("github_login")
            or user.get("internal_username")
            or ""
        )
        self._check_plugin_identity(
            {
                **payload,
                "id": plugin_id,
                "owner_user_id": user["id"],
                "owner_github_login": (existing or {}).get(
                    "owner_github_login", user.get("github_login", "")
                ),
                "owner_username": owner_username,
            }
        )
        return plugin_id, existing

    def _check_plugin_identity(self, candidate: dict[str, Any]) -> None:
        candidate_name = str(candidate.get("name") or "").strip().lower()
        candidate_author = str(candidate.get("author") or "").strip().lower()
        login = str(candidate.get("owner_github_login") or "").lower()
        owner_username = str(candidate.get("owner_username") or login).lower()
        for other in self.state["plugins"]:
            if other["id"] == candidate["id"]:
                continue
            same_name = str(other.get("name") or "").strip().lower() == candidate_name
            same_owner = other.get("owner_user_id") == candidate.get("owner_user_id")
            other_login = str(other.get("owner_github_login") or "").lower()
            other_username = str(other.get("owner_username") or other_login).lower()
            same_login = bool(login) and other_login == login
            same_username = bool(owner_username) and other_username == owner_username
            if same_name and (same_owner or same_login or same_username):
                raise PluginIdentityConflict("该用户名下已存在同名插件")
            other_author = str(other.get("author") or "").strip().lower()
            if same_name and candidate_author and other_author == candidate_author:
                raise PluginIdentityConflict("已存在相同作者与插件名组合的插件")
            _, _, other_repo_key = _canonical_repo_fields(
                str(other.get("normalized_repo") or other.get("repo") or "")
            )
            _, _, candidate_repo_key = _canonical_repo_fields(
                str(candidate.get("normalized_repo") or candidate.get("repo") or "")
            )
            if other_repo_key and other_repo_key == candidate_repo_key:
                raise PluginIdentityConflict("该仓库已登记为其他插件")

    def get_comment(self, comment_id: str) -> dict[str, Any] | None:
        return self._find("comments", "id", comment_id)

    def submit_plugin(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        plugin_id, existing = self._plugin_registration_identity(user, payload)
        plugin = self._normalize_plugin(
            {
                **(existing or {}),
                **plugin_metadata_from_payload(payload),
                **(
                    {
                        key: existing[key]
                        for key in ("cdn_enabled", "cdn_generation", "cdn_whitelist")
                        if key in existing
                    }
                    if existing
                    else {}
                ),
                "id": plugin_id,
                "name": payload["name"],
                "display_name": payload.get("display_name") or "",
                "desc": payload["desc"],
                "author": payload["author"],
                "repo": payload["repo"],
                "tags": payload.get("tags", []),
                "social_link": payload.get("social_link", ""),
                "category": payload.get("category", (existing or {}).get("category", "other")),
                "category_source": (
                    "user"
                    if "category" in payload
                    else (existing or {}).get("category_source", "user")
                ),
                "repo_version": payload.get("repo_version")
                or payload.get("version")
                or (existing or {}).get("repo_version", ""),
                "current_artifact_id": (existing or {}).get("current_artifact_id"),
                "owner_user_id": user["id"],
                "owner_github_login": (existing or {}).get(
                    "owner_github_login", user.get("github_login", "")
                ),
                "owner_username": (existing or {}).get("owner_username")
                or (existing or {}).get("owner_github_login")
                or user.get("public_username")
                or user.get("github_login")
                or user.get("internal_username")
                or "",
                "status": (existing or {}).get("status", "pending"),
                "stars": int((existing or {}).get("stars") or 0),
                "likes": int((existing or {}).get("likes") or 0),
                "comments_count": int((existing or {}).get("comments_count") or 0),
                "created_at": (existing or {}).get("created_at") or utc_now(),
                "version_updated_at": (
                    utc_now()
                    if str(payload.get("repo_version") or payload.get("version") or "")
                    and str(payload.get("repo_version") or payload.get("version"))
                    != str((existing or {}).get("repo_version") or "")
                    else (existing or {}).get("version_updated_at") or utc_now()
                ),
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

    def register_plugin(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        plugin_id, existing = self._plugin_registration_identity(user, payload)
        if existing:
            if existing.get("owner_user_id") != user.get("id"):
                raise PermissionError("plugin_owner_mismatch")
            return existing
        repo_provider, repo_external_id, normalized_repo = _canonical_repo_fields(
            str(payload.get("repo") or ""),
            str(payload.get("repo_provider") or ""),
            str(payload.get("repo_external_id") or ""),
        )
        plugin = self._normalize_plugin(
            {
                **plugin_metadata_from_payload(payload),
                "id": plugin_id,
                "name": payload["name"],
                "display_name": payload.get("display_name") or "",
                "desc": payload["desc"],
                "author": payload["author"],
                "repo": payload["repo"],
                "repo_provider": repo_provider,
                "repo_external_id": repo_external_id,
                "normalized_repo": normalized_repo,
                "tags": payload.get("tags", []),
                "category": payload.get("category", "other"),
                "category_source": "user",
                "owner_user_id": user["id"],
                "owner_github_login": user.get("github_login", ""),
                "owner_username": user.get("public_username")
                or user.get("github_login")
                or user.get("internal_username")
                or "",
                "status": "pending",
                "stars": 0,
                "likes": 0,
                "comments_count": 0,
                "repo_version": payload.get("version", ""),
                "current_artifact_id": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "version_updated_at": utc_now(),
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
        if status == "listed":
            plugin["listed_at"] = utc_now()
            for key in ("unlist_reason", "unlisted_at", "unlisted_by"):
                plugin.pop(key, None)
        self._complete_pending_submissions(plugin_id, status)
        plugin["updated_at"] = utc_now()
        return deepcopy(plugin)

    def unlist_plugin(
        self,
        plugin_id: str,
        by_user_id: str,
        reason: str,
    ) -> dict[str, Any] | None:
        plugin = self.get_plugin(plugin_id)
        if not plugin:
            return None
        plugin["status"] = "unlisted"
        plugin["moderated_by"] = by_user_id
        plugin["unlist_reason"] = reason
        plugin["unlisted_at"] = utc_now()
        plugin["unlisted_by"] = by_user_id
        plugin["updated_at"] = utc_now()
        self._complete_pending_submissions(plugin_id, "unlisted")
        return deepcopy(plugin)

    def request_plugin_listing(
        self,
        plugin_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        plugin = self.get_plugin(plugin_id)
        if not plugin:
            return None
        plugin["status"] = "pending"
        plugin["moderated_by"] = None
        plugin["updated_at"] = utc_now()
        payload = submission_payload_from_plugin(plugin)
        existing = next(
            (
                submission
                for submission in self.state["submissions"]
                if submission.get("plugin_id") == plugin_id
                and submission.get("status") == "pending"
            ),
            None,
        )
        if existing:
            existing["payload"] = deepcopy(payload)
            existing["user_id"] = user_id
        else:
            self.state["submissions"].append(
                {
                    "id": self._next_id("submission"),
                    "plugin_id": plugin_id,
                    "user_id": user_id,
                    "payload": deepcopy(payload),
                    "status": "pending",
                    "created_at": utc_now(),
                }
            )
        return deepcopy(plugin)

    def update_plugin_metadata(
        self,
        plugin_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        plugin = self.get_plugin(plugin_id)
        if not plugin:
            return None
        changes = {key: value for key, value in patch.items() if value is not None}
        changes.pop("version_updated_at", None)
        if "repo" in changes:
            new_repo = str(changes["repo"])
            prev_norm = str(plugin.get("normalized_repo") or "")
            repo_provider, repo_external_id, normalized_repo = _canonical_repo_fields(
                new_repo,
                str(changes.get("repo_provider") or ""),
                str(changes.get("repo_external_id") or ""),
            )
            if normalized_repo == prev_norm:
                if "repo_provider" not in changes and not repo_provider:
                    repo_provider = str(plugin.get("repo_provider") or "")
                if "repo_external_id" not in changes:
                    repo_external_id = str(plugin.get("repo_external_id") or "")
            changes["repo_provider"] = repo_provider
            changes["repo_external_id"] = repo_external_id
            changes["normalized_repo"] = normalized_repo
        new_repo_version = str(changes.get("repo_version") or "")
        if new_repo_version and new_repo_version != str(plugin.get("repo_version") or ""):
            changes["version_updated_at"] = utc_now()
        self._check_plugin_identity({**plugin, **changes})
        plugin.update(changes)
        plugin["updated_at"] = utc_now()
        return deepcopy(plugin)

    def _complete_pending_submissions(self, plugin_id: str, status: str) -> None:
        if status == "pending":
            return
        for submission in self.state["submissions"]:
            if submission.get("plugin_id") == plugin_id and submission.get("status") == "pending":
                submission["status"] = status

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
            "likes": 0,
            "created_at": utc_now(),
        }
        self.state["comments"].append(comment)
        plugin = self.get_plugin(plugin_id)
        if plugin:
            plugin["comments_count"] = len(self.list_comments(plugin_id))
        return deepcopy(comment)

    def like_plugin(self, plugin_id: str, user_id: str) -> dict[str, Any] | None:
        plugin = self.get_plugin(plugin_id)
        if not plugin:
            return None
        if not self._has_like("pluginLikes", plugin_id, user_id):
            self.state["pluginLikes"].append({"plugin_id": plugin_id, "user_id": user_id})
            plugin["likes"] = int(plugin.get("likes") or 0) + 1
            plugin["updated_at"] = utc_now()
        return deepcopy({**plugin, "liked": True})

    def unlike_plugin(self, plugin_id: str, user_id: str) -> dict[str, Any] | None:
        plugin = self.get_plugin(plugin_id)
        if not plugin:
            return None
        before = len(self.state["pluginLikes"])
        self.state["pluginLikes"] = [
            like
            for like in self.state["pluginLikes"]
            if not (like["plugin_id"] == plugin_id and like["user_id"] == user_id)
        ]
        if len(self.state["pluginLikes"]) != before:
            plugin["likes"] = max(0, int(plugin.get("likes") or 0) - 1)
            plugin["updated_at"] = utc_now()
        return deepcopy({**plugin, "liked": False})

    def like_comment(self, comment_id: str, user_id: str) -> dict[str, Any] | None:
        comment = self.get_comment(comment_id)
        if not comment or comment.get("deleted"):
            return None
        if not self._has_like("commentLikes", comment_id, user_id):
            self.state["commentLikes"].append({"comment_id": comment_id, "user_id": user_id})
            comment["likes"] = int(comment.get("likes") or 0) + 1
        return deepcopy({**comment, "liked": True})

    def unlike_comment(self, comment_id: str, user_id: str) -> dict[str, Any] | None:
        comment = self.get_comment(comment_id)
        if not comment or comment.get("deleted"):
            return None
        before = len(self.state["commentLikes"])
        self.state["commentLikes"] = [
            like
            for like in self.state["commentLikes"]
            if not (like["comment_id"] == comment_id and like["user_id"] == user_id)
        ]
        if len(self.state["commentLikes"]) != before:
            comment["likes"] = max(0, int(comment.get("likes") or 0) - 1)
        return deepcopy({**comment, "liked": False})

    def delete_comment(self, comment_id: str, by_user_id: str) -> dict[str, Any] | None:
        comment = self._find("comments", "id", comment_id)
        if not comment:
            return None
        comment["deleted"] = True
        comment["deleted_by"] = by_user_id
        comment["deleted_at"] = utc_now()
        for reply in self.state["comments"]:
            if reply.get("parent_id") == comment_id and not reply.get("deleted"):
                reply["deleted"] = True
                reply["deleted_by"] = by_user_id
                reply["deleted_at"] = utc_now()
        plugin = self.get_plugin(comment["plugin_id"])
        if plugin:
            plugin["comments_count"] = len(self.list_comments(comment["plugin_id"]))
        return deepcopy(comment)

    def mute_user(
        self,
        user_id: str,
        muted_until: str,
        by_user_id: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        user["muted_until"] = muted_until
        user["muted_by"] = by_user_id
        user["muted_reason"] = reason
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def unmute_user(self, user_id: str) -> dict[str, Any] | None:
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        user["muted_until"] = None
        user["muted_by"] = None
        user["muted_reason"] = ""
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def update_user_role(self, user_id: str, role: str) -> dict[str, Any] | None:
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        user["role"] = normalize_role(role).value
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def delete_user(self, user_id: str, replacement_user_id: str) -> dict[str, Any] | None:
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        for plugin in self.state["plugins"]:
            if plugin.get("owner_user_id") == user_id:
                self._check_plugin_identity({**plugin, "owner_user_id": replacement_user_id})
        for plugin in self.state["plugins"]:
            if plugin.get("owner_user_id") == user_id:
                plugin["owner_user_id"] = replacement_user_id
                plugin["updated_at"] = utc_now()
            if plugin.get("moderated_by") == user_id:
                plugin["moderated_by"] = None
        removed_comment_ids = {
            comment["id"] for comment in self.state["comments"] if comment.get("user_id") == user_id
        }
        self.state["comments"] = [
            comment for comment in self.state["comments"] if comment.get("user_id") != user_id
        ]
        for comment in self.state["comments"]:
            if comment.get("parent_id") in removed_comment_ids:
                comment["parent_id"] = None
            if comment.get("deleted_by") == user_id:
                comment["deleted_by"] = None
        self.state["submissions"] = [
            submission
            for submission in self.state["submissions"]
            if submission.get("user_id") != user_id
        ]
        self.state["announcements"] = [
            announcement
            for announcement in self.state["announcements"]
            if announcement.get("author_user_id") != user_id
        ]
        self.state["pluginLikes"] = [
            like for like in self.state["pluginLikes"] if like.get("user_id") != user_id
        ]
        self.state["commentLikes"] = [
            like
            for like in self.state["commentLikes"]
            if like.get("user_id") != user_id and like.get("comment_id") not in removed_comment_ids
        ]
        for target in self.state["users"]:
            if target.get("muted_by") == user_id:
                target["muted_by"] = None
        for collection in ("notifications", "apiKeys", "sessions", "oauthIdentities"):
            self.state[collection] = [
                item for item in self.state[collection] if item.get("user_id") != user_id
            ]
        self.state["oauthCredentials"] = {
            ref: cred
            for ref, cred in self.state.setdefault("oauthCredentials", {}).items()
            if cred.get("user_id") != user_id
        }
        for binding in self.state.setdefault("gitBindings", []):
            if binding.get("user_id") == user_id and binding.get("revoked_at") is None:
                binding["user_id"] = replacement_user_id
                binding["credential_ref"] = ""
                binding["binding_generation"] = int(binding.get("binding_generation") or 1) + 1
                binding["updated_at"] = utc_now()
        self.state["users"] = [item for item in self.state["users"] if item["id"] != user_id]
        self._refresh_like_counts()
        for plugin in self.state["plugins"]:
            plugin["comments_count"] = len(self.list_comments(plugin["id"]))
        return deepcopy(user)

    def update_user_profile(
        self,
        user_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any] | None:
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        for key in USER_PROFILE_FIELDS:
            if key in profile:
                user[key] = profile[key]
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def set_user_uploaded_avatar(
        self,
        user_id: str,
        *,
        url: str,
        profile_id: str,
        object_id: str,
        sha256: str,
        expected_object_id: str,
    ) -> dict[str, Any] | None:
        """Point the user at a new uploaded avatar; fails if another upload landed first."""
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        if (user.get("uploaded_avatar_object_id") or "") != expected_object_id:
            return None
        user["uploaded_avatar_url"] = url
        user["uploaded_avatar_profile_id"] = profile_id
        user["uploaded_avatar_object_id"] = object_id
        user["uploaded_avatar_sha256"] = sha256
        user["uploaded_avatar_updated_at"] = utc_now()
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def clear_user_uploaded_avatar(
        self,
        user_id: str,
        *,
        expected_object_id: str,
    ) -> dict[str, Any] | None:
        """Drop the uploaded avatar reference; the remote object is left for storage GC."""
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        if (user.get("uploaded_avatar_object_id") or "") != expected_object_id:
            return None
        user["uploaded_avatar_url"] = ""
        user["uploaded_avatar_profile_id"] = ""
        user["uploaded_avatar_object_id"] = ""
        user["uploaded_avatar_sha256"] = ""
        user["uploaded_avatar_updated_at"] = None
        user["updated_at"] = utc_now()
        return deepcopy(user)

    def merge_user_into_user(self, from_user_id: str, to_user_id: str) -> dict[str, Any] | None:
        source = self.get_user_by_id(from_user_id)
        target = self.get_user_by_id(to_user_id)
        if not source or not target:
            return target

        for plugin in self.state["plugins"]:
            if plugin.get("owner_user_id") == from_user_id:
                self._check_plugin_identity({**plugin, "owner_user_id": to_user_id})
        for plugin in self.state["plugins"]:
            if plugin.get("owner_user_id") == from_user_id:
                plugin["owner_user_id"] = to_user_id
            if plugin.get("moderated_by") == from_user_id:
                plugin["moderated_by"] = to_user_id
        for submission in self.state["submissions"]:
            if submission.get("user_id") == from_user_id:
                submission["user_id"] = to_user_id
        for comment in self.state["comments"]:
            if comment.get("user_id") == from_user_id:
                comment["user_id"] = to_user_id
            if comment.get("deleted_by") == from_user_id:
                comment["deleted_by"] = to_user_id
        for announcement in self.state["announcements"]:
            if announcement.get("author_user_id") == from_user_id:
                announcement["author_user_id"] = to_user_id
        for notification in self.state["notifications"]:
            if notification.get("user_id") == from_user_id:
                notification["user_id"] = to_user_id
        for api_key in self.state["apiKeys"]:
            if api_key.get("user_id") == from_user_id:
                api_key["user_id"] = to_user_id
        for identity in self.state["oauthIdentities"]:
            if identity.get("user_id") == from_user_id:
                identity["user_id"] = to_user_id
        for cred in self.state.setdefault("oauthCredentials", {}).values():
            if cred.get("user_id") == from_user_id:
                cred["user_id"] = to_user_id
        for binding in self.state.setdefault("gitBindings", []):
            if binding.get("user_id") == from_user_id:
                binding["user_id"] = to_user_id
        self._merge_likes("pluginLikes", "plugin_id", from_user_id, to_user_id)
        self._merge_likes("commentLikes", "comment_id", from_user_id, to_user_id)
        self._refresh_like_counts()
        for user in self.state["users"]:
            if user.get("muted_by") == from_user_id:
                user["muted_by"] = to_user_id
        # An uploaded avatar is a first-class profile field: carry it over when the surviving
        # account has none, so a merge never silently degrades the target to the fallback image.
        if not (target.get("uploaded_avatar_object_id") or "") and source.get(
            "uploaded_avatar_object_id"
        ):
            target["uploaded_avatar_url"] = source["uploaded_avatar_url"]
            target["uploaded_avatar_profile_id"] = source["uploaded_avatar_profile_id"]
            target["uploaded_avatar_object_id"] = source["uploaded_avatar_object_id"]
            target["uploaded_avatar_sha256"] = source["uploaded_avatar_sha256"]
            target["uploaded_avatar_updated_at"] = source.get("uploaded_avatar_updated_at")
            target["updated_at"] = utc_now()
        self.state["users"] = [user for user in self.state["users"] if user["id"] != from_user_id]
        return deepcopy(target)

    def publish_announcement(
        self,
        title: str,
        body: str,
        author_user_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        announcement = {
            "id": self._next_id("announcement"),
            "title": title,
            "body": body,
            "author_user_id": author_user_id,
            "created_at": now,
            "updated_at": now,
        }
        self.state["announcements"].insert(0, announcement)
        return deepcopy(announcement)

    def update_announcement(
        self,
        announcement_id: str | int,
        *,
        title: str | None = None,
        body: str | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        for announcement in self.state["announcements"]:
            if str(announcement["id"]) == str(announcement_id):
                current_updated_at = announcement.get("updated_at") or announcement.get(
                    "created_at"
                )
                if expected_updated_at is not None and current_updated_at != expected_updated_at:
                    raise AnnouncementConflict("Announcement was updated by another administrator")
                if title is not None:
                    announcement["title"] = title
                if body is not None:
                    announcement["body"] = body
                announcement["updated_at"] = utc_now()
                return deepcopy(announcement)
        return None

    def delete_announcement(self, announcement_id: str | int) -> bool:
        before = len(self.state["announcements"])
        self.state["announcements"] = [
            announcement
            for announcement in self.state["announcements"]
            if str(announcement["id"]) != str(announcement_id)
        ]
        return len(self.state["announcements"]) != before

    def list_announcements(self) -> list[dict[str, Any]]:
        return deepcopy(self.state["announcements"])

    def create_notification(
        self,
        user_id: str,
        title: str,
        body: str,
        notification_type: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        notification = {
            "id": self._next_id("notification"),
            "user_id": user_id,
            "type": notification_type,
            "title": title,
            "body": body,
            "metadata": metadata or {},
            "read": False,
            "created_at": utc_now(),
        }
        self.state["notifications"].insert(0, notification)
        return deepcopy(notification)

    def create_notification_once(
        self,
        user_id: str,
        title: str,
        body: str,
        notification_type: str,
        metadata: dict[str, Any],
        dedupe_key: str,
    ) -> dict[str, Any]:
        for notification in self.state["notifications"]:
            if notification.get("dedupe_key") == dedupe_key:
                return deepcopy(notification)
        notification = self.create_notification(
            user_id,
            title,
            body,
            notification_type,
            metadata,
        )
        self.state["notifications"][0]["dedupe_key"] = dedupe_key
        notification["dedupe_key"] = dedupe_key
        return notification

    def list_notifications(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        notifications = [
            deepcopy(notification)
            for notification in self.state["notifications"]
            if notification["user_id"] == user_id
        ]
        return notifications[offset : offset + limit]

    def count_notifications(self, user_id: str) -> int:
        return sum(
            1 for notification in self.state["notifications"] if notification["user_id"] == user_id
        )

    def count_unread_notifications(self, user_id: str) -> int:
        return sum(
            1
            for notification in self.state["notifications"]
            if notification["user_id"] == user_id and not notification.get("read")
        )

    def mark_notifications_read(self, user_id: str) -> int:
        updated = 0
        for notification in self.state["notifications"]:
            if notification["user_id"] == user_id and not notification.get("read"):
                notification["read"] = True
                updated += 1
        return updated

    def delete_notifications(
        self,
        user_id: str,
        notification_ids: list[str] | None = None,
    ) -> int:
        id_set = set(notification_ids or [])
        if notification_ids is not None and not id_set:
            return 0
        before = len(self.state["notifications"])
        self.state["notifications"] = [
            notification
            for notification in self.state["notifications"]
            if not (
                notification["user_id"] == user_id
                and (notification_ids is None or notification["id"] in id_set)
            )
        ]
        return before - len(self.state["notifications"])

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
            "key": generate_access_key(),
            "created_at": utc_now(),
        }
        self.state["apiKeys"].append(api_key)
        return deepcopy(api_key)

    def delete_api_key(self, user_id: str, key_id: str) -> int:
        before = len(self.state["apiKeys"])
        self.state["apiKeys"] = [
            api_key
            for api_key in self.state["apiKeys"]
            if not (api_key.get("user_id") == user_id and api_key.get("id") == key_id)
        ]
        return before - len(self.state["apiKeys"])

    def create_session(self, user_id: str, token: str | None = None) -> dict[str, Any]:
        user = self.get_user_by_id(user_id)
        session = {
            "token": token or f"sess_{secrets.token_urlsafe(24)}",
            "user_id": user_id,
            "session_version": int((user or {}).get("session_version") or 1),
            "created_at": utc_now(),
            "last_seen_at": utc_now(),
        }
        self.state["sessions"].append(session)
        return deepcopy(session)

    def get_user_by_session(self, token: str) -> dict[str, Any] | None:
        session = self._find("sessions", "token", token)
        if not session:
            return None
        user = self.get_user_by_id(session["user_id"])
        if not user:
            return None
        expected_version = int(user.get("session_version") or 1)
        if int(session.get("session_version") or 1) != expected_version:
            self.revoke_session(token)
            return None
        session["last_seen_at"] = utc_now()
        return user

    def revoke_session(self, token: str) -> bool:
        self.delete_github_authorization(hashlib.sha256(token.encode()).hexdigest())
        sessions = self.state["sessions"]
        index = next((i for i, item in enumerate(sessions) if item["token"] == token), -1)
        if index == -1:
            return False
        sessions.pop(index)
        return True

    def _revoke_sessions_for_user_locked(self, user_id: str) -> int:
        kept = []
        removed = 0
        for session in self.state["sessions"]:
            if session.get("user_id") == user_id:
                self.delete_github_authorization(
                    hashlib.sha256(str(session["token"]).encode()).hexdigest()
                )
                removed += 1
            else:
                kept.append(session)
        self.state["sessions"] = kept
        getattr(self, "_github_authorization_references", {}).pop(user_id, None)
        return removed

    def revoke_all_user_sessions(self, user_id: str) -> int:
        user = self.get_user_by_id(user_id)
        if user:
            user["session_version"] = int(user.get("session_version") or 1) + 1
            user["updated_at"] = utc_now()
        return self._revoke_sessions_for_user_locked(user_id)

    def save_email_challenge(
        self, challenge_id: str, record: dict[str, Any], ttl_seconds: int
    ) -> None:
        challenges = self.state.setdefault("emailChallenges", {})
        challenges[challenge_id] = {
            **deepcopy(record),
            "expires_at_ts": time.time() + max(1, int(ttl_seconds)),
        }

    def get_email_challenge_salt(self, challenge_id: str) -> str | None:
        challenges = self.state.setdefault("emailChallenges", {})
        entry = challenges.get(challenge_id)
        if not entry:
            return None
        if float(entry.get("expires_at_ts") or 0) <= time.time():
            challenges.pop(challenge_id, None)
            return None
        return str(entry.get("salt") or "") or None

    def reserve_email_challenge(
        self,
        challenge_id: str,
        purpose: str,
        expected_digest: str,
        max_attempts: int,
    ) -> tuple[str, dict[str, Any] | None]:
        challenges = self.state.setdefault("emailChallenges", {})
        entry = challenges.get(challenge_id)
        if not entry:
            return ("not_found", None)
        if float(entry.get("expires_at_ts") or 0) <= time.time():
            challenges.pop(challenge_id, None)
            return ("expired", None)
        if str(entry.get("purpose") or "") != purpose:
            return ("wrong_purpose", None)
        if str(entry.get("state") or "") != "pending":
            return ("in_progress", None)
        attempts = int(entry.get("attempts") or 0)
        if attempts >= max_attempts:
            challenges.pop(challenge_id, None)
            return ("too_many_attempts", None)
        if not secrets.compare_digest(str(entry.get("code_digest") or ""), expected_digest):
            attempts += 1
            entry["attempts"] = attempts
            if attempts >= max_attempts:
                challenges.pop(challenge_id, None)
                return ("too_many_attempts", None)
            return ("invalid_code", None)
        entry["state"] = "in_progress"
        return ("ok", deepcopy(entry.get("payload") or {}))

    def release_email_challenge(self, challenge_id: str) -> None:
        entry = self.state.setdefault("emailChallenges", {}).get(challenge_id)
        if entry and entry.get("state") == "in_progress":
            entry["state"] = "pending"

    def delete_email_challenge(self, challenge_id: str) -> None:
        self.state.setdefault("emailChallenges", {}).pop(challenge_id, None)

    def get_rate_limit_count(self, key: str) -> int:
        limits = self.state.setdefault("rateLimits", {})
        entry = limits.get(key)
        now = time.time()
        if not entry or float(entry.get("expires_at_ts") or 0) <= now:
            limits.pop(key, None)
            return 0
        return int(entry.get("count") or 0)

    def increment_rate_limit(self, key: str, ttl_seconds: int) -> int:
        limits = self.state.setdefault("rateLimits", {})
        now = time.time()
        entry = limits.get(key)
        if not entry or float(entry.get("expires_at_ts") or 0) <= now:
            entry = {"count": 0, "expires_at_ts": now + max(1, int(ttl_seconds))}
            limits[key] = entry
        entry["count"] = int(entry.get("count") or 0) + 1
        return int(entry["count"])

    def reserve_rate_limit(self, key: str, limit: int, ttl_seconds: int) -> bool:
        if self.get_rate_limit_count(key) >= int(limit):
            return False
        self.increment_rate_limit(key, ttl_seconds)
        return True

    def decrement_rate_limit(self, key: str) -> None:
        limits = self.state.setdefault("rateLimits", {})
        entry = limits.get(key)
        if entry:
            entry["count"] = max(0, int(entry.get("count") or 0) - 1)

    def check_and_set_cooldown(self, key: str, ttl_seconds: int) -> int:
        cooldowns = self.state.setdefault("cooldowns", {})
        now = time.time()
        expires_at = float(cooldowns.get(key) or 0)
        if expires_at > now:
            return max(1, int(expires_at - now + 0.999))
        cooldowns[key] = now + max(1, int(ttl_seconds))
        return 0

    def clear_cooldown(self, key: str) -> None:
        self.state.setdefault("cooldowns", {}).pop(key, None)

    def set_github_authorization(self, reference: str, ciphertext: str, ttl_seconds: int) -> None:
        credentials = getattr(self, "_github_authorizations", {})
        credentials[reference] = (ciphertext, time.time() + ttl_seconds)
        self._github_authorizations = credentials

    def get_github_authorization(self, reference: str) -> str | None:
        value = getattr(self, "_github_authorizations", {}).get(reference)
        if value and value[1] > time.time():
            return value[0]
        self.delete_github_authorization(reference)
        return None

    def delete_github_authorization(self, reference: str) -> None:
        getattr(self, "_github_authorizations", {}).pop(reference, None)

    def set_latest_github_authorization(
        self, user_id: str, reference: str, ttl_seconds: int
    ) -> None:
        references = getattr(self, "_github_authorization_references", {})
        references[user_id] = (reference, time.time() + ttl_seconds)
        self._github_authorization_references = references

    def get_latest_github_authorization(self, user_id: str) -> str | None:
        value = getattr(self, "_github_authorization_references", {}).get(user_id)
        return value[0] if value and value[1] > time.time() else None

    def list_comments(self, plugin_id: str) -> list[dict[str, Any]]:
        comments = []
        for comment in self.state["comments"]:
            if comment["plugin_id"] != plugin_id or comment.get("deleted"):
                continue
            user = self.get_user_by_id(comment["user_id"]) or {}
            comments.append(self._comment_with_user(comment, user))
        return comments

    def has_plugin_like(self, plugin_id: str, user_id: str) -> bool:
        return self._has_like("pluginLikes", plugin_id, user_id)

    def list_liked_comment_ids(self, plugin_id: str, user_id: str) -> list[str]:
        comment_ids = {
            comment["id"]
            for comment in self.state["comments"]
            if comment["plugin_id"] == plugin_id and not comment.get("deleted")
        }
        return [
            like["comment_id"]
            for like in self.state["commentLikes"]
            if like["user_id"] == user_id and like["comment_id"] in comment_ids
        ]

    def _next_id(self, prefix: str) -> str:
        next_id = f"{prefix}_{self.state['nextNumericId']}"
        self.state["nextNumericId"] += 1
        return next_id

    def _find(self, collection: str, key: str, value: str) -> dict[str, Any] | None:
        return next((item for item in self.state[collection] if item.get(key) == value), None)

    def _has_like(self, collection: str, target_id: str, user_id: str) -> bool:
        target_key = "plugin_id" if collection == "pluginLikes" else "comment_id"
        return any(
            like[target_key] == target_id and like["user_id"] == user_id
            for like in self.state[collection]
        )

    def _merge_likes(
        self,
        collection: str,
        target_key: str,
        from_user_id: str,
        to_user_id: str,
    ) -> None:
        existing = {
            like[target_key] for like in self.state[collection] if like["user_id"] == to_user_id
        }
        merged = []
        for like in self.state[collection]:
            if like["user_id"] != from_user_id:
                merged.append(like)
            elif like[target_key] not in existing:
                merged.append({**like, "user_id": to_user_id})
                existing.add(like[target_key])
        self.state[collection] = merged

    def _refresh_like_counts(self) -> None:
        plugin_like_counts: dict[str, int] = {}
        for like in self.state["pluginLikes"]:
            plugin_like_counts[like["plugin_id"]] = plugin_like_counts.get(like["plugin_id"], 0) + 1
        for plugin in self.state["plugins"]:
            plugin["likes"] = plugin_like_counts.get(plugin["id"], 0)

        comment_like_counts: dict[str, int] = {}
        for like in self.state["commentLikes"]:
            comment_like_counts[like["comment_id"]] = (
                comment_like_counts.get(like["comment_id"], 0) + 1
            )
        for comment in self.state["comments"]:
            comment["likes"] = comment_like_counts.get(comment["id"], 0)

    def _upsert_plugin(self, plugin: dict[str, Any]) -> None:
        existing = self.get_plugin(plugin["id"])
        if existing:
            existing.update(plugin)
            return
        self.state["plugins"].append(plugin)

    def _normalize_user(self, user: dict[str, Any]) -> dict[str, Any]:
        github_login = user.get("github_login") or user.get("login") or ""
        internal_username = user.get("internal_username") or ""
        public_username = user.get("public_username") or github_login or internal_username or ""
        return {
            **user,
            "role": normalize_role(user.get("role")).value,
            "github_login": github_login,
            "internal_username": internal_username,
            "public_username": public_username,
            "email": user.get("email") or "",
            "email_verified_at": user.get("email_verified_at") or None,
            "session_version": int(user.get("session_version") or 1),
            "uploaded_avatar_url": user.get("uploaded_avatar_url") or "",
            "uploaded_avatar_profile_id": user.get("uploaded_avatar_profile_id") or "",
            "uploaded_avatar_object_id": user.get("uploaded_avatar_object_id") or "",
            "uploaded_avatar_sha256": user.get("uploaded_avatar_sha256") or "",
            "uploaded_avatar_updated_at": user.get("uploaded_avatar_updated_at") or None,
            "password_hash": user.get("password_hash") or "",
            "auth_source": user.get("auth_source") or "github",
            "github_token": user.get("github_token") or "",
            "github_email": user.get("github_email") or "",
            "github_refresh_interval_seconds": int(
                user.get("github_refresh_interval_seconds") or 3600
            ),
            "notification_email": user.get("notification_email") or "",
            "notify_plugin_review": coerce_bool(user.get("notify_plugin_review"), True),
            "notify_comments": coerce_bool(user.get("notify_comments"), True),
            "notify_replies": coerce_bool(user.get("notify_replies"), True),
            "notify_likes": coerce_bool(user.get("notify_likes"), True),
            "notify_unlist": coerce_bool(user.get("notify_unlist"), True),
            "email_notify_plugin_review": coerce_bool(user.get("email_notify_plugin_review"), True),
            "email_notify_pending_review": coerce_bool(
                user.get("email_notify_pending_review"), True
            ),
            "email_notify_comments": coerce_bool(user.get("email_notify_comments"), False),
            "email_notify_replies": coerce_bool(user.get("email_notify_replies"), False),
            "email_notify_likes": coerce_bool(user.get("email_notify_likes"), False),
            "email_notify_unlist": coerce_bool(user.get("email_notify_unlist"), True),
            "github_name": user.get("github_name")
            or user.get("name")
            or github_login
            or public_username
            or internal_username
            or user.get("login")
            or "",
            "muted_until": user.get("muted_until") or None,
            "muted_reason": user.get("muted_reason") or "",
        }

    def _normalize_plugin(self, plugin: dict[str, Any]) -> dict[str, Any]:
        repo_provider, repo_external_id, normalized_repo = _canonical_repo_fields(
            str(plugin.get("repo") or ""),
            str(plugin.get("repo_provider") or ""),
            str(plugin.get("repo_external_id") or ""),
        )
        return {
            **plugin,
            "download_count": int(plugin.get("download_count") or 0),
            "repo_provider": str(plugin.get("repo_provider") or repo_provider),
            "repo_external_id": str(plugin.get("repo_external_id") or repo_external_id),
            "normalized_repo": str(plugin.get("normalized_repo") or normalized_repo).lower(),
            "owner_username": str(plugin.get("owner_username") or ""),
            "tags": plugin.get("tags") if isinstance(plugin.get("tags"), list) else [],
            "stars": int(plugin.get("stars") or 0),
            "likes": int(plugin.get("likes") or 0),
            "comments_count": int(plugin.get("comments_count") or 0),
            "status": plugin.get("status") or "pending",
            "suggested_category": str(plugin.get("suggested_category") or ""),
            "category_confidence": (
                float(plugin["category_confidence"])
                if plugin.get("category_confidence") is not None
                else None
            ),
            "category_reason": str(plugin.get("category_reason") or ""),
        }

    def _comment_with_user(self, comment: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        return {
            **deepcopy(comment),
            "likes": int(comment.get("likes") or 0),
            "public_username": user.get("public_username")
            or user.get("github_login")
            or user.get("internal_username")
            or "",
            "github_login": user.get("github_login") or "",
            "github_name": user.get("github_name")
            or user.get("public_username")
            or user.get("internal_username")
            or "",
            "avatar_url": user.get("avatar_url") or "",
            "role": normalize_role(user.get("role")).value,
        }


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _pg_timestamp(value: Any, *, error_message: str = "Invalid timestamp") -> datetime | None:
    """Convert an optional ISO timestamp or datetime into a UTC datetime for asyncpg."""
    if value in {None, ""}:
        return None
    parsed = _parse_utc_timestamp(value)
    if parsed is None:
        raise ValueError(error_message)
    return parsed


def _oauth_timestamp(value: Any) -> datetime | None:
    """Convert an optional OAuth timestamp for asyncpg."""
    return _pg_timestamp(value, error_message="Invalid OAuth timestamp")


class LocalFileMarketStore(InMemoryMarketStore):
    """Single-process store persisting market state and sessions in one atomic JSON file."""

    def __init__(
        self,
        data_path: str | Path,
        session_ttl_seconds: int = 60 * 60 * 24 * 7,
        seed: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(seed=seed)
        self.data_path = Path(data_path)
        self.session_ttl_seconds = max(1, int(session_ttl_seconds))
        self._lock = threading.RLock()
        self._write_depth = 0
        self._session_persisted_at: dict[str, float] = {}

    def connect(self) -> None:
        with self._lock:
            self.data_path.parent.mkdir(parents=True, exist_ok=True)
            loaded = False
            if self.data_path.exists():
                raw = self.data_path.read_text(encoding="utf-8").strip()
                if raw:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("Local market data file must contain a JSON object")
                    self._load_state_locked(data)
                    loaded = True
            pruned = self._prune_expired_sessions_locked()
            if not loaded or pruned:
                self._save_locked()

    def close(self) -> None:
        with self._lock:
            self._save_locked()

    def persist_object_storage(self) -> None:
        with self._lock:
            self._save_locked()

    def get_first_internal_core_admin(self) -> dict[str, Any] | None:
        with self._lock:
            for user in self.state["users"]:
                if (
                    user.get("role") == Role.CORE_ADMIN.value
                    and user.get("internal_username")
                    and user.get("password_hash")
                ):
                    return deepcopy(user)
            return None

    def _load_state_locked(self, data: dict[str, Any]) -> None:
        self.state = {
            "users": [self._normalize_user(user) for user in data.get("users", [])],
            "plugins": [self._normalize_plugin(plugin) for plugin in data.get("plugins", [])],
            "submissions": list(data.get("submissions", [])),
            "comments": list(data.get("comments", [])),
            "pluginLikes": list(data.get("pluginLikes", [])),
            "commentLikes": list(data.get("commentLikes", [])),
            "announcements": list(data.get("announcements", [])),
            "notifications": list(data.get("notifications", [])),
            "apiKeys": list(data.get("apiKeys", [])),
            "oauthIdentities": list(data.get("oauthIdentities", [])),
            "oauthCredentials": dict(data.get("oauthCredentials", {})),
            "gitBindings": list(data.get("gitBindings", [])),
            "emailChallenges": dict(data.get("emailChallenges", {})),
            "rateLimits": dict(data.get("rateLimits", {})),
            "cooldowns": dict(data.get("cooldowns", {})),
            "options": {
                str(key): str(value) for key, value in dict(data.get("options", {})).items()
            },
            "objectStorage": deepcopy(data.get("objectStorage", {})),
            "sessions": list(data.get("sessions", [])),
            "nextNumericId": max(1, int(data.get("nextNumericId") or 1)),
        }
        self._backfill_seeded_identities()

    def _prune_expired_sessions_locked(self) -> bool:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.session_ttl_seconds)
        kept = []
        pruned = False
        for session in self.state["sessions"]:
            seen = _parse_utc_timestamp(session.get("last_seen_at") or session.get("created_at"))
            if seen is None or seen < cutoff:
                pruned = True
                token = str(session.get("token") or "")
                if token:
                    self._session_persisted_at.pop(token, None)
                    self.delete_github_authorization(hashlib.sha256(token.encode()).hexdigest())
                continue
            kept.append(session)
        if pruned:
            self.state["sessions"] = kept
        return pruned

    def _save_locked(self) -> None:
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(serialize_value(self.state), ensure_ascii=False, indent=2) + "\n"
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.data_path.parent,
                prefix=f".{self.data_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.data_path)
            tmp_path = None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @contextmanager
    def _persist(self) -> Iterator[None]:
        with self._lock:
            self._write_depth += 1
            succeeded = False
            try:
                yield
                succeeded = True
            finally:
                self._write_depth -= 1
                if succeeded and self._write_depth == 0:
                    self._save_locked()

    def upsert_github_user(self, profile: dict[str, Any]) -> dict[str, Any]:
        with self._persist():
            return super().upsert_github_user(profile)

    def create_internal_admin(self, username: str, password_hash: str) -> dict[str, Any]:
        with self._persist():
            return super().create_internal_admin(username, password_hash)

    def create_internal_user(
        self,
        username: str,
        password_hash: str,
        role: str = Role.USER.value,
        email: str = "",
    ) -> dict[str, Any]:
        with self._persist():
            return super().create_internal_user(username, password_hash, role, email=email)

    def create_email_user(
        self,
        *,
        username: str,
        email: str,
        password_hash: str,
        email_verified_at: str | None = None,
        role: str = Role.USER.value,
    ) -> dict[str, Any]:
        with self._persist():
            return super().create_email_user(
                username=username,
                email=email,
                password_hash=password_hash,
                email_verified_at=email_verified_at,
                role=role,
            )

    def set_user_password(
        self,
        user_id: str,
        password_hash: str,
        *,
        bump_session_version: bool = True,
        verify_email_at: str | None = None,
        email: str | None = None,
        expected_email: str | None = None,
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().set_user_password(
                user_id,
                password_hash,
                bump_session_version=bump_session_version,
                verify_email_at=verify_email_at,
                email=email,
                expected_email=expected_email,
            )

    def update_user_email(
        self,
        user_id: str,
        new_email: str,
        *,
        email_verified_at: str | None,
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().update_user_email(
                user_id, new_email, email_verified_at=email_verified_at
            )

    def upsert_oauth_identity(
        self,
        user_id: str,
        *,
        provider: str,
        instance_url: str,
        provider_user_id: str,
        provider_login: str,
        provider_email: str = "",
        avatar_url: str = "",
        scopes: str = "",
        credential_ref: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        with self._persist():
            return super().upsert_oauth_identity(
                user_id,
                provider=provider,
                instance_url=instance_url,
                provider_user_id=provider_user_id,
                provider_login=provider_login,
                provider_email=provider_email,
                avatar_url=avatar_url,
                scopes=scopes,
                credential_ref=credential_ref,
                expires_at=expires_at,
            )

    def revoke_oauth_identity(self, user_id: str, identity_id: str) -> dict[str, Any] | None:
        with self._persist():
            return super().revoke_oauth_identity(user_id, identity_id)

    def create_oauth_user(
        self,
        *,
        provider: str,
        provider_login: str,
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        with self._persist():
            return super().create_oauth_user(
                provider=provider,
                provider_login=provider_login,
                display_name=display_name,
                avatar_url=avatar_url,
            )

    def update_oauth_identity_rotation(
        self,
        identity_id: str,
        *,
        credential_ref: str = "",
        expires_at: str | None = None,
        rotation_status: str = "",
        token_refreshed_at: str | None = None,
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().update_oauth_identity_rotation(
                identity_id,
                credential_ref=credential_ref,
                expires_at=expires_at,
                rotation_status=rotation_status,
                token_refreshed_at=token_refreshed_at,
            )

    def upsert_oauth_credential(
        self,
        credential_ref: str,
        *,
        user_id: str,
        provider: str,
        ciphertext: str,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        with self._persist():
            return super().upsert_oauth_credential(
                credential_ref,
                user_id=user_id,
                provider=provider,
                ciphertext=ciphertext,
                expires_at=expires_at,
            )

    def get_oauth_credential(self, credential_ref: str) -> dict[str, Any] | None:
        with self._lock:
            return super().get_oauth_credential(credential_ref)

    def list_oauth_credentials_by_user(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return super().list_oauth_credentials_by_user(user_id)

    def delete_oauth_credential(self, credential_ref: str) -> bool:
        with self._persist():
            return super().delete_oauth_credential(credential_ref)

    def upsert_git_repository_binding(
        self,
        user_id: str,
        *,
        normalized_repo: str,
        repo_url: str,
        provider: str,
        instance_url: str = "",
        repo_external_id: str = "",
        owner_id: str = "",
        permission_level: str = "",
        verification_method: str = "oauth",
        credential_ref: str = "",
        verified_at: str | None = None,
    ) -> dict[str, Any]:
        with self._persist():
            return super().upsert_git_repository_binding(
                user_id,
                normalized_repo=normalized_repo,
                repo_url=repo_url,
                provider=provider,
                instance_url=instance_url,
                repo_external_id=repo_external_id,
                owner_id=owner_id,
                permission_level=permission_level,
                verification_method=verification_method,
                credential_ref=credential_ref,
                verified_at=verified_at,
            )

    def get_active_git_repository_binding(self, normalized_repo: str) -> dict[str, Any] | None:
        with self._lock:
            return super().get_active_git_repository_binding(normalized_repo)

    def list_git_repository_bindings(self, include_revoked: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            return super().list_git_repository_bindings(include_revoked=include_revoked)

    def revoke_git_repository_binding(
        self,
        binding_id: str,
        *,
        revoked_by: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().revoke_git_repository_binding(
                binding_id, revoked_by=revoked_by, reason=reason
            )

    def transfer_git_repository_binding(
        self,
        binding_id: str,
        *,
        target_user_id: str,
        actor_user_id: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().transfer_git_repository_binding(
                binding_id,
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                reason=reason,
            )

    def upsert_options(self, values: dict[str, str]) -> dict[str, str]:
        with self._persist():
            return super().upsert_options(values)

    def submit_plugin(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        with self._persist():
            return super().submit_plugin(user, payload)

    def register_plugin(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        with self._persist():
            return super().register_plugin(user, payload)

    def update_plugin_status(
        self,
        plugin_id: str,
        status: str,
        by_user_id: str | None,
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().update_plugin_status(plugin_id, status, by_user_id)

    def unlist_plugin(
        self,
        plugin_id: str,
        by_user_id: str,
        reason: str,
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().unlist_plugin(plugin_id, by_user_id, reason)

    def request_plugin_listing(
        self,
        plugin_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().request_plugin_listing(plugin_id, user_id)

    def update_plugin_metadata(
        self,
        plugin_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().update_plugin_metadata(plugin_id, patch)

    def add_comment(
        self,
        plugin_id: str,
        user_id: str,
        body: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        with self._persist():
            return super().add_comment(plugin_id, user_id, body, parent_id)

    def like_plugin(self, plugin_id: str, user_id: str) -> dict[str, Any] | None:
        with self._persist():
            return super().like_plugin(plugin_id, user_id)

    def unlike_plugin(self, plugin_id: str, user_id: str) -> dict[str, Any] | None:
        with self._persist():
            return super().unlike_plugin(plugin_id, user_id)

    def like_comment(self, comment_id: str, user_id: str) -> dict[str, Any] | None:
        with self._persist():
            return super().like_comment(comment_id, user_id)

    def unlike_comment(self, comment_id: str, user_id: str) -> dict[str, Any] | None:
        with self._persist():
            return super().unlike_comment(comment_id, user_id)

    def delete_comment(self, comment_id: str, by_user_id: str) -> dict[str, Any] | None:
        with self._persist():
            return super().delete_comment(comment_id, by_user_id)

    def mute_user(
        self,
        user_id: str,
        muted_until: str,
        by_user_id: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().mute_user(user_id, muted_until, by_user_id, reason)

    def unmute_user(self, user_id: str) -> dict[str, Any] | None:
        with self._persist():
            return super().unmute_user(user_id)

    def update_user_role(self, user_id: str, role: str) -> dict[str, Any] | None:
        with self._persist():
            return super().update_user_role(user_id, role)

    def delete_user(self, user_id: str, replacement_user_id: str) -> dict[str, Any] | None:
        with self._persist():
            return super().delete_user(user_id, replacement_user_id)

    def update_user_profile(
        self,
        user_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().update_user_profile(user_id, profile)

    def set_user_uploaded_avatar(self, user_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._persist():
            return super().set_user_uploaded_avatar(user_id, **fields)

    def clear_user_uploaded_avatar(
        self, user_id: str, *, expected_object_id: str
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().clear_user_uploaded_avatar(
                user_id, expected_object_id=expected_object_id
            )

    def merge_user_into_user(self, from_user_id: str, to_user_id: str) -> dict[str, Any] | None:
        with self._persist():
            return super().merge_user_into_user(from_user_id, to_user_id)

    def publish_announcement(
        self,
        title: str,
        body: str,
        author_user_id: str,
    ) -> dict[str, Any]:
        with self._persist():
            return super().publish_announcement(title, body, author_user_id)

    def update_announcement(
        self,
        announcement_id: str | int,
        *,
        title: str | None = None,
        body: str | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        with self._persist():
            return super().update_announcement(
                announcement_id,
                title=title,
                body=body,
                expected_updated_at=expected_updated_at,
            )

    def delete_announcement(self, announcement_id: str | int) -> bool:
        with self._persist():
            return super().delete_announcement(announcement_id)

    def create_notification(
        self,
        user_id: str,
        title: str,
        body: str,
        notification_type: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._persist():
            return super().create_notification(user_id, title, body, notification_type, metadata)

    def create_notification_once(
        self,
        user_id: str,
        title: str,
        body: str,
        notification_type: str,
        metadata: dict[str, Any],
        dedupe_key: str,
    ) -> dict[str, Any]:
        with self._persist():
            return super().create_notification_once(
                user_id,
                title,
                body,
                notification_type,
                metadata,
                dedupe_key,
            )

    def mark_notifications_read(self, user_id: str) -> int:
        with self._persist():
            return super().mark_notifications_read(user_id)

    def delete_notifications(
        self,
        user_id: str,
        notification_ids: list[str] | None = None,
    ) -> int:
        with self._persist():
            return super().delete_notifications(user_id, notification_ids)

    def issue_api_key(
        self,
        name: str,
        user_id: str,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._persist():
            return super().issue_api_key(name, user_id, scopes)

    def delete_api_key(self, user_id: str, key_id: str) -> int:
        with self._persist():
            return super().delete_api_key(user_id, key_id)

    def create_session(self, user_id: str, token: str | None = None) -> dict[str, Any]:
        with self._persist():
            session = super().create_session(user_id, token)
            self._session_persisted_at[session["token"]] = time.monotonic()
            return session

    def get_user_by_session(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._find("sessions", "token", token)
            if not session:
                return None
            seen = _parse_utc_timestamp(session.get("last_seen_at") or session.get("created_at"))
            cutoff = datetime.now(UTC) - timedelta(seconds=self.session_ttl_seconds)
            if seen is None or seen < cutoff:
                self.revoke_session(token)
                return None
            user = self.get_user_by_id(session["user_id"])
            if not user:
                return None
            expected_version = int(user.get("session_version") or 1)
            if int(session.get("session_version") or 1) != expected_version:
                self.revoke_session(token)
                return None
            session["last_seen_at"] = utc_now()
            now_mono = time.monotonic()
            if now_mono - self._session_persisted_at.get(token, 0.0) >= 60.0:
                self._session_persisted_at[token] = now_mono
                self._save_locked()
            return deepcopy(user)

    def revoke_session(self, token: str) -> bool:
        with self._persist():
            self._session_persisted_at.pop(token, None)
            return super().revoke_session(token)

    def revoke_all_user_sessions(self, user_id: str) -> int:
        with self._persist():
            for session in list(self.state["sessions"]):
                if session.get("user_id") == user_id:
                    self._session_persisted_at.pop(str(session.get("token") or ""), None)
            return super().revoke_all_user_sessions(user_id)

    def save_email_challenge(
        self, challenge_id: str, record: dict[str, Any], ttl_seconds: int
    ) -> None:
        with self._persist():
            super().save_email_challenge(challenge_id, record, ttl_seconds)

    def get_email_challenge_salt(self, challenge_id: str) -> str | None:
        with self._lock:
            return super().get_email_challenge_salt(challenge_id)

    def reserve_email_challenge(
        self,
        challenge_id: str,
        purpose: str,
        expected_digest: str,
        max_attempts: int,
    ) -> tuple[str, dict[str, Any] | None]:
        with self._persist():
            return super().reserve_email_challenge(
                challenge_id, purpose, expected_digest, max_attempts
            )

    def release_email_challenge(self, challenge_id: str) -> None:
        with self._persist():
            super().release_email_challenge(challenge_id)

    def delete_email_challenge(self, challenge_id: str) -> None:
        with self._persist():
            super().delete_email_challenge(challenge_id)

    def get_rate_limit_count(self, key: str) -> int:
        with self._lock:
            return super().get_rate_limit_count(key)

    def increment_rate_limit(self, key: str, ttl_seconds: int) -> int:
        with self._persist():
            return super().increment_rate_limit(key, ttl_seconds)

    def reserve_rate_limit(self, key: str, limit: int, ttl_seconds: int) -> bool:
        with self._persist():
            return super().reserve_rate_limit(key, limit, ttl_seconds)

    def decrement_rate_limit(self, key: str) -> None:
        with self._persist():
            super().decrement_rate_limit(key)

    def check_and_set_cooldown(self, key: str, ttl_seconds: int) -> int:
        with self._persist():
            return super().check_and_set_cooldown(key, ttl_seconds)

    def clear_cooldown(self, key: str) -> None:
        with self._persist():
            super().clear_cooldown(key)


PLUGIN_COLUMN_KEYS = {
    "name",
    "display_name",
    "desc",
    "author",
    "repo",
    "repo_provider",
    "repo_external_id",
    "normalized_repo",
    "tags",
    "social_link",
    "owner_user_id",
    "owner_github_login",
    "owner_username",
    "status",
    "stars",
    "likes",
    "comments_count",
    "download_count",
    "moderated_by",
    "repo_version",
    "current_artifact_id",
    "category",
    "category_source",
    "suggested_category",
    "category_confidence",
    "category_reason",
}


def plugin_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in PLUGIN_COLUMN_KEYS
        and key not in {"id", "created_at", "updated_at", "version_updated_at"}
        and value is not None
    }


def submission_payload_from_plugin(plugin: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: deepcopy(plugin[key])
        for key in (
            "name",
            "display_name",
            "desc",
            "author",
            "repo",
            "tags",
            "social_link",
            "category",
        )
        if key in plugin and plugin[key] is not None
    }
    return payload


SCHEMA_COMPATIBILITY_SQL = """
ALTER TABLE IF EXISTS market_notifications
    ADD COLUMN IF NOT EXISTS dedupe_key text;
"""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_users (
    id text PRIMARY KEY,
    github_id text UNIQUE,
    github_login text NOT NULL DEFAULT '',
    internal_username text NOT NULL DEFAULT '',
    password_hash text NOT NULL DEFAULT '',
    auth_source text NOT NULL DEFAULT 'github' CHECK (auth_source IN ('github', 'internal')),
    github_name text NOT NULL DEFAULT '',
    github_email text NOT NULL DEFAULT '',
    avatar_url text NOT NULL DEFAULT '',
    github_token text NOT NULL DEFAULT '',
    github_refresh_interval_seconds integer NOT NULL DEFAULT 3600,
    notification_email text NOT NULL DEFAULT '',
    notify_plugin_review boolean NOT NULL DEFAULT true,
    notify_comments boolean NOT NULL DEFAULT true,
    notify_replies boolean NOT NULL DEFAULT true,
    notify_likes boolean NOT NULL DEFAULT true,
    notify_unlist boolean NOT NULL DEFAULT true,
    email_notify_plugin_review boolean NOT NULL DEFAULT true,
    email_notify_pending_review boolean NOT NULL DEFAULT true,
    email_notify_comments boolean NOT NULL DEFAULT false,
    email_notify_replies boolean NOT NULL DEFAULT false,
    email_notify_likes boolean NOT NULL DEFAULT false,
    email_notify_unlist boolean NOT NULL DEFAULT true,
    uploaded_avatar_url text NOT NULL DEFAULT '',
    uploaded_avatar_profile_id text NOT NULL DEFAULT '',
    uploaded_avatar_object_id text NOT NULL DEFAULT '',
    uploaded_avatar_sha256 text NOT NULL DEFAULT '',
    uploaded_avatar_updated_at timestamptz,
    role text NOT NULL CHECK (role IN ('core_admin', 'admin', 'user')),
    muted_until timestamptz,
    muted_by text REFERENCES market_users(id) ON DELETE SET NULL,
    muted_reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS market_users_github_login_lower_idx
    ON market_users (lower(github_login)) WHERE github_login <> '';
CREATE UNIQUE INDEX IF NOT EXISTS market_users_internal_username_lower_idx
    ON market_users (lower(internal_username)) WHERE internal_username <> '';

CREATE TABLE IF NOT EXISTS market_plugins (
    id text PRIMARY KEY,
    name text NOT NULL,
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
    updated_at timestamptz NOT NULL DEFAULT now(),
    version_updated_at timestamptz NOT NULL DEFAULT now(),
    listed_at timestamptz
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
    likes integer NOT NULL DEFAULT 0 CHECK (likes >= 0),
    muted boolean NOT NULL DEFAULT false,
    deleted boolean NOT NULL DEFAULT false,
    deleted_by text REFERENCES market_users(id) ON DELETE SET NULL,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_comments_plugin_idx
    ON market_comments(plugin_id, created_at ASC) WHERE deleted = false;

CREATE TABLE IF NOT EXISTS market_plugin_likes (
    plugin_id text NOT NULL REFERENCES market_plugins(id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plugin_id, user_id)
);
CREATE INDEX IF NOT EXISTS market_plugin_likes_user_idx
    ON market_plugin_likes(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS market_comment_likes (
    comment_id text NOT NULL REFERENCES market_comments(id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (comment_id, user_id)
);
CREATE INDEX IF NOT EXISTS market_comment_likes_user_idx
    ON market_comment_likes(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS market_announcements (
    id text PRIMARY KEY,
    title text NOT NULL,
    body text NOT NULL,
    author_user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_announcements_created_idx
    ON market_announcements(created_at DESC);

CREATE TABLE IF NOT EXISTS market_notifications (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    type text NOT NULL DEFAULT 'system',
    title text NOT NULL,
    body text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key text,
    read boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_notifications_user_idx
    ON market_notifications(user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS market_notifications_dedupe_idx
    ON market_notifications(dedupe_key)
    WHERE dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS market_api_keys (
    id text PRIMARY KEY,
    name text NOT NULL,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    scopes jsonb NOT NULL DEFAULT '["market:read"]'::jsonb,
    key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market_options (
    option_key text PRIMARY KEY,
    option_value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
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
            # Existing installations need the column before SCHEMA_SQL recreates its index.
            await connection.execute(SCHEMA_COMPATIBILITY_SQL)
            await connection.execute(SCHEMA_SQL)
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS internal_username text "
                "NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS password_hash text "
                "NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS auth_source text "
                "NOT NULL DEFAULT 'github'"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS github_token text "
                "NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "github_refresh_interval_seconds integer NOT NULL DEFAULT 3600"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS github_email text "
                "NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS notification_email text "
                "NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "notify_plugin_review boolean NOT NULL DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "notify_comments boolean NOT NULL DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "notify_replies boolean NOT NULL DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "notify_likes boolean NOT NULL DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "notify_unlist boolean NOT NULL DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "email_notify_plugin_review boolean NOT NULL DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ALTER COLUMN email_notify_plugin_review SET DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "email_notify_pending_review boolean NOT NULL DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ALTER COLUMN email_notify_pending_review SET DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "email_notify_comments boolean NOT NULL DEFAULT false"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "email_notify_replies boolean NOT NULL DEFAULT false"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "email_notify_likes boolean NOT NULL DEFAULT false"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "email_notify_unlist boolean NOT NULL DEFAULT true"
            )
            await connection.execute(
                "ALTER TABLE market_users ALTER COLUMN email_notify_unlist SET DEFAULT true"
            )
            await self._apply_email_notification_defaults_migration(connection)
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS muted_reason text "
                "NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_comments ADD COLUMN IF NOT EXISTS likes integer "
                "NOT NULL DEFAULT 0"
            )
            await connection.execute(
                "ALTER TABLE market_notifications ADD COLUMN IF NOT EXISTS read boolean "
                "NOT NULL DEFAULT false"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS uploaded_avatar_url text "
                "NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "uploaded_avatar_profile_id text NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "uploaded_avatar_object_id text NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS uploaded_avatar_sha256 text "
                "NOT NULL DEFAULT ''"
            )
            await connection.execute(
                "ALTER TABLE market_users ADD COLUMN IF NOT EXISTS "
                "uploaded_avatar_updated_at timestamptz"
            )
            await apply_schema_migrations(connection)

    async def _apply_email_notification_defaults_migration(
        self,
        connection: asyncpg.Connection,
    ) -> None:
        migration_key = "migration_email_notification_defaults_20260704"
        applied = await connection.fetchval(
            "SELECT option_value FROM market_options WHERE option_key = $1",
            migration_key,
        )
        if applied == "done":
            return
        await connection.execute(
            """
            UPDATE market_users
               SET email_notify_plugin_review = true,
                   email_notify_pending_review = true,
                   email_notify_unlist = true
            """
        )
        await connection.execute(
            """
            INSERT INTO market_options (option_key, option_value)
            VALUES ($1, 'done')
            ON CONFLICT (option_key) DO UPDATE
               SET option_value = EXCLUDED.option_value,
                   updated_at = now()
            """,
            migration_key,
        )

    async def _allocate_pg_public_username(
        self,
        connection: asyncpg.Connection,
        preferred: str,
        *,
        exclude_user_id: str = "",
        suffix_tag: str = "gh",
    ) -> str:
        base = str(preferred or "").strip() or f"user_{secrets.token_hex(4)}"
        for suffix in ("", f"_{suffix_tag}", *(f"_{i}" for i in range(1, 100))):
            candidate = f"{base}{suffix}"
            exists = await connection.fetchval(
                """
                SELECT 1
                  FROM market_users
                 WHERE (
                       lower(btrim(public_username)) = lower($1)
                    OR lower(btrim(internal_username)) = lower($1)
                    OR lower(btrim(github_login)) = lower($1)
                 )
                   AND id <> $2
                """,
                candidate,
                exclude_user_id,
            )
            if not exists:
                return candidate
        return f"{base}_{secrets.token_hex(4)}"

    async def create_oauth_user(
        self,
        *,
        provider: str,
        provider_login: str,
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        norm_provider = str(provider or "oauth").strip().lower()
        clean_login = str(provider_login or "").strip() or f"{norm_provider}_{secrets.token_hex(3)}"
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute("LOCK TABLE market_users IN EXCLUSIVE MODE")
                public_username = await self._allocate_pg_public_username(
                    connection, clean_login, suffix_tag=norm_provider
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_users (
                        id, github_id, github_login, internal_username, public_username,
                        email, email_verified_at, session_version, password_hash,
                        auth_source, github_name, github_email, avatar_url, role
                    )
                    VALUES ($1, NULL, '', '', $2, '', NULL, 1, '', 'oauth', $3, '', $4, $5)
                    RETURNING *
                    """,
                    new_id("user"),
                    public_username,
                    str(display_name or clean_login).strip(),
                    str(avatar_url or "").strip(),
                    Role.USER.value,
                )
                return self._user_from_record(row)

    async def _upsert_pg_oauth_identity(
        self,
        connection: asyncpg.Connection,
        user_id: str,
        *,
        provider: str,
        instance_url: str,
        provider_user_id: str,
        provider_login: str,
        provider_email: str = "",
        avatar_url: str = "",
        scopes: str = "",
        credential_ref: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        norm_provider = str(provider or "").strip().lower()
        norm_instance = str(instance_url or "").strip()
        uid = str(provider_user_id or "").strip()
        existing = await connection.fetchrow(
            """
            SELECT *
              FROM market_oauth_identities
             WHERE lower(provider) = lower($1)
               AND lower(instance_url) = lower($2)
               AND provider_user_id = $3
               AND revoked_at IS NULL
            """,
            norm_provider,
            norm_instance,
            uid,
        )
        if existing:
            if existing["user_id"] != user_id:
                raise GithubIdentityConflict("该 OAuth 账号已绑定到其他市场账号")
            row = await connection.fetchrow(
                """
                UPDATE market_oauth_identities
                   SET provider_login = $2,
                       provider_email = COALESCE(NULLIF($3, ''), provider_email),
                       avatar_url = COALESCE(NULLIF($4, ''), avatar_url),
                       scopes = COALESCE(NULLIF($5, ''), scopes),
                       credential_ref = COALESCE(NULLIF($6, ''), credential_ref),
                       expires_at = COALESCE($7::timestamptz, expires_at),
                       updated_at = now()
                 WHERE id = $1
             RETURNING *
                """,
                existing["id"],
                str(provider_login or ""),
                str(provider_email or ""),
                str(avatar_url or ""),
                str(scopes or ""),
                str(credential_ref or ""),
                _oauth_timestamp(expires_at),
            )
            return serialize_value(dict(row))
        row = await connection.fetchrow(
            """
            INSERT INTO market_oauth_identities (
                id, user_id, provider, instance_url, provider_user_id,
                provider_login, provider_email, avatar_url, scopes,
                credential_ref, expires_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::timestamptz)
            RETURNING *
            """,
            new_id("oauth"),
            user_id,
            norm_provider,
            norm_instance,
            uid,
            str(provider_login or ""),
            str(provider_email or ""),
            str(avatar_url or ""),
            str(scopes or ""),
            str(credential_ref or ""),
            _oauth_timestamp(expires_at),
        )
        return serialize_value(dict(row))

    async def upsert_github_user(self, profile: dict[str, Any]) -> dict[str, Any]:
        login = profile.get("login") or profile.get("github_login")
        if not login:
            raise ValueError("GitHub profile login is required")

        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute("LOCK TABLE market_users IN EXCLUSIVE MODE")
                login_owner = await connection.fetchrow(
                    "SELECT * FROM market_users WHERE lower(github_login) = lower($1)",
                    str(login),
                )
                github_id = str(profile.get("id") or "")
                if (
                    github_id
                    and login_owner
                    and str(login_owner["github_id"] or login_owner["id"]) != github_id
                ):
                    raise GithubIdentityConflict("GitHub 用户名已关联其他身份，请联系管理员核对")
                identity_owner = None
                if github_id:
                    identity_row = await connection.fetchrow(
                        """
                        SELECT user_id
                          FROM market_oauth_identities
                         WHERE lower(provider) = 'github'
                           AND lower(instance_url) = 'https://github.com'
                           AND provider_user_id = $1
                           AND revoked_at IS NULL
                        """,
                        github_id,
                    )
                    if identity_row:
                        identity_owner = await connection.fetchrow(
                            "SELECT * FROM market_users WHERE id = $1",
                            identity_row["user_id"],
                        )
                existing = (
                    identity_owner
                    or (
                        await connection.fetchrow(
                            "SELECT * FROM market_users WHERE github_id = $1 OR (github_id IS NULL AND auth_source = 'github' AND id = $1)",
                            github_id,
                        )
                        if github_id
                        else None
                    )
                    or login_owner
                )
                github_email = str(profile.get("github_email") or profile.get("email") or "")
                avatar_url = str(profile.get("avatar_url") or "")
                if existing:
                    is_github_origin = existing["auth_source"] == "github"
                    public_username = existing["public_username"] or (
                        await self._allocate_pg_public_username(
                            connection, str(login), exclude_user_id=existing["id"]
                        )
                    )
                    next_name = (
                        profile.get("name") or login
                        if is_github_origin or not existing["github_name"]
                        else existing["github_name"]
                    )
                    next_avatar = (
                        avatar_url or existing["avatar_url"]
                        if is_github_origin or not existing["avatar_url"]
                        else existing["avatar_url"]
                    )
                    row = await connection.fetchrow(
                        """
                        UPDATE market_users
                           SET github_id = COALESCE($2, github_id),
                               github_login = $3,
                               public_username = $4,
                               github_name = $5,
                               avatar_url = $6,
                               github_email = COALESCE(NULLIF($7, ''), github_email),
                               updated_at = now()
                         WHERE id = $1
                     RETURNING *
                        """,
                        existing["id"],
                        str(profile.get("id")) if profile.get("id") else None,
                        str(login),
                        public_username,
                        next_name,
                        next_avatar,
                        github_email,
                    )
                    await self._upsert_pg_oauth_identity(
                        connection,
                        existing["id"],
                        provider="github",
                        instance_url="https://github.com",
                        provider_user_id=str(row["github_id"] or row["id"]),
                        provider_login=str(login),
                        provider_email=github_email,
                        avatar_url=avatar_url,
                    )
                    return self._user_from_record(row)

                user_id = str(profile.get("id") or new_id("user"))
                public_username = await self._allocate_pg_public_username(connection, str(login))
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_users (
                        id, github_id, github_login, internal_username, public_username,
                        email, email_verified_at, session_version, password_hash,
                        auth_source, github_name, github_email, avatar_url, role
                    )
                    VALUES ($1, $2, $3, '', $4, '', NULL, 1, '', 'github', $5, $6, $7, $8)
                    RETURNING *
                    """,
                    user_id,
                    str(profile.get("id")) if profile.get("id") else None,
                    str(login),
                    public_username,
                    profile.get("name") or login,
                    github_email,
                    avatar_url,
                    Role.USER.value,
                )
                await self._upsert_pg_oauth_identity(
                    connection,
                    row["id"],
                    provider="github",
                    instance_url="https://github.com",
                    provider_user_id=str(row["github_id"] or row["id"]),
                    provider_login=str(login),
                    provider_email=github_email,
                    avatar_url=avatar_url,
                )
                return self._user_from_record(row)

    async def create_internal_admin(self, username: str, password_hash: str) -> dict[str, Any]:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute("LOCK TABLE market_users IN EXCLUSIVE MODE")
                existing = await connection.fetchrow(
                    "SELECT * FROM market_users WHERE lower(internal_username) = lower($1)",
                    username,
                )
                if existing:
                    public_username = existing["public_username"] or (
                        await self._allocate_pg_public_username(
                            connection, username, exclude_user_id=existing["id"]
                        )
                    )
                    row = await connection.fetchrow(
                        """
                        UPDATE market_users
                           SET password_hash = $2,
                               role = $3,
                               public_username = $4,
                               updated_at = now()
                         WHERE id = $1
                     RETURNING *
                        """,
                        existing["id"],
                        password_hash,
                        Role.CORE_ADMIN.value,
                        public_username,
                    )
                    return self._user_from_record(row)

                public_username = await self._allocate_pg_public_username(connection, username)
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_users (
                        id, github_id, github_login, internal_username, public_username,
                        email, email_verified_at, session_version, password_hash,
                        auth_source, github_name, avatar_url, role
                    )
                    VALUES ($1, NULL, '', $2, $3, '', NULL, 1, $4, 'internal', $2, '', $5)
                    RETURNING *
                    """,
                    new_id("user"),
                    username,
                    public_username,
                    password_hash,
                    Role.CORE_ADMIN.value,
                )
                return self._user_from_record(row)

    async def create_internal_user(
        self,
        username: str,
        password_hash: str,
        role: str = Role.USER.value,
        email: str = "",
    ) -> dict[str, Any]:
        clean_username = str(username or "").strip()
        clean_email = str(email or "").strip()
        try:
            row = await self._pool().fetchrow(
                """
                INSERT INTO market_users (
                    id, github_id, github_login, internal_username, public_username,
                    email, email_verified_at, session_version, password_hash,
                    auth_source, github_name, avatar_url, role
                )
                VALUES ($1, NULL, '', $2, $2, $3, NULL, 1, $4, 'internal', $2, '', $5)
                RETURNING *
                """,
                new_id("user"),
                clean_username,
                clean_email,
                password_hash,
                normalize_role(role).value,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "market_users_email_lower_idx":
                raise UserIdentityConflict("邮箱已被注册", "email_already_exists") from exc
            if exc.constraint_name in {
                "market_users_internal_username_lower_idx",
                "market_users_public_username_lower_idx",
            }:
                raise UserIdentityConflict("用户名已被占用", "username_already_exists") from exc
            raise
        return self._user_from_record(row)

    async def create_email_user(
        self,
        *,
        username: str,
        email: str,
        password_hash: str,
        email_verified_at: str | None = None,
        role: str = Role.USER.value,
    ) -> dict[str, Any]:
        clean_username = str(username or "").strip()
        clean_email = str(email or "").strip()
        try:
            row = await self._pool().fetchrow(
                """
                INSERT INTO market_users (
                    id, github_id, github_login, internal_username, public_username,
                    email, email_verified_at, session_version, password_hash,
                    auth_source, github_name, avatar_url, role
                )
                VALUES ($1, NULL, '', $2, $2, $3, $4::timestamptz, 1, $5, 'email', $2, '', $6)
                RETURNING *
                """,
                new_id("user"),
                clean_username,
                clean_email,
                _pg_timestamp(email_verified_at),
                password_hash,
                normalize_role(role).value,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "market_users_email_lower_idx":
                raise UserIdentityConflict("邮箱已被注册", "email_already_exists") from exc
            if exc.constraint_name in {
                "market_users_internal_username_lower_idx",
                "market_users_public_username_lower_idx",
            }:
                raise UserIdentityConflict("用户名已被占用", "username_already_exists") from exc
            raise
        return self._user_from_record(row)

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow("SELECT * FROM market_users WHERE id = $1", user_id)
        return self._user_from_record(row) if row else None

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        clean = str(email or "").strip()
        if not clean:
            return None
        row = await self._pool().fetchrow(
            "SELECT * FROM market_users WHERE lower(btrim(email)) = lower($1)",
            clean,
        )
        return self._user_from_record(row) if row else None

    async def get_user_by_public_username(self, username: str) -> dict[str, Any] | None:
        clean = str(username or "").strip()
        if not clean:
            return None
        row = await self._pool().fetchrow(
            "SELECT * FROM market_users WHERE lower(btrim(public_username)) = lower($1)",
            clean,
        )
        return self._user_from_record(row) if row else None

    async def get_user_by_login_identifier(self, identifier: str) -> dict[str, Any] | None:
        clean = str(identifier or "").strip()
        if not clean:
            return None
        if "@" in clean:
            return await self.get_user_by_email(clean)
        rows = await self._pool().fetch(
            """
            SELECT *
              FROM market_users
             WHERE lower(btrim(internal_username)) = lower($1)
                OR lower(btrim(public_username)) = lower($1)
             LIMIT 2
            """,
            clean,
        )
        if len(rows) > 1:
            raise UserIdentityConflict(
                "登录标识符存在歧义，请联系管理员处理", "login_identifier_ambiguous"
            )
        return self._user_from_record(rows[0]) if rows else None

    async def get_user_by_github_id(self, github_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT u.*
              FROM market_users u
              JOIN market_oauth_identities oi
                ON oi.user_id = u.id
               AND lower(oi.provider) = 'github'
               AND lower(oi.instance_url) = 'https://github.com'
               AND oi.provider_user_id = $1
               AND oi.revoked_at IS NULL
             LIMIT 1
            """,
            github_id,
        )
        if row:
            return self._user_from_record(row)
        row = await self._pool().fetchrow(
            "SELECT * FROM market_users WHERE github_id = $1 OR (github_id IS NULL AND auth_source = 'github' AND id = $1)",
            github_id,
        )
        return self._user_from_record(row) if row else None

    async def get_user_by_github_login(self, login: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_users WHERE lower(github_login) = lower($1)",
            login,
        )
        return self._user_from_record(row) if row else None

    async def get_user_by_internal_username(self, username: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_users WHERE lower(internal_username) = lower($1)",
            username,
        )
        return self._user_from_record(row) if row else None

    async def set_user_password(
        self,
        user_id: str,
        password_hash: str,
        *,
        bump_session_version: bool = True,
        verify_email_at: str | None = None,
        email: str | None = None,
        expected_email: str | None = None,
    ) -> dict[str, Any] | None:
        clean_email = str(email).strip() if email is not None else None
        expected = str(expected_email or "").strip()
        try:
            row = await self._pool().fetchrow(
                """
                UPDATE market_users
                   SET password_hash = $2,
                       email = COALESCE(NULLIF($3, ''), email),
                       email_verified_at = COALESCE($4::timestamptz, email_verified_at),
                       session_version = CASE
                           WHEN $5 THEN session_version + 1
                           ELSE session_version
                       END,
                       updated_at = now()
                 WHERE id = $1
                   AND ($6 = '' OR lower(btrim(email)) = lower($6))
             RETURNING *
                """,
                user_id,
                password_hash,
                clean_email or "",
                _pg_timestamp(verify_email_at),
                bump_session_version,
                expected,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "market_users_email_lower_idx":
                raise UserIdentityConflict(
                    "该邮箱已被其他账号使用", "email_already_exists"
                ) from exc
            raise
        if row and bump_session_version:
            await self._redis().delete(f"astrbot_market:github_oauth_user:{user_id}")
        return self._user_from_record(row) if row else None

    async def update_user_email(
        self,
        user_id: str,
        new_email: str,
        *,
        email_verified_at: str | None,
    ) -> dict[str, Any] | None:
        clean_email = str(new_email or "").strip()
        try:
            row = await self._pool().fetchrow(
                """
                UPDATE market_users
                   SET email = $2,
                       email_verified_at = $3::timestamptz,
                       updated_at = now()
                 WHERE id = $1
             RETURNING *
                """,
                user_id,
                clean_email,
                _pg_timestamp(email_verified_at),
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "market_users_email_lower_idx":
                raise UserIdentityConflict(
                    "该邮箱已被其他账号使用", "email_already_exists"
                ) from exc
            raise
        return self._user_from_record(row) if row else None

    async def list_oauth_identities(
        self,
        user_id: str,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        if include_revoked:
            rows = await self._pool().fetch(
                "SELECT * FROM market_oauth_identities WHERE user_id = $1 ORDER BY created_at ASC",
                user_id,
            )
        else:
            rows = await self._pool().fetch(
                """
                SELECT *
                  FROM market_oauth_identities
                 WHERE user_id = $1
                   AND revoked_at IS NULL
              ORDER BY created_at ASC
                """,
                user_id,
            )
        return [serialize_value(dict(row)) for row in rows]

    async def get_oauth_identity_by_provider_user(
        self,
        provider: str,
        instance_url: str,
        provider_user_id: str,
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT *
              FROM market_oauth_identities
             WHERE lower(provider) = lower($1)
               AND lower(instance_url) = lower($2)
               AND provider_user_id = $3
               AND revoked_at IS NULL
            """,
            provider,
            instance_url,
            provider_user_id,
        )
        return serialize_value(dict(row)) if row else None

    async def upsert_oauth_identity(
        self,
        user_id: str,
        *,
        provider: str,
        instance_url: str,
        provider_user_id: str,
        provider_login: str,
        provider_email: str = "",
        avatar_url: str = "",
        scopes: str = "",
        credential_ref: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                return await self._upsert_pg_oauth_identity(
                    connection,
                    user_id,
                    provider=provider,
                    instance_url=instance_url,
                    provider_user_id=provider_user_id,
                    provider_login=provider_login,
                    provider_email=provider_email,
                    avatar_url=avatar_url,
                    scopes=scopes,
                    credential_ref=credential_ref,
                    expires_at=expires_at,
                )

    async def revoke_oauth_identity(self, user_id: str, identity_id: str) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE market_oauth_identities
                       SET revoked_at = now(),
                           updated_at = now()
                     WHERE id = $1
                       AND user_id = $2
                       AND revoked_at IS NULL
                 RETURNING *
                    """,
                    identity_id,
                    user_id,
                )
                if not row:
                    return None
                cred_ref = str(row["credential_ref"] or "").strip()
                if cred_ref:
                    await connection.execute(
                        "DELETE FROM market_oauth_credentials WHERE credential_ref = $1",
                        cred_ref,
                    )
                if row["provider"] == "github":
                    remaining = await connection.fetchrow(
                        """
                        SELECT provider_user_id, provider_login, provider_email
                          FROM market_oauth_identities
                         WHERE user_id = $1
                           AND provider = 'github'
                           AND revoked_at IS NULL
                         ORDER BY updated_at DESC, created_at DESC, id DESC
                         LIMIT 1
                        """,
                        user_id,
                    )
                    await connection.execute(
                        """
                        UPDATE market_users
                           SET github_id = $2,
                               github_login = $3,
                               github_email = $4,
                               updated_at = now()
                         WHERE id = $1
                        """,
                        user_id,
                        str(remaining["provider_user_id"]) if remaining else None,
                        str(remaining["provider_login"] or "") if remaining else "",
                        str(remaining["provider_email"] or "") if remaining else "",
                    )
        return serialize_value(dict(row))

    async def update_oauth_identity_rotation(
        self,
        identity_id: str,
        *,
        credential_ref: str = "",
        expires_at: str | None = None,
        rotation_status: str = "",
        token_refreshed_at: str | None = None,
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE market_oauth_identities
               SET credential_ref = COALESCE(NULLIF($2, ''), credential_ref),
                   expires_at = COALESCE($3::timestamptz, expires_at),
                   rotation_status = COALESCE(NULLIF($4, ''), rotation_status),
                   token_refreshed_at = COALESCE($5::timestamptz, token_refreshed_at),
                   updated_at = now()
             WHERE id = $1
               AND revoked_at IS NULL
         RETURNING *
            """,
            identity_id,
            str(credential_ref or ""),
            _oauth_timestamp(expires_at),
            str(rotation_status or ""),
            _oauth_timestamp(token_refreshed_at),
        )
        return serialize_value(dict(row)) if row else None

    async def upsert_oauth_credential(
        self,
        credential_ref: str,
        *,
        user_id: str,
        provider: str,
        ciphertext: str,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        row = await self._pool().fetchrow(
            """
            INSERT INTO market_oauth_credentials (
                credential_ref, user_id, provider, ciphertext, expires_at
            )
            VALUES ($1, $2, $3, $4, $5::timestamptz)
            ON CONFLICT (credential_ref) DO UPDATE
               SET user_id = EXCLUDED.user_id,
                   provider = EXCLUDED.provider,
                   ciphertext = EXCLUDED.ciphertext,
                   expires_at = EXCLUDED.expires_at,
                   updated_at = now()
            RETURNING *
            """,
            str(credential_ref),
            str(user_id),
            str(provider or "").strip().lower(),
            str(ciphertext),
            _oauth_timestamp(expires_at),
        )
        return serialize_value(dict(row))

    async def get_oauth_credential(self, credential_ref: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_oauth_credentials WHERE credential_ref = $1",
            str(credential_ref),
        )
        return serialize_value(dict(row)) if row else None

    async def list_oauth_credentials_by_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM market_oauth_credentials WHERE user_id = $1",
            str(user_id),
        )
        return [serialize_value(dict(row)) for row in rows]

    async def delete_oauth_credential(self, credential_ref: str) -> bool:
        result = await self._pool().execute(
            "DELETE FROM market_oauth_credentials WHERE credential_ref = $1",
            str(credential_ref),
        )
        return int(result.rsplit(" ", 1)[-1]) > 0

    @plugin_identity_write
    async def upsert_git_repository_binding(
        self,
        user_id: str,
        *,
        normalized_repo: str,
        repo_url: str,
        provider: str,
        instance_url: str = "",
        repo_external_id: str = "",
        owner_id: str = "",
        permission_level: str = "",
        verification_method: str = "oauth",
        credential_ref: str = "",
        verified_at: str | None = None,
    ) -> dict[str, Any]:
        norm_key = str(normalized_repo or "").strip().lower()
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"git_binding:{norm_key}",
                )
                active = await connection.fetchrow(
                    """
                    SELECT *
                      FROM market_git_repository_bindings
                     WHERE lower(normalized_repo) = lower($1)
                       AND revoked_at IS NULL
                       FOR UPDATE
                    """,
                    norm_key,
                )
                if active:
                    if str(active["user_id"]) != str(user_id):
                        raise PluginIdentityConflict("该仓库已绑定到其他市场账号，请联系管理员核对")
                    gen = int(active["binding_generation"] or 1)
                    identity_changed = (
                        bool(active["repo_external_id"])
                        and bool(repo_external_id)
                        and str(active["repo_external_id"]) != str(repo_external_id)
                    ) or (
                        bool(active["owner_id"])
                        and bool(owner_id)
                        and str(active["owner_id"]) != str(owner_id)
                    )
                    if identity_changed:
                        gen += 1
                    row = await connection.fetchrow(
                        """
                        UPDATE market_git_repository_bindings
                           SET repo_url = $2,
                               provider = $3,
                               instance_url = COALESCE(NULLIF($4, ''), instance_url),
                               repo_external_id = COALESCE(NULLIF($5, ''), repo_external_id),
                               owner_id = COALESCE(NULLIF($6, ''), owner_id),
                               permission_level = COALESCE(NULLIF($7, ''), permission_level),
                               verification_method = $8,
                               credential_ref = COALESCE(NULLIF($9, ''), credential_ref),
                               binding_generation = $10,
                               verified_at = COALESCE($11::timestamptz, now()),
                               updated_at = now()
                         WHERE id = $1
                     RETURNING *
                        """,
                        active["id"],
                        str(repo_url),
                        str(provider or "").strip().lower(),
                        str(instance_url or ""),
                        str(repo_external_id or ""),
                        str(owner_id or ""),
                        str(permission_level or ""),
                        str(verification_method or "oauth"),
                        str(credential_ref or ""),
                        gen,
                        _oauth_timestamp(verified_at),
                    )
                    return serialize_value(dict(row))

                prior_max = int(
                    await connection.fetchval(
                        """
                        SELECT COALESCE(MAX(binding_generation), 0)
                          FROM market_git_repository_bindings
                         WHERE lower(normalized_repo) = lower($1)
                        """,
                        norm_key,
                    )
                    or 0
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_git_repository_bindings (
                        id, user_id, normalized_repo, repo_url, provider, instance_url,
                        repo_external_id, owner_id, permission_level, verification_method,
                        credential_ref, binding_generation, verified_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, COALESCE($13::timestamptz, now()))
                    RETURNING *
                    """,
                    new_id("gitbind"),
                    str(user_id),
                    norm_key,
                    str(repo_url),
                    str(provider or "").strip().lower(),
                    str(instance_url or ""),
                    str(repo_external_id or ""),
                    str(owner_id or ""),
                    str(permission_level or ""),
                    str(verification_method or "oauth"),
                    str(credential_ref or ""),
                    prior_max + 1,
                    _oauth_timestamp(verified_at),
                )
                return serialize_value(dict(row))

    async def get_active_git_repository_binding(
        self, normalized_repo: str
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT *
              FROM market_git_repository_bindings
             WHERE lower(normalized_repo) = lower($1)
               AND revoked_at IS NULL
             LIMIT 1
            """,
            str(normalized_repo or "").strip().lower(),
        )
        return serialize_value(dict(row)) if row else None

    async def list_git_repository_bindings(
        self, include_revoked: bool = False
    ) -> list[dict[str, Any]]:
        if include_revoked:
            rows = await self._pool().fetch(
                "SELECT * FROM market_git_repository_bindings ORDER BY created_at DESC"
            )
        else:
            rows = await self._pool().fetch(
                """
                SELECT *
                  FROM market_git_repository_bindings
                 WHERE revoked_at IS NULL
              ORDER BY created_at DESC
                """
            )
        return [serialize_value(dict(row)) for row in rows]

    async def revoke_git_repository_binding(
        self,
        binding_id: str,
        *,
        revoked_by: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE market_git_repository_bindings
               SET revoked_at = now(),
                   revoked_by = $2,
                   revoke_reason = $3,
                   updated_at = now()
             WHERE id = $1
               AND revoked_at IS NULL
         RETURNING *
            """,
            binding_id,
            str(revoked_by),
            str(reason or ""),
        )
        return serialize_value(dict(row)) if row else None

    @plugin_identity_write
    async def transfer_git_repository_binding(
        self,
        binding_id: str,
        *,
        target_user_id: str,
        actor_user_id: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                old = await connection.fetchrow(
                    """
                    UPDATE market_git_repository_bindings
                       SET revoked_at = now(),
                           revoked_by = $2,
                           revoke_reason = $3,
                           updated_at = now()
                     WHERE id = $1
                       AND revoked_at IS NULL
                 RETURNING *
                    """,
                    binding_id,
                    str(actor_user_id),
                    str(reason or "管理员转移绑定"),
                )
                if not old:
                    return None
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_git_repository_bindings (
                        id, user_id, normalized_repo, repo_url, provider, instance_url,
                        repo_external_id, owner_id, permission_level, verification_method,
                        credential_ref, binding_generation, verified_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, '', $11, now())
                    RETURNING *
                    """,
                    new_id("gitbind"),
                    str(target_user_id),
                    old["normalized_repo"],
                    old["repo_url"],
                    old["provider"],
                    old["instance_url"],
                    old["repo_external_id"],
                    old["owner_id"],
                    old["permission_level"],
                    old["verification_method"],
                    int(old["binding_generation"] or 1) + 1,
                )
                return serialize_value(dict(row))

    async def list_users(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch("SELECT * FROM market_users ORDER BY created_at ASC")
        return [self._user_from_record(row) for row in rows]

    async def list_public_plugins(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM market_plugins WHERE status = 'listed' ORDER BY updated_at DESC"
        )
        return [self._plugin_from_record(row) for row in rows]

    async def list_due_github_sync_plugins(self, limit: int) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT *
              FROM market_plugins
             WHERE status = 'listed'
               AND COALESCE(
                       (metadata->>'github_next_sync_at')::timestamptz,
                       'epoch'::timestamptz
                   ) <= now()
          ORDER BY COALESCE(
                       (metadata->>'github_next_sync_at')::timestamptz,
                       'epoch'::timestamptz
                   ) ASC,
                   updated_at ASC
             LIMIT $1
            """,
            limit,
        )
        return [self._plugin_from_record(row) for row in rows]

    async def list_author_plugins(self, username: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT *
              FROM market_plugins
             WHERE lower(COALESCE(NULLIF(owner_username, ''), owner_github_login)) = lower($1)
               AND status = 'listed'
             ORDER BY updated_at DESC
            """,
            username,
        )
        return [self._plugin_from_record(row) for row in rows]

    async def has_active_plugins(self, user_id: str) -> bool:
        return bool(
            await self._pool().fetchval(
                """
                SELECT 1
                  FROM market_plugins
                 WHERE owner_user_id = $1
                   AND status IN ('listed', 'pending')
                 LIMIT 1
                """,
                user_id,
            )
        )

    async def list_plugins(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch("SELECT * FROM market_plugins ORDER BY updated_at DESC")
        return [self._plugin_from_record(row) for row in rows]

    async def list_user_plugins(self, user_id: str, github_login: str = "") -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT *
              FROM market_plugins
             WHERE owner_user_id = $1
          ORDER BY updated_at DESC
            """,
            user_id,
        )
        return [self._plugin_from_record(row) for row in rows]

    async def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow("SELECT * FROM market_plugins WHERE id = $1", plugin_id)
        return self._plugin_from_record(row) if row else None

    async def get_plugin_by_author(self, username: str, name: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT *
              FROM market_plugins
             WHERE lower(COALESCE(NULLIF(owner_username, ''), owner_github_login)) = lower($1)
               AND lower(name) = lower($2)
            """,
            username,
            name,
        )
        return self._plugin_from_record(row) if row else None

    async def get_owned_plugin_by_name(self, user_id: str, name: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM market_plugins WHERE owner_user_id = $1 AND lower(name) = lower($2)",
            user_id,
            name,
        )
        return self._plugin_from_record(row) if row else None

    async def _plugin_registration_id(
        self, connection: Any, user: dict[str, Any], payload: dict[str, Any]
    ) -> str:
        # Keep historical IDs, while serializing allocation for simultaneous same-name registrations.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", payload["name"].lower()
        )
        if payload.get("id"):
            existing = await connection.fetchrow(
                "SELECT * FROM market_plugins WHERE id = $1", payload["id"]
            )
        else:
            existing = await connection.fetchrow(
                "SELECT * FROM market_plugins WHERE owner_user_id = $1 AND lower(name) = lower($2)",
                user["id"],
                payload["name"],
            )
        if existing:
            if existing["owner_user_id"] != user["id"]:
                raise PermissionError("plugin_owner_mismatch")
            _, _, existing_repo_key = _canonical_repo_fields(
                str(existing.get("normalized_repo") or existing.get("repo") or "")
            )
            _, _, payload_repo_key = _canonical_repo_fields(
                str(payload.get("normalized_repo") or payload.get("repo") or "")
            )
            if existing_repo_key != payload_repo_key:
                raise PluginIdentityConflict("该用户名下的插件名已关联其他仓库")
            return existing["id"]
        plugin_id = payload.get("id") or payload["name"]
        if await connection.fetchval("SELECT 1 FROM market_plugins WHERE id = $1", plugin_id):
            return new_id("astrbot_plugin")
        return plugin_id

    async def get_comment(self, comment_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow("SELECT * FROM market_comments WHERE id = $1", comment_id)
        return self._comment_from_record(row) if row else None

    @plugin_identity_write
    async def submit_plugin(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        metadata = plugin_metadata_from_payload(payload)
        repo_provider, repo_external_id, normalized_repo = _canonical_repo_fields(
            str(payload.get("repo") or ""),
            str(payload.get("repo_provider") or ""),
            str(payload.get("repo_external_id") or ""),
        )
        owner_username = str(
            user.get("public_username")
            or user.get("github_login")
            or user.get("internal_username")
            or ""
        )
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                plugin_id = await self._plugin_registration_id(connection, user, payload)
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_plugins (
                        id, name, display_name, desc_text, author, repo,
                        repo_provider, repo_external_id, normalized_repo,
                        tags, social_link,
                        owner_user_id, owner_github_login, owner_username, status, stars, likes,
                        comments_count, metadata, repo_version
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14,
                            'pending', 0, 0, 0, $15::jsonb, $16)
                    ON CONFLICT (id) DO UPDATE
                       SET name = EXCLUDED.name,
                           display_name = EXCLUDED.display_name,
                           desc_text = EXCLUDED.desc_text,
                           author = EXCLUDED.author,
                           repo = EXCLUDED.repo,
                           repo_provider = COALESCE(NULLIF(EXCLUDED.repo_provider, ''), market_plugins.repo_provider),
                           repo_external_id = COALESCE(NULLIF(EXCLUDED.repo_external_id, ''), market_plugins.repo_external_id),
                           normalized_repo = COALESCE(NULLIF(EXCLUDED.normalized_repo, ''), market_plugins.normalized_repo),
                           tags = EXCLUDED.tags,
                           social_link = EXCLUDED.social_link,
                           repo_version = COALESCE(NULLIF(EXCLUDED.repo_version, ''), market_plugins.repo_version),
                           version_updated_at = CASE
                               WHEN NULLIF(EXCLUDED.repo_version, '') IS NOT NULL
                                    AND market_plugins.repo_version IS DISTINCT FROM EXCLUDED.repo_version
                               THEN now()
                               ELSE market_plugins.version_updated_at
                           END,
                           metadata = EXCLUDED.metadata ||
                               CASE WHEN market_plugins.metadata ? 'cdn_enabled'
                                    THEN jsonb_build_object(
                                        'cdn_enabled', market_plugins.metadata->'cdn_enabled',
                                        'cdn_generation', COALESCE(market_plugins.metadata->'cdn_generation', '0'::jsonb),
                                        'cdn_whitelist', COALESCE(market_plugins.metadata->'cdn_whitelist', 'false'::jsonb))
                                    ELSE '{}'::jsonb END,
                           updated_at = now()
                     WHERE market_plugins.owner_user_id = EXCLUDED.owner_user_id
                    RETURNING *
                    """,
                    plugin_id,
                    payload["name"],
                    payload.get("display_name") or "",
                    payload["desc"],
                    payload["author"],
                    payload["repo"],
                    repo_provider,
                    repo_external_id,
                    normalized_repo,
                    payload.get("tags", []),
                    payload.get("social_link", ""),
                    user["id"],
                    user.get("github_login", ""),
                    owner_username,
                    metadata,
                    payload.get("repo_version") or payload.get("version") or "",
                )
                if not row:
                    raise PermissionError("plugin_owner_mismatch")
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

    @plugin_identity_write
    async def register_plugin(
        self, user: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = plugin_metadata_from_payload(payload)
        repo_provider, repo_external_id, normalized_repo = _canonical_repo_fields(
            str(payload.get("repo") or ""),
            str(payload.get("repo_provider") or ""),
            str(payload.get("repo_external_id") or ""),
        )
        owner_username = str(
            user.get("public_username")
            or user.get("github_login")
            or user.get("internal_username")
            or ""
        )
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                plugin_id = await self._plugin_registration_id(connection, user, payload)
                row = await connection.fetchrow(
                    """
                    INSERT INTO market_plugins (
                        id, name, display_name, desc_text, author, repo,
                        repo_provider, repo_external_id, normalized_repo,
                        tags, social_link,
                        owner_user_id, owner_github_login, owner_username, status, stars, likes,
                        comments_count, metadata, category, category_source, repo_version
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14,
                            'pending', 0, 0, 0, $15::jsonb, $16, 'user', $17)
                    ON CONFLICT (id) DO UPDATE SET id = market_plugins.id
                      WHERE market_plugins.owner_user_id = EXCLUDED.owner_user_id
                    RETURNING *
                    """,
                    plugin_id,
                    payload["name"],
                    payload.get("display_name") or "",
                    payload["desc"],
                    payload["author"],
                    payload["repo"],
                    repo_provider,
                    repo_external_id,
                    normalized_repo,
                    payload.get("tags", []),
                    payload.get("social_link", ""),
                    user["id"],
                    user.get("github_login", ""),
                    owner_username,
                    metadata,
                    payload.get("category", "other"),
                    payload.get("version", ""),
                )
                if not row:
                    raise PermissionError("plugin_owner_mismatch")
                return self._plugin_from_record(row)

    async def list_submissions(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM market_submissions WHERE status = 'pending' ORDER BY created_at DESC"
        )
        return [self._submission_from_record(row) for row in rows]

    async def update_plugin_status(
        self,
        plugin_id: str,
        status: str,
        by_user_id: str | None,
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE market_plugins
                       SET status = $2,
                           moderated_by = $3,
                           metadata = CASE
                               WHEN $2 = 'listed'
                               THEN metadata - 'unlist_reason' - 'unlisted_at' - 'unlisted_by'
                               ELSE metadata
                           END,
                           listed_at = CASE WHEN $2 = 'listed' THEN now() ELSE listed_at END,
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    plugin_id,
                    status,
                    by_user_id,
                )
                if row and status != "pending":
                    await connection.execute(
                        """
                        UPDATE market_submissions
                           SET status = $2
                         WHERE plugin_id = $1
                           AND status = 'pending'
                        """,
                        plugin_id,
                        status,
                    )
        return self._plugin_from_record(row) if row else None

    async def unlist_plugin(
        self,
        plugin_id: str,
        by_user_id: str,
        reason: str,
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE market_plugins
                       SET status = 'unlisted',
                           moderated_by = $2,
                           metadata = metadata || $3::jsonb,
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    plugin_id,
                    by_user_id,
                    {
                        "unlist_reason": reason,
                        "unlisted_at": utc_now(),
                        "unlisted_by": by_user_id,
                    },
                )
                if row:
                    await connection.execute(
                        """
                        UPDATE market_submissions
                           SET status = 'unlisted'
                         WHERE plugin_id = $1
                           AND status = 'pending'
                        """,
                        plugin_id,
                    )
        return self._plugin_from_record(row) if row else None

    async def request_plugin_listing(
        self,
        plugin_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        current = await self.get_plugin(plugin_id)
        if not current:
            return None
        payload = submission_payload_from_plugin(current)
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE market_plugins
                       SET status = 'pending',
                           moderated_by = NULL,
                           updated_at = now()
                     WHERE id = $1
                 RETURNING *
                    """,
                    plugin_id,
                )
                existing_submission_id = await connection.fetchval(
                    """
                    SELECT id
                      FROM market_submissions
                     WHERE plugin_id = $1
                       AND status = 'pending'
                  ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    plugin_id,
                )
                if existing_submission_id:
                    await connection.execute(
                        """
                        UPDATE market_submissions
                           SET user_id = $2,
                               payload = $3::jsonb
                         WHERE id = $1
                        """,
                        existing_submission_id,
                        user_id,
                        payload,
                    )
                else:
                    await connection.execute(
                        """
                        INSERT INTO market_submissions (id, plugin_id, user_id, payload, status)
                        VALUES ($1, $2, $3, $4::jsonb, 'pending')
                        """,
                        new_id("submission"),
                        plugin_id,
                        user_id,
                        payload,
                    )
        return self._plugin_from_record(row) if row else None

    @plugin_identity_write
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
        if "repo" in clean_patch:
            prev_norm = str(current.get("normalized_repo") or "")
            repo_provider, repo_external_id, normalized_repo = _canonical_repo_fields(
                str(clean_patch["repo"]),
                str(clean_patch.get("repo_provider") or ""),
                str(clean_patch.get("repo_external_id") or ""),
            )
            if normalized_repo == prev_norm:
                if "repo_provider" not in clean_patch and not repo_provider:
                    repo_provider = str(current.get("repo_provider") or "")
                if "repo_external_id" not in clean_patch:
                    repo_external_id = str(current.get("repo_external_id") or "")
        else:
            repo_provider, repo_external_id, normalized_repo = _canonical_repo_fields(
                str(updated.get("repo") or ""),
                str(updated.get("repo_provider") or ""),
                str(updated.get("repo_external_id") or ""),
            )
        updated["repo_provider"] = repo_provider
        updated["repo_external_id"] = repo_external_id
        updated["normalized_repo"] = normalized_repo
        metadata = {
            key: value
            for key, value in updated.items()
            if key not in PLUGIN_COLUMN_KEYS
            and key not in {"id", "created_at", "updated_at", "version_updated_at"}
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
                   repo_provider = $7,
                   repo_external_id = $8,
                   normalized_repo = $9,
                   tags = $10::jsonb,
                   social_link = $11,
                   owner_user_id = $12,
                   owner_github_login = $13,
                   owner_username = $14,
                   status = $15,
                   stars = $16,
                   likes = $17,
                   comments_count = $18,
                   moderated_by = $19,
                   repo_version = $20,
                   version_updated_at = CASE
                       WHEN NULLIF($20, '') IS NOT NULL
                            AND market_plugins.repo_version IS DISTINCT FROM $20
                       THEN now()
                       ELSE market_plugins.version_updated_at
                   END,
                   category = $21,
                   category_source = $22,
                   suggested_category = $23,
                   category_confidence = $24,
                   category_reason = $25,
                   metadata = $26::jsonb,
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
            repo_provider,
            repo_external_id,
            normalized_repo,
            updated["tags"],
            updated.get("social_link", ""),
            updated["owner_user_id"],
            updated["owner_github_login"],
            updated.get("owner_username") or updated.get("owner_github_login") or "",
            updated["status"],
            int(updated.get("stars") or 0),
            int(updated.get("likes") or 0),
            int(updated.get("comments_count") or 0),
            updated.get("moderated_by"),
            updated.get("repo_version", ""),
            updated.get("category", "other"),
            updated.get("category_source", "user"),
            updated.get("suggested_category", ""),
            updated.get("category_confidence"),
            updated.get("category_reason", ""),
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
                    INSERT INTO market_comments (id, plugin_id, user_id, parent_id, body, likes)
                    VALUES ($1, $2, $3, $4, $5, 0)
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

    async def like_plugin(self, plugin_id: str, user_id: str) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                plugin = await connection.fetchrow(
                    "SELECT * FROM market_plugins WHERE id = $1 FOR UPDATE", plugin_id
                )
                if not plugin:
                    return None
                inserted = await connection.fetchval(
                    """
                    INSERT INTO market_plugin_likes (plugin_id, user_id)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                    """,
                    plugin_id,
                    user_id,
                )
                if inserted:
                    await connection.execute(
                        """
                        UPDATE market_plugins
                           SET likes = likes + 1, updated_at = now()
                         WHERE id = $1
                        """,
                        plugin_id,
                    )
                row = await connection.fetchrow(
                    "SELECT * FROM market_plugins WHERE id = $1", plugin_id
                )
                return {**self._plugin_from_record(row), "liked": True} if row else None

    async def unlike_plugin(self, plugin_id: str, user_id: str) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                plugin = await connection.fetchrow(
                    "SELECT * FROM market_plugins WHERE id = $1 FOR UPDATE", plugin_id
                )
                if not plugin:
                    return None
                deleted = await connection.fetchval(
                    """
                    DELETE FROM market_plugin_likes
                     WHERE plugin_id = $1 AND user_id = $2
                 RETURNING 1
                    """,
                    plugin_id,
                    user_id,
                )
                if deleted:
                    await connection.execute(
                        """
                        UPDATE market_plugins
                           SET likes = GREATEST(likes - 1, 0), updated_at = now()
                         WHERE id = $1
                        """,
                        plugin_id,
                    )
                row = await connection.fetchrow(
                    "SELECT * FROM market_plugins WHERE id = $1", plugin_id
                )
                return {**self._plugin_from_record(row), "liked": False} if row else None

    async def like_comment(self, comment_id: str, user_id: str) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                comment = await connection.fetchrow(
                    "SELECT * FROM market_comments WHERE id = $1 AND deleted = false FOR UPDATE",
                    comment_id,
                )
                if not comment:
                    return None
                inserted = await connection.fetchval(
                    """
                    INSERT INTO market_comment_likes (comment_id, user_id)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                    """,
                    comment_id,
                    user_id,
                )
                if inserted:
                    await connection.execute(
                        "UPDATE market_comments SET likes = likes + 1 WHERE id = $1",
                        comment_id,
                    )
                row = await connection.fetchrow(
                    "SELECT * FROM market_comments WHERE id = $1", comment_id
                )
                return {**self._comment_from_record(row), "liked": True} if row else None

    async def unlike_comment(self, comment_id: str, user_id: str) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                comment = await connection.fetchrow(
                    "SELECT * FROM market_comments WHERE id = $1 AND deleted = false FOR UPDATE",
                    comment_id,
                )
                if not comment:
                    return None
                deleted = await connection.fetchval(
                    """
                    DELETE FROM market_comment_likes
                     WHERE comment_id = $1 AND user_id = $2
                 RETURNING 1
                    """,
                    comment_id,
                    user_id,
                )
                if deleted:
                    await connection.execute(
                        "UPDATE market_comments SET likes = GREATEST(likes - 1, 0) WHERE id = $1",
                        comment_id,
                    )
                row = await connection.fetchrow(
                    "SELECT * FROM market_comments WHERE id = $1", comment_id
                )
                return {**self._comment_from_record(row), "liked": False} if row else None

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
                    await connection.execute(
                        """
                        UPDATE market_comments
                           SET deleted = true, deleted_by = $2, deleted_at = now()
                         WHERE parent_id = $1 AND deleted = false
                        """,
                        comment_id,
                        by_user_id,
                    )
                    await self._refresh_comments_count(connection, row["plugin_id"])
                return self._comment_from_record(row) if row else None

    async def mute_user(
        self,
        user_id: str,
        muted_until: str,
        by_user_id: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE market_users
               SET muted_until = $2::timestamptz,
                   muted_by = $3,
                   muted_reason = $4,
                   updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            user_id,
            _pg_timestamp(muted_until),
            by_user_id,
            reason,
        )
        return self._user_from_record(row) if row else None

    async def unmute_user(self, user_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE market_users
               SET muted_until = NULL,
                   muted_by = NULL,
                   muted_reason = '',
                   updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            user_id,
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

    async def set_user_uploaded_avatar(
        self,
        user_id: str,
        *,
        url: str,
        profile_id: str,
        object_id: str,
        sha256: str,
        expected_object_id: str,
    ) -> dict[str, Any] | None:
        # The WHERE clause is the compare-and-set: a concurrent upload that already moved the
        # pointer makes this no-op return None instead of overwriting the newer avatar.
        row = await self._pool().fetchrow(
            """
            UPDATE market_users
               SET uploaded_avatar_url = $3,
                   uploaded_avatar_profile_id = $4,
                   uploaded_avatar_object_id = $5,
                   uploaded_avatar_sha256 = $6,
                   uploaded_avatar_updated_at = now(),
                   updated_at = now()
             WHERE id = $1
               AND COALESCE(uploaded_avatar_object_id, '') = $2
         RETURNING *
            """,
            user_id,
            expected_object_id,
            url,
            profile_id,
            object_id,
            sha256,
        )
        return self._user_from_record(row) if row else None

    async def clear_user_uploaded_avatar(
        self,
        user_id: str,
        *,
        expected_object_id: str,
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE market_users
               SET uploaded_avatar_url = '',
                   uploaded_avatar_profile_id = '',
                   uploaded_avatar_object_id = '',
                   uploaded_avatar_sha256 = '',
                   uploaded_avatar_updated_at = NULL,
                   updated_at = now()
             WHERE id = $1
               AND COALESCE(uploaded_avatar_object_id, '') = $2
         RETURNING *
            """,
            user_id,
            expected_object_id,
        )
        return self._user_from_record(row) if row else None

    @plugin_identity_write
    async def delete_user(
        self,
        user_id: str,
        replacement_user_id: str,
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                target = await connection.fetchrow(
                    "SELECT * FROM market_users WHERE id = $1 FOR UPDATE",
                    user_id,
                )
                if not target:
                    return None
                await connection.execute(
                    """
                    UPDATE market_plugins
                       SET owner_user_id = $2,
                           updated_at = now()
                     WHERE owner_user_id = $1
                    """,
                    user_id,
                    replacement_user_id,
                )
                await connection.execute(
                    "UPDATE market_plugins SET moderated_by = NULL WHERE moderated_by = $1",
                    user_id,
                )
                await connection.execute(
                    "UPDATE market_comments SET deleted_by = NULL WHERE deleted_by = $1",
                    user_id,
                )
                await connection.execute(
                    "UPDATE market_users SET muted_by = NULL WHERE muted_by = $1",
                    user_id,
                )
                await connection.execute(
                    """
                    UPDATE market_git_repository_bindings
                       SET user_id = $2,
                           credential_ref = CASE WHEN revoked_at IS NULL THEN '' ELSE credential_ref END,
                           binding_generation = CASE
                               WHEN revoked_at IS NULL THEN binding_generation + 1
                               ELSE binding_generation
                           END,
                           updated_at = now()
                     WHERE user_id = $1
                    """,
                    user_id,
                    replacement_user_id,
                )
                await connection.execute("DELETE FROM market_users WHERE id = $1", user_id)
                await connection.execute(
                    """
                    UPDATE market_comments c
                       SET likes = counts.total
                      FROM (
                          SELECT c.id, count(cl.comment_id)::integer AS total
                            FROM market_comments c
                       LEFT JOIN market_comment_likes cl ON cl.comment_id = c.id
                        GROUP BY c.id
                      ) counts
                     WHERE c.id = counts.id
                    """
                )
                await connection.execute(
                    """
                    UPDATE market_plugins p
                       SET likes = counts.likes,
                           comments_count = counts.comments_count,
                           updated_at = now()
                      FROM (
                          SELECT p.id,
                                 count(DISTINCT pl.user_id)::integer AS likes,
                                 count(DISTINCT c.id)::integer AS comments_count
                            FROM market_plugins p
                       LEFT JOIN market_plugin_likes pl ON pl.plugin_id = p.id
                       LEFT JOIN market_comments c
                              ON c.plugin_id = p.id
                             AND c.deleted = false
                        GROUP BY p.id
                      ) counts
                     WHERE p.id = counts.id
                    """
                )
                return self._user_from_record(target)

    async def update_user_profile(
        self,
        user_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any] | None:
        values = {key: profile[key] for key in USER_PROFILE_FIELDS if key in profile}
        if not values:
            return await self.get_user_by_id(user_id)
        args: list[Any] = [user_id]
        set_clauses: list[str] = []
        for key, value in values.items():
            args.append(value)
            set_clauses.append(f"{key} = ${len(args)}")
        row = await self._pool().fetchrow(
            f"""
            UPDATE market_users
               SET {", ".join(set_clauses)},
                   updated_at = now()
             WHERE id = $1
         RETURNING *
            """,
            *args,
        )
        return self._user_from_record(row) if row else None

    @plugin_identity_write
    async def merge_user_into_user(
        self, from_user_id: str, to_user_id: str
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                source = await connection.fetchrow(
                    "SELECT * FROM market_users WHERE id = $1 FOR UPDATE",
                    from_user_id,
                )
                target = await connection.fetchrow(
                    "SELECT * FROM market_users WHERE id = $1 FOR UPDATE",
                    to_user_id,
                )
                if not source or not target:
                    return self._user_from_record(target) if target else None

                await connection.execute(
                    """
                    UPDATE market_plugins
                       SET owner_user_id = $2, updated_at = now()
                     WHERE owner_user_id = $1
                    """,
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "UPDATE market_plugins SET moderated_by = $2 WHERE moderated_by = $1",
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "UPDATE market_submissions SET user_id = $2 WHERE user_id = $1",
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "UPDATE market_comments SET user_id = $2 WHERE user_id = $1",
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "UPDATE market_comments SET deleted_by = $2 WHERE deleted_by = $1",
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "UPDATE market_users SET muted_by = $2 WHERE muted_by = $1",
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "UPDATE market_announcements SET author_user_id = $2 WHERE author_user_id = $1",
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "UPDATE market_notifications SET user_id = $2 WHERE user_id = $1",
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "UPDATE market_api_keys SET user_id = $2 WHERE user_id = $1",
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    """
                    UPDATE market_oauth_identities
                       SET user_id = $2, updated_at = now()
                     WHERE user_id = $1
                    """,
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    """
                    UPDATE market_oauth_credentials
                       SET user_id = $2, updated_at = now()
                     WHERE user_id = $1
                    """,
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    """
                    UPDATE market_git_repository_bindings
                       SET user_id = $2, updated_at = now()
                     WHERE user_id = $1
                    """,
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    """
                    INSERT INTO market_plugin_likes (plugin_id, user_id, created_at)
                    SELECT plugin_id, $2, min(created_at)
                      FROM market_plugin_likes
                     WHERE user_id = $1
                       AND NOT EXISTS (
                           SELECT 1 FROM market_plugin_likes existing
                            WHERE existing.plugin_id = market_plugin_likes.plugin_id
                              AND existing.user_id = $2
                       )
                  GROUP BY plugin_id
                    """,
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "DELETE FROM market_plugin_likes WHERE user_id = $1", from_user_id
                )
                await connection.execute(
                    """
                    UPDATE market_plugins p
                       SET likes = counts.total
                      FROM (
                          SELECT plugin_id, count(*)::integer AS total
                            FROM market_plugin_likes
                        GROUP BY plugin_id
                      ) counts
                     WHERE p.id = counts.plugin_id
                    """
                )
                await connection.execute(
                    """
                    INSERT INTO market_comment_likes (comment_id, user_id, created_at)
                    SELECT comment_id, $2, min(created_at)
                      FROM market_comment_likes
                     WHERE user_id = $1
                       AND NOT EXISTS (
                           SELECT 1 FROM market_comment_likes existing
                            WHERE existing.comment_id = market_comment_likes.comment_id
                              AND existing.user_id = $2
                       )
                  GROUP BY comment_id
                    """,
                    from_user_id,
                    to_user_id,
                )
                await connection.execute(
                    "DELETE FROM market_comment_likes WHERE user_id = $1", from_user_id
                )
                await connection.execute(
                    """
                    UPDATE market_comments c
                       SET likes = counts.total
                      FROM (
                          SELECT comment_id, count(*)::integer AS total
                            FROM market_comment_likes
                        GROUP BY comment_id
                      ) counts
                     WHERE c.id = counts.comment_id
                    """
                )
                # Keep the custom avatar when only the source account had one.
                await connection.execute(
                    """
                    UPDATE market_users
                       SET uploaded_avatar_url = source.uploaded_avatar_url,
                           uploaded_avatar_profile_id = source.uploaded_avatar_profile_id,
                           uploaded_avatar_object_id = source.uploaded_avatar_object_id,
                           uploaded_avatar_sha256 = source.uploaded_avatar_sha256,
                           uploaded_avatar_updated_at = source.uploaded_avatar_updated_at,
                           updated_at = now()
                      FROM (SELECT * FROM market_users WHERE id = $1) AS source
                     WHERE market_users.id = $2
                       AND COALESCE(market_users.uploaded_avatar_object_id, '') = ''
                    """,
                    from_user_id,
                    to_user_id,
                )
                await connection.execute("DELETE FROM market_users WHERE id = $1", from_user_id)
                return self._user_from_record(target)

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

    async def update_announcement(
        self,
        announcement_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE market_announcements
            SET title = COALESCE($2, title),
                body = COALESCE($3, body),
                updated_at = now()
            WHERE id = $1
              AND ($4::text IS NULL OR updated_at = $4::timestamptz)
            RETURNING *
            """,
            announcement_id,
            title,
            body,
            expected_updated_at,
        )
        if row is None and expected_updated_at is not None:
            exists = await self._pool().fetchval(
                "SELECT EXISTS(SELECT 1 FROM market_announcements WHERE id = $1)",
                announcement_id,
            )
            if exists:
                raise AnnouncementConflict("Announcement was updated by another administrator")
        return self._announcement_from_record(row) if row is not None else None

    async def delete_announcement(self, announcement_id: str) -> bool:
        row = await self._pool().fetchrow(
            "DELETE FROM market_announcements WHERE id = $1 RETURNING id",
            announcement_id,
        )
        return row is not None

    async def list_announcements(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM market_announcements ORDER BY created_at DESC"
        )
        return [self._announcement_from_record(row) for row in rows]

    async def create_notification(
        self,
        user_id: str,
        title: str,
        body: str,
        notification_type: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self._pool().fetchrow(
            """
            INSERT INTO market_notifications (id, user_id, type, title, body, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING *
            """,
            new_id("notification"),
            user_id,
            notification_type,
            title,
            body,
            metadata or {},
        )
        return self._notification_from_record(row)

    async def create_notification_once(
        self,
        user_id: str,
        title: str,
        body: str,
        notification_type: str,
        metadata: dict[str, Any],
        dedupe_key: str,
    ) -> dict[str, Any]:
        async with self._pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO market_notifications (
                    id, user_id, type, title, body, metadata, dedupe_key
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                new_id("notification"),
                user_id,
                notification_type,
                title,
                body,
                metadata,
                dedupe_key,
            )
            if row is None:
                row = await connection.fetchrow(
                    "SELECT * FROM market_notifications WHERE dedupe_key = $1",
                    dedupe_key,
                )
            if row is None:
                raise RuntimeError("notification_deduplication_failed")
            return self._notification_from_record(row)

    async def list_notifications(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT * FROM market_notifications
             WHERE user_id = $1
          ORDER BY created_at DESC
             LIMIT $2 OFFSET $3
            """,
            user_id,
            limit,
            offset,
        )
        return [self._notification_from_record(row) for row in rows]

    async def count_notifications(self, user_id: str) -> int:
        return int(
            await self._pool().fetchval(
                "SELECT count(*) FROM market_notifications WHERE user_id = $1",
                user_id,
            )
            or 0
        )

    async def count_unread_notifications(self, user_id: str) -> int:
        return int(
            await self._pool().fetchval(
                """
                SELECT count(*) FROM market_notifications
                 WHERE user_id = $1
                   AND read = false
                """,
                user_id,
            )
            or 0
        )

    async def mark_notifications_read(self, user_id: str) -> int:
        result = await self._pool().execute(
            """
            UPDATE market_notifications
               SET read = true
             WHERE user_id = $1
               AND read = false
            """,
            user_id,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def delete_notifications(
        self,
        user_id: str,
        notification_ids: list[str] | None = None,
    ) -> int:
        if notification_ids is not None:
            ids = [str(item) for item in notification_ids if str(item)]
            if not ids:
                return 0
            result = await self._pool().execute(
                """
                DELETE FROM market_notifications
                 WHERE user_id = $1
                   AND id = ANY($2::text[])
                """,
                user_id,
                ids,
            )
            return int(result.rsplit(" ", 1)[-1])
        result = await self._pool().execute(
            "DELETE FROM market_notifications WHERE user_id = $1",
            user_id,
        )
        return int(result.rsplit(" ", 1)[-1])

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
            generate_access_key(),
        )
        return self._api_key_from_record(row)

    async def list_api_keys(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch("SELECT * FROM market_api_keys ORDER BY created_at DESC")
        return [self._api_key_from_record(row) for row in rows]

    async def list_api_keys_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            "SELECT * FROM market_api_keys WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )
        return [self._api_key_from_record(row) for row in rows]

    async def delete_api_key(self, user_id: str, key_id: str) -> int:
        result = await self._pool().execute(
            "DELETE FROM market_api_keys WHERE id = $1 AND user_id = $2",
            key_id,
            user_id,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def list_options(self) -> dict[str, str]:
        rows = await self._pool().fetch("SELECT option_key, option_value FROM market_options")
        return {str(row["option_key"]): str(row["option_value"]) for row in rows}

    async def upsert_options(self, values: dict[str, str]) -> dict[str, str]:
        if not values:
            return await self.list_options()
        rows = [(str(key), str(value)) for key, value in values.items()]
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.executemany(
                    """
                    INSERT INTO market_options (option_key, option_value)
                    VALUES ($1, $2)
                    ON CONFLICT (option_key) DO UPDATE
                       SET option_value = EXCLUDED.option_value,
                           updated_at = now()
                    """,
                    rows,
                )
        return await self.list_options()

    _RESERVE_CHALLENGE_LUA = """
    local key = KEYS[1]
    local expected_purpose = ARGV[1]
    local expected_digest = ARGV[2]
    local max_attempts = tonumber(ARGV[3])
    if redis.call('EXISTS', key) == 0 then
        return {'not_found', ''}
    end
    local purpose = redis.call('HGET', key, 'purpose')
    if purpose ~= expected_purpose then
        return {'wrong_purpose', ''}
    end
    local state = redis.call('HGET', key, 'state')
    if state ~= 'pending' then
        return {'in_progress', ''}
    end
    local attempts = tonumber(redis.call('HGET', key, 'attempts') or '0')
    if attempts >= max_attempts then
        redis.call('DEL', key)
        return {'too_many_attempts', ''}
    end
    local actual_digest = redis.call('HGET', key, 'code_digest')
    if actual_digest ~= expected_digest then
        attempts = redis.call('HINCRBY', key, 'attempts', 1)
        if attempts >= max_attempts then
            redis.call('DEL', key)
            return {'too_many_attempts', ''}
        end
        return {'invalid_code', ''}
    end
    redis.call('HSET', key, 'state', 'in_progress')
    local payload = redis.call('HGET', key, 'payload')
    return {'ok', payload or '{}'}
    """

    async def create_session(self, user_id: str, token: str | None = None) -> dict[str, Any]:
        user = await self.get_user_by_id(user_id)
        session = {
            "token": token or f"sess_{secrets.token_urlsafe(24)}",
            "user_id": user_id,
            "session_version": int((user or {}).get("session_version") or 1),
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
        user = await self.get_user_by_id(session["user_id"])
        if not user:
            return None
        expected_version = int(user.get("session_version") or 1)
        if int(session.get("session_version") or 1) != expected_version:
            await self.revoke_session(token)
            return None
        session["last_seen_at"] = utc_now()
        await self._redis().set(
            self._session_key(token),
            serialize_json(session),
            ex=self.session_ttl_seconds,
        )
        return user

    async def revoke_session(self, token: str) -> bool:
        await self.delete_github_authorization(hashlib.sha256(token.encode()).hexdigest())
        return bool(await self._redis().delete(self._session_key(token)))

    async def revoke_all_user_sessions(self, user_id: str) -> int:
        await self._pool().execute(
            """
            UPDATE market_users
               SET session_version = session_version + 1,
                   updated_at = now()
             WHERE id = $1
            """,
            user_id,
        )
        latest_ref = await self.get_latest_github_authorization(user_id)
        if latest_ref:
            await self.delete_github_authorization(latest_ref)
        await self._redis().delete(f"astrbot_market:github_oauth_user:{user_id}")
        return 1

    async def save_email_challenge(
        self, challenge_id: str, record: dict[str, Any], ttl_seconds: int
    ) -> None:
        key = f"astrbot_market:email_challenge:{challenge_id}"
        mapping = {
            "purpose": str(record["purpose"]),
            "email": str(record.get("email") or ""),
            "salt": str(record["salt"]),
            "code_digest": str(record["code_digest"]),
            "state": str(record.get("state") or "pending"),
            "attempts": str(int(record.get("attempts") or 0)),
            "payload": serialize_json(record.get("payload") or {}),
        }
        pipe = self._redis().pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, max(1, int(ttl_seconds)))
        await pipe.execute()

    async def get_email_challenge_salt(self, challenge_id: str) -> str | None:
        key = f"astrbot_market:email_challenge:{challenge_id}"
        value = await self._redis().hget(key, "salt")
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return str(value) if value else None

    async def reserve_email_challenge(
        self,
        challenge_id: str,
        purpose: str,
        expected_digest: str,
        max_attempts: int,
    ) -> tuple[str, dict[str, Any] | None]:
        key = f"astrbot_market:email_challenge:{challenge_id}"
        result = await self._redis().eval(
            self._RESERVE_CHALLENGE_LUA,
            1,
            key,
            purpose,
            expected_digest,
            str(int(max_attempts)),
        )
        status = result[0].decode("utf-8") if isinstance(result[0], bytes) else str(result[0])
        raw_payload = (
            result[1].decode("utf-8") if isinstance(result[1], bytes) else str(result[1] or "")
        )
        if status == "ok":
            return ("ok", parse_json(raw_payload) if raw_payload else {})
        return (status, None)

    async def release_email_challenge(self, challenge_id: str) -> None:
        key = f"astrbot_market:email_challenge:{challenge_id}"
        if await self._redis().hget(key, "state") == "in_progress":
            await self._redis().hset(key, "state", "pending")

    async def delete_email_challenge(self, challenge_id: str) -> None:
        await self._redis().delete(f"astrbot_market:email_challenge:{challenge_id}")

    async def get_rate_limit_count(self, key: str) -> int:
        value = await self._redis().get(f"astrbot_market:rl:{key}")
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    async def increment_rate_limit(self, key: str, ttl_seconds: int) -> int:
        redis_key = f"astrbot_market:rl:{key}"
        count = int(await self._redis().incr(redis_key))
        if count == 1:
            await self._redis().expire(redis_key, max(1, int(ttl_seconds)))
        return count

    async def reserve_rate_limit(self, key: str, limit: int, ttl_seconds: int) -> bool:
        redis_key = f"astrbot_market:rl:{key}"
        result = await self._redis().eval(
            """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then
    return 0
end
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 1
            """,
            1,
            redis_key,
            str(int(limit)),
            str(max(1, int(ttl_seconds))),
        )
        return bool(result)

    async def decrement_rate_limit(self, key: str) -> None:
        await self._redis().eval(
            """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current > 0 then
    return redis.call('DECR', KEYS[1])
end
return 0
            """,
            1,
            f"astrbot_market:rl:{key}",
        )

    async def check_and_set_cooldown(self, key: str, ttl_seconds: int) -> int:
        redis_key = f"astrbot_market:cd:{key}"
        acquired = await self._redis().set(redis_key, "1", ex=max(1, int(ttl_seconds)), nx=True)
        if acquired:
            return 0
        ttl = int(await self._redis().ttl(redis_key) or 1)
        return max(1, ttl)

    async def clear_cooldown(self, key: str) -> None:
        await self._redis().delete(f"astrbot_market:cd:{key}")

    async def set_github_authorization(
        self, reference: str, ciphertext: str, ttl_seconds: int
    ) -> None:
        await self._redis().set(
            f"astrbot_market:github_oauth:{reference}", ciphertext, ex=ttl_seconds
        )

    async def get_github_authorization(self, reference: str) -> str | None:
        value = await self._redis().get(f"astrbot_market:github_oauth:{reference}")
        return value.decode() if isinstance(value, bytes) else value

    async def delete_github_authorization(self, reference: str) -> None:
        await self._redis().delete(f"astrbot_market:github_oauth:{reference}")

    async def set_latest_github_authorization(
        self, user_id: str, reference: str, ttl_seconds: int
    ) -> None:
        await self._redis().set(
            f"astrbot_market:github_oauth_user:{user_id}", reference, ex=ttl_seconds
        )

    async def get_latest_github_authorization(self, user_id: str) -> str | None:
        value = await self._redis().get(f"astrbot_market:github_oauth_user:{user_id}")
        return value.decode() if isinstance(value, bytes) else value

    async def list_github_authorization_references(self, user_id: str) -> list[str]:
        references: list[str] = []
        async for key in self._redis().scan_iter(match="astrbot_market:session:*", count=200):
            payload = await self._redis().get(key)
            if not payload:
                continue
            session = parse_json(payload)
            if str(session.get("user_id") or "") != str(user_id):
                continue
            token = str(session.get("token") or "")
            if not token:
                decoded = key.decode() if isinstance(key, bytes) else str(key)
                token = decoded.rsplit(":", 1)[-1]
            references.append(hashlib.sha256(token.encode()).hexdigest())
        latest = await self.get_latest_github_authorization(user_id)
        if latest:
            references.append(latest)
        return list(dict.fromkeys(references))

    async def clear_latest_github_authorization(self, user_id: str) -> None:
        await self._redis().delete(f"astrbot_market:github_oauth_user:{user_id}")

    async def list_comments(self, plugin_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT c.*,
                   u.public_username,
                   u.github_login,
                   u.github_name,
                   u.avatar_url,
                   u.internal_username,
                   u.role
            FROM market_comments c
         LEFT JOIN market_users u ON u.id = c.user_id
             WHERE c.plugin_id = $1 AND c.deleted = false
          ORDER BY c.created_at ASC
            """,
            plugin_id,
        )
        return [self._comment_from_record(row) for row in rows]

    async def has_plugin_like(self, plugin_id: str, user_id: str) -> bool:
        return bool(
            await self._pool().fetchval(
                "SELECT 1 FROM market_plugin_likes WHERE plugin_id = $1 AND user_id = $2",
                plugin_id,
                user_id,
            )
        )

    async def list_liked_comment_ids(self, plugin_id: str, user_id: str) -> list[str]:
        rows = await self._pool().fetch(
            """
            SELECT cl.comment_id
              FROM market_comment_likes cl
              JOIN market_comments c ON c.id = cl.comment_id
             WHERE c.plugin_id = $1
               AND c.deleted = false
               AND cl.user_id = $2
            """,
            plugin_id,
            user_id,
        )
        return [row["comment_id"] for row in rows]

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

    async def record_plugin_download(self, plugin_id: str, requester_hash: str) -> bool:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchval(
                    """
                    INSERT INTO market_plugin_download_windows (plugin_id, requester_hash)
                    SELECT id, $2 FROM market_plugins WHERE id = $1
                    ON CONFLICT (plugin_id, requester_hash) DO UPDATE
                       SET window_started_at = now()
                     WHERE market_plugin_download_windows.window_started_at
                           <= now() - interval '1 minute'
                    RETURNING 1
                    """,
                    plugin_id,
                    requester_hash,
                )
                if inserted is None:
                    return False
                result = await connection.execute(
                    """
                    UPDATE market_plugins
                       SET download_count = download_count + 1
                     WHERE id = $1
                    """,
                    plugin_id,
                )
                return result == "UPDATE 1"

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
        data = serialize_value(dict(row))
        data["updated_at"] = data.get("updated_at") or data["created_at"]
        return data

    def _notification_from_record(self, row: asyncpg.Record) -> dict[str, Any]:
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
