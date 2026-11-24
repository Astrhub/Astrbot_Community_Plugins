"""Provider registry, runtime OAuth configuration helpers, and automatic token refresh."""

from __future__ import annotations

import asyncio
import inspect
import weakref
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException

from .base import BaseGitProviderAdapter, ProviderOAuthConfig, utc_now_iso
from .credentials import CredentialStore
from .gitea import GiteaAdapter
from .gitlab import GitLabAdapter
from .models import (
    PROVIDER_CANONICAL_HOSTS,
    PROVIDER_DEFAULT_SCOPES,
    PROVIDER_INSTANCE_URLS,
    PROVIDER_LABELS,
    SUPPORTED_PROVIDERS,
    GitSourceError,
    OAuthTokenSet,
)
from .v5_providers import AtomGitAdapter, GiteeAdapter

TOKEN_REFRESH_SKEW_SECONDS = 120
_REFRESH_LOCKS: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _refresh_lock_for(store: Any, credential_ref: str) -> asyncio.Lock:
    lock_key = (id(store), str(credential_ref))
    lock = _REFRESH_LOCKS.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _REFRESH_LOCKS[lock_key] = lock
    return lock


_ADAPTER_CLASSES: dict[str, type[BaseGitProviderAdapter]] = {
    "gitlab": GitLabAdapter,
    "gitee": GiteeAdapter,
    "atomgit": AtomGitAdapter,
    "gitea": GiteaAdapter,
}


async def _store_call(store: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    fn = getattr(store, method)
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _parse_bool(raw: Any, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _default_callback_url(web_url: str, provider: str) -> str:
    base = str(web_url or "http://127.0.0.1:8787").strip().rstrip("/")
    if provider == "github":
        return f"{base}/v1/auth/github/callback"
    return f"{base}/v1/auth/oauth/{provider}/callback"


def get_provider_oauth_config(
    settings: Any,
    provider: str,
    runtime_config: Mapping[str, str] | None = None,
) -> ProviderOAuthConfig:
    key = str(provider or "").strip().lower()
    if key not in SUPPORTED_PROVIDERS:
        raise GitSourceError("unsupported_git_provider", "不支持的 OAuth 平台", status_code=404)
    cfg = runtime_config or {}
    prefix = key.upper()
    web_url = str(cfg.get("WEB_URL") or getattr(settings, "web_url", "http://127.0.0.1:8787"))
    enabled = _parse_bool(
        cfg.get(f"{prefix}_LOGIN_ENABLED"),
        bool(getattr(settings, f"{key}_login_enabled", False)),
    )
    client_id = str(
        cfg.get(f"{prefix}_CLIENT_ID", getattr(settings, f"{key}_client_id", ""))
    ).strip()
    client_secret = str(getattr(settings, f"{key}_client_secret", "")).strip()
    callback_url = str(
        cfg.get(f"{prefix}_CALLBACK_URL", getattr(settings, f"{key}_callback_url", ""))
    ).strip() or _default_callback_url(web_url, key)
    scope = (
        str(cfg.get(f"{prefix}_SCOPE", getattr(settings, f"{key}_scope", ""))).strip()
        or PROVIDER_DEFAULT_SCOPES[key]
    )
    api_base_url = ""
    if key == "atomgit":
        api_base_url = str(
            cfg.get("ATOMGIT_API_BASE_URL", getattr(settings, "atomgit_api_base_url", ""))
        ).strip()
    return ProviderOAuthConfig(
        provider=key,
        enabled=enabled,
        client_id=client_id,
        client_secret=client_secret,
        callback_url=callback_url,
        scope=scope,
        api_base_url=api_base_url,
    )


def get_provider_adapter(
    settings: Any,
    provider: str,
    *,
    runtime_config: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> BaseGitProviderAdapter:
    key = str(provider or "").strip().lower()
    adapter_cls = _ADAPTER_CLASSES.get(key)
    if adapter_cls is None:
        raise GitSourceError("unsupported_git_provider", "不支持的第三方 Git 平台", status_code=404)
    oauth_cfg = get_provider_oauth_config(settings, key, runtime_config=runtime_config)
    return adapter_cls(oauth_cfg, transport=transport)


def public_oauth_providers_config(
    settings: Any,
    runtime_config: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for provider in SUPPORTED_PROVIDERS:
        cfg = get_provider_oauth_config(settings, provider, runtime_config=runtime_config)
        items.append(
            {
                "provider": provider,
                "label": PROVIDER_LABELS[provider],
                "host": PROVIDER_CANONICAL_HOSTS[provider],
                "instance_url": PROVIDER_INSTANCE_URLS[provider],
                "enabled": cfg.enabled,
                "configured": bool(cfg.client_id and cfg.client_secret),
                "login_path": (
                    "/v1/auth/github/login"
                    if provider == "github"
                    else f"/v1/auth/oauth/{provider}/login"
                ),
            }
        )
    return items


def _is_token_expiring(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= datetime.now(UTC) + timedelta(seconds=TOKEN_REFRESH_SKEW_SECONDS)


async def ensure_valid_provider_token(
    store: Any,
    settings: Any,
    user: Mapping[str, Any],
    provider: str,
    *,
    adapter: BaseGitProviderAdapter | None = None,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], OAuthTokenSet]:
    key = str(provider or "").strip().lower()
    label = PROVIDER_LABELS.get(key, key)
    user_id = str(user.get("id") or "")
    identities = await _store_call(store, "list_oauth_identities", user_id)
    matching = [
        item
        for item in identities
        if str(item.get("provider") or "").lower() == key and not item.get("revoked_at")
    ]
    if not matching:
        raise HTTPException(
            409,
            {"code": f"{key}_authorization_required", "error": f"请先连接 {label} 账号后再操作"},
        )
    identity = max(
        matching,
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        ),
    )
    cred_store = CredentialStore(store, getattr(settings, "oauth_credential_key", ""))
    cred_ref = str(identity.get("credential_ref") or "").strip()
    token_set = await cred_store.load_token_set(
        cred_ref,
        expected_user_id=user_id,
        expected_provider=key,
    )
    if token_set is None:
        raise HTTPException(
            409,
            {"code": f"{key}_authorization_required", "error": f"{label} 授权已失效，请重新连接"},
        )
    if not force_refresh and not _is_token_expiring(
        token_set.expires_at or identity.get("expires_at")
    ):
        return identity, token_set

    lock = _refresh_lock_for(store, cred_ref)
    async with lock:
        # Re-read the latest stored credential after acquiring the lock so concurrent callers
        # reuse a token already rotated by another request instead of replaying a spent refresh token.
        latest_identities = await _store_call(store, "list_oauth_identities", user_id)
        latest_identity = (
            next(
                (
                    item
                    for item in latest_identities
                    if str(item.get("id") or "") == str(identity.get("id") or "")
                    and not item.get("revoked_at")
                ),
                None,
            )
            or identity
        )
        latest_ref = str(latest_identity.get("credential_ref") or cred_ref).strip()
        latest_token = await cred_store.load_token_set(
            latest_ref,
            expected_user_id=user_id,
            expected_provider=key,
        )
        if latest_token is None:
            raise HTTPException(
                409,
                {
                    "code": f"{key}_authorization_required",
                    "error": f"{label} 授权已失效，请重新连接",
                },
            )
        if not force_refresh and not _is_token_expiring(
            latest_token.expires_at or latest_identity.get("expires_at")
        ):
            return latest_identity, latest_token
        if force_refresh and latest_token.access_token != token_set.access_token:
            return latest_identity, latest_token

        provider_adapter = adapter or get_provider_adapter(settings, key)
        try:
            refreshed = await provider_adapter.refresh_access_token(latest_token.refresh_token)
        except GitSourceError as exc:
            if hasattr(store, "update_oauth_identity_rotation"):
                await _store_call(
                    store,
                    "update_oauth_identity_rotation",
                    latest_identity["id"],
                    rotation_status="refresh_failed",
                )
            raise HTTPException(exc.status_code, {"code": exc.code, "error": exc.message}) from exc

        rotated = OAuthTokenSet(
            provider=key,
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token or latest_token.refresh_token,
            scope=refreshed.scope or latest_token.scope,
            expires_at=refreshed.expires_at,
            provider_user_id=latest_token.provider_user_id
            or str(latest_identity.get("provider_user_id") or ""),
            provider_login=latest_token.provider_login
            or str(latest_identity.get("provider_login") or ""),
        )
        new_ref = await cred_store.save_token_set(
            user_id=user_id,
            token_set=rotated,
            credential_ref=latest_ref,
        )
        if hasattr(store, "update_oauth_identity_rotation"):
            await _store_call(
                store,
                "update_oauth_identity_rotation",
                latest_identity["id"],
                credential_ref=new_ref,
                expires_at=rotated.expires_at,
                rotation_status="rotated",
                token_refreshed_at=utc_now_iso(),
            )
        updated_identity = {
            **latest_identity,
            "credential_ref": new_ref,
            "expires_at": rotated.expires_at,
            "rotation_status": "rotated",
        }
        return updated_identity, rotated
