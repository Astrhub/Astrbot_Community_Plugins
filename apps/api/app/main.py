from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import inspect
import json
import re
import smtplib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import httpx
import asyncpg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .auth import (
    Role,
    can_edit_plugin,
    can_manage_admins,
    can_manage_plugin_submission,
    can_moderate_community,
    can_moderate_plugins,
    can_publish_announcement,
    hash_password,
    is_admin,
    is_core_admin,
    normalize_role,
    require_api_key,
    verify_password,
)
from .config import ApiKey, Settings, load_settings
from .runtime_config import read_runtime_config, write_runtime_config
from .schemas import (
    AnnouncementCreate,
    ApiKeyCreate,
    CommentCreate,
    InternalLoginPayload,
    MuteUserPayload,
    PluginGithubRefreshPayload,
    PluginPatch,
    PluginSubmission,
    PluginUnlistPayload,
    RoleUpdatePayload,
    SetupConfig,
    SystemSettingsPayload,
    TestEmailPayload,
    UserProfileUpdate,
)
from .store import InMemoryMarketStore
from .store import PgRedisMarketStore

GITHUB_REPO_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
PLUGIN_NAME_PATTERN = re.compile(r"^astrbot_plugin_[a-z0-9_-]+$", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
METADATA_FIELD_PATTERN = re.compile(
    r"^(\s*)(name|display_name|desc|short_desc|author|social_link|tags|version|astrbot_version|category|download_url|support_platforms)\s*:\s*(.*)$"
)
MASKED_SECRET = "********"
CLOUDFLARE_EMAIL_SEND_ENDPOINT = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/email/sending/send"
)
MARKET_WEB_DIST = Path(__file__).resolve().parents[3] / "apps" / "market-web" / "dist"
DEFAULT_POSTGRES_CONFIG = {
    "host": "127.0.0.1",
    "port": 5432,
    "database": "",
    "username": "",
    "password": "",
    "ssl": False,
}
DEFAULT_REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "database": 0,
    "password": "",
    "ssl": False,
}
RESERVED_WEB_PATHS = {
    "v1",
    "health",
    "plugins.json",
    "plugins-md5.json",
    "openapi.json",
    "docs",
    "redoc",
}
RESERVED_WEB_PREFIXES = ("v1/", "health/", "plugins.json/", "plugins-md5.json/", "docs/", "redoc/")
POSTGRES_MAINTENANCE_DATABASE = "postgres"
GITHUB_METADATA_SYNC_BATCH_SIZE = 10
GITHUB_METADATA_SYNC_WORKER_SLEEP_SECONDS = 60
GITHUB_RATE_LIMIT_MESSAGE = (
    "GitHub API rate limit reached. Provide a read-only GitHub token and try again."
)
PLUGIN_METADATA_SYNC_FIELDS = (
    "name",
    "display_name",
    "desc",
    "short_desc",
    "author",
    "social_link",
    "tags",
    "version",
    "astrbot_version",
    "category",
    "download_url",
    "support_platforms",
)


def create_app(
    settings: Settings | None = None,
    store: InMemoryMarketStore | None = None,
) -> FastAPI:
    app = FastAPI(
        title="AstrBot Community Plugins API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings or load_settings()
    app.state.store = store or create_store(app.state.settings)
    app.state.email_daily_counter = {"date": "", "count": 0}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app.state.settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["content-type", "authorization", "x-dev-github-login"],
    )
    app.add_exception_handler(HTTPException, http_exception_handler)
    register_routes(app)
    register_market_web_routes(app)
    return app


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


def create_store(settings: Settings) -> InMemoryMarketStore | PgRedisMarketStore:
    if settings.database_url and settings.redis_url:
        return PgRedisMarketStore(
            settings.database_url,
            settings.redis_url,
            settings.session_max_age_seconds,
        )
    return InMemoryMarketStore()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await maybe_call_store_lifecycle(app, "connect")
    await bootstrap_internal_core_admin(app)
    sync_task = asyncio.create_task(github_metadata_sync_worker(app))
    try:
        yield None
    finally:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            # Expected during application shutdown after cancelling the sync worker.
            pass
        await maybe_call_store_lifecycle(app, "close")


async def maybe_call_store_lifecycle(app: FastAPI, method_name: str) -> None:
    method = getattr(app.state.store, method_name, None)
    if not method:
        return
    await resolve_optional_awaitable(method())


async def resolve_optional_awaitable(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def bootstrap_internal_core_admin(app: FastAPI) -> None:
    settings = app.state.settings
    if not settings.core_admin_username or not settings.core_admin_password_hash:
        return
    await resolve_optional_awaitable(
        app.state.store.create_internal_admin(
            settings.core_admin_username,
            settings.core_admin_password_hash,
        )
    )


def register_routes(app: FastAPI) -> None:
    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        settings = get_settings(request)
        return {
            "status": "ok",
            "setup": "required" if settings.is_setup_required() else "complete",
            "database": "configured" if settings.database_url else "missing",
            "redis": "configured" if settings.redis_url else "missing",
        }

    @app.get("/v1/site")
    async def site_config(request: Request) -> dict[str, Any]:
        settings = get_settings(request)
        runtime_config = read_runtime_config(settings.runtime_config_path)
        return {
            **get_site_config(settings, runtime_config),
            "auth": get_public_auth_config(settings, runtime_config),
            "market": get_public_market_config(settings, runtime_config),
        }

    @app.get("/v1/admin/settings")
    async def admin_settings(request: Request) -> dict[str, Any]:
        user = await require_user(request)
        if not is_core_admin(user):
            raise error(403, "Only core admin can manage system settings")
        settings = get_settings(request)
        runtime_config = read_runtime_config(settings.runtime_config_path)
        return build_system_settings(settings, runtime_config, include_secrets=False)

    @app.put("/v1/admin/settings")
    async def update_admin_settings(
        request: Request,
        payload: SystemSettingsPayload,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not is_core_admin(user):
            raise error(403, "Only core admin can manage system settings")
        settings = get_settings(request)
        runtime_config = read_runtime_config(settings.runtime_config_path)
        validate_system_settings_payload(payload, runtime_config, settings)
        write_runtime_config(
            settings.runtime_config_path,
            runtime_values_from_system_settings(payload, runtime_config),
        )
        updated_runtime_config = read_runtime_config(settings.runtime_config_path)
        updated = build_system_settings(settings, updated_runtime_config, include_secrets=False)
        request.app.state.settings = settings_from_system_settings(
            settings,
            payload,
            runtime_config,
        )
        return {
            "saved": True,
            "restart_required": settings_restart_required(settings, updated_runtime_config),
            "settings": updated,
        }

    @app.post("/v1/admin/settings/email/test")
    async def send_test_email(request: Request, payload: TestEmailPayload) -> dict[str, bool]:
        user = await require_user(request)
        if not is_core_admin(user):
            raise error(403, "Only core admin can test email settings")
        if not is_valid_email(payload.to):
            raise error(400, "Invalid recipient email")
        settings = get_settings(request)
        await send_email(request.app, settings, payload.to, payload.subject, payload.body)
        return {"sent": True}

    @app.get("/v1/setup/status")
    async def setup_status(request: Request) -> dict[str, Any]:
        settings = get_settings(request)
        runtime_config = read_runtime_config(settings.runtime_config_path)
        runtime_database_url = runtime_config.get("DATABASE_URL", "")
        runtime_redis_url = runtime_config.get("REDIS_URL", "")
        saved_setup = build_saved_setup_config(settings, runtime_config)
        user = await current_user(request)
        can_view_secrets = bool(user) and is_core_admin(user)
        public_setup = saved_setup if can_view_secrets else redact_setup_infrastructure(saved_setup)
        return {
            "required": settings.is_setup_required(),
            "missing": list(settings.missing_setup_fields()),
            "database_configured": bool(settings.database_url or runtime_database_url),
            "redis_configured": bool(settings.redis_url or runtime_redis_url),
            "site": saved_setup["site"],
            "saved_setup": public_setup,
            "restart_required": settings_restart_required(settings, runtime_config),
        }

    @app.post("/v1/setup")
    async def save_setup(
        request: Request,
        payload: SetupConfig,
    ) -> dict[str, Any]:
        settings = get_settings(request)
        runtime_config = read_runtime_config(settings.runtime_config_path)
        if not can_save_setup_without_auth(settings, runtime_config):
            user = await require_user(request)
            if not is_core_admin(user):
                raise error(403, "Only core admin can update infrastructure settings")
        validate_setup_payload(payload)
        database_url = build_postgres_url(payload.postgres.model_dump())
        redis_url = build_redis_url(payload.redis.model_dump())
        core_admin_password_hash = hash_password(payload.admin.password)
        initializer = getattr(
            request.app.state,
            "setup_initializer",
            initialize_setup_infrastructure,
        )
        new_store = await resolve_optional_awaitable(
            initializer(payload, database_url, redis_url, core_admin_password_hash)
        )
        try:
            write_runtime_config(
                settings.runtime_config_path,
                {
                    "CORE_ADMIN_PASSWORD_HASH": core_admin_password_hash,
                    "CORE_ADMIN_USERNAME": payload.admin.username,
                    "DATABASE_URL": database_url,
                    "POSTGRES_DATABASE": payload.postgres.database,
                    "POSTGRES_HOST": payload.postgres.host,
                    "POSTGRES_PASSWORD": payload.postgres.password,
                    "POSTGRES_PORT": str(payload.postgres.port),
                    "POSTGRES_SSL": serialize_bool(payload.postgres.ssl),
                    "POSTGRES_USER": payload.postgres.username,
                    "REDIS_DATABASE": str(payload.redis.database),
                    "REDIS_HOST": payload.redis.host,
                    "REDIS_PASSWORD": payload.redis.password,
                    "REDIS_PORT": str(payload.redis.port),
                    "REDIS_SSL": serialize_bool(payload.redis.ssl),
                    "REDIS_URL": redis_url,
                    "SITE_ICON_URL": payload.site.icon_url,
                    "SITE_NAME": payload.site.name,
                },
            )
        except Exception:
            await close_setup_store(new_store)
            raise
        request.app.state.settings = settings.with_updates(
            core_admin_password_hash=core_admin_password_hash,
            core_admin_username=payload.admin.username,
            database_url=database_url,
            redis_url=redis_url,
            site_icon_url=payload.site.icon_url,
            site_name=payload.site.name,
        )
        await activate_setup_store(request.app, new_store)
        return {
            "saved": True,
            "restart_required": False,
            "activated": True,
            "message": "Configuration saved and PostgreSQL/Redis storage is active.",
        }

    @app.get("/v1/me")
    async def me(request: Request) -> dict[str, Any]:
        return public_user(await require_user(request))

    @app.patch("/v1/me/profile")
    async def update_my_profile(request: Request, payload: UserProfileUpdate) -> dict[str, Any]:
        user = await require_user(request)
        profile = {key: value for key, value in payload.model_dump().items() if value is not None}
        if not profile:
            raise error(400, "No fields to update")
        if (
            "avatar_url" in profile
            and profile["avatar_url"]
            and not is_valid_public_url(profile["avatar_url"])
        ):
            raise error(400, "Avatar URL must be http(s)")
        updated = await call_store(request, "update_user_profile", user["id"], profile)
        if not updated:
            raise error(404, "User not found")
        return public_user(updated)

    @app.get("/v1/me/notifications")
    async def my_notifications(request: Request) -> dict[str, list[dict[str, Any]]]:
        user = await require_user(request)
        return {"items": await call_store(request, "list_notifications", user["id"])}

    @app.post("/v1/auth/internal/login")
    async def internal_login(request: Request, payload: InternalLoginPayload) -> Response:
        settings = get_settings(request)
        user = await call_store(request, "get_user_by_internal_username", payload.username)
        if not user or not verify_password(payload.password, user.get("password_hash", "")):
            raise error(401, "Invalid username or password")
        if not settings.public_login_enabled and not is_core_admin(user):
            raise error(403, "Login is closed")
        session = await call_store(request, "create_session", user["id"])
        response = JSONResponse({"user": public_user(user), "session": session})
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        return response

    @app.get("/v1/auth/github/login")
    async def github_login(request: Request) -> Response:
        settings = get_runtime_settings(request)
        if not settings.public_login_enabled or not settings.github_login_enabled:
            return JSONResponse(status_code=403, content={"error": "GitHub login is disabled"})
        if not settings.github_client_id:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "GitHub OAuth is not configured",
                    "next": "Set GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET and GITHUB_CALLBACK_URL",
                },
            )

        state = str(uuid.uuid4())
        params = urlencode(
            {
                "client_id": settings.github_client_id,
                "redirect_uri": github_callback_url_for_request(request, settings),
                "scope": settings.github_scope,
                "state": state,
            }
        )
        response = RedirectResponse(f"https://github.com/login/oauth/authorize?{params}")
        set_cookie(response, settings.oauth_state_cookie_name, state, settings, max_age=600)
        return response

    @app.get("/v1/auth/github/callback")
    async def github_callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> Response:
        settings = get_runtime_settings(request)
        expected_state = request.cookies.get(settings.oauth_state_cookie_name)
        if not code or not state or not expected_state or state != expected_state:
            raise error(400, "Invalid OAuth callback")

        access_token = await exchange_github_code(settings, code)
        profile = await fetch_github_profile(access_token)
        current = await current_user(request)
        profile_payload = github_profile_payload(profile)
        if current:
            user = await link_github_profile_to_user(request, current, profile_payload)
        else:
            user = await call_store(request, "upsert_github_user", profile_payload)
        await promote_org_admin_if_needed(request, user, access_token)
        session = await call_store(request, "create_session", user["id"])
        response = RedirectResponse(settings.web_url)
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        response.delete_cookie(settings.oauth_state_cookie_name, path="/")
        return response

    @app.post("/v1/auth/logout", status_code=204)
    async def logout(request: Request, response: Response) -> None:
        settings = get_settings(request)
        session_token = request.cookies.get(settings.session_cookie_name)
        if session_token:
            await call_store(request, "revoke_session", session_token)
        response.delete_cookie(settings.session_cookie_name, path="/")

    @app.get("/v1/auth/debug-login")
    async def debug_login(request: Request, login: str = "") -> Response:
        settings = get_settings(request)
        if not settings.enable_dev_auth:
            raise error(403, "Dev auth is disabled")
        if not login.strip():
            raise error(400, "login is required")
        user = await call_store(
            request, "upsert_github_user", {"login": login.strip(), "name": login.strip()}
        )
        session = await call_store(request, "create_session", user["id"])
        response = JSONResponse({"user": public_user(user), "session": session})
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        return response

    @app.get("/v1/auth/session")
    async def auth_session(request: Request) -> dict[str, Any]:
        return {"authenticated": True, "user": public_user(await require_user(request))}

    @app.get("/v1/admin/check")
    async def admin_check(request: Request) -> dict[str, bool]:
        user = await require_user(request)
        return {
            "core_admin": is_core_admin(user),
            "admin": is_admin(user),
            "can_moderate_plugins": can_moderate_plugins(user),
            "can_moderate_community": can_moderate_community(user),
            "can_manage_admins": can_manage_admins(user),
        }

    @app.get("/v1/permissions")
    async def permissions(request: Request) -> dict[str, bool]:
        user = await require_user(request)
        return {
            "can_edit_any_plugin": is_admin(user),
            "can_moderate_plugins": can_moderate_plugins(user),
            "can_moderate_community": can_moderate_community(user),
            "can_publish_announcement": can_publish_announcement(user),
            "can_manage_admins": can_manage_admins(user),
        }

    @app.get("/v1/plugins")
    async def list_plugins(request: Request) -> dict[str, list[dict[str, Any]]]:
        return {"items": await call_store(request, "list_public_plugins")}

    @app.get("/plugins.json")
    async def astrbot_plugin_source(request: Request) -> dict[str, dict[str, Any]]:
        return build_astrbot_plugin_source(await call_store(request, "list_public_plugins"))

    @app.get("/plugins-md5.json")
    async def astrbot_plugin_source_md5(request: Request) -> dict[str, str]:
        feed = build_astrbot_plugin_source(await call_store(request, "list_public_plugins"))
        return {"md5": digest_plugin_source(feed)}

    @app.get("/v1/astrbot/plugins")
    @app.get("/v1/astrbot/plugins.json")
    async def astrbot_plugin_source_v1(request: Request) -> dict[str, dict[str, Any]]:
        return build_astrbot_plugin_source(await call_store(request, "list_public_plugins"))

    @app.get("/v1/astrbot/plugins-md5.json")
    async def astrbot_plugin_source_v1_md5(request: Request) -> dict[str, str]:
        feed = build_astrbot_plugin_source(await call_store(request, "list_public_plugins"))
        return {"md5": digest_plugin_source(feed)}

    @app.get("/v1/plugins/submissions")
    async def list_submissions(request: Request) -> dict[str, list[dict[str, Any]]]:
        user = await require_user(request)
        if not is_admin(user):
            raise error(403, "Forbidden")
        return {"items": await call_store(request, "list_submissions")}

    @app.post("/v1/plugins/submissions", status_code=201)
    async def submit_plugin(request: Request, payload: PluginSubmission) -> dict[str, Any]:
        user = await require_user(request)
        settings = get_settings(request)
        if not settings.market_submissions_enabled:
            raise error(403, "Plugin submissions are closed")
        data = payload.model_dump()
        validate_plugin_submission(data, settings)
        validate_repo_owner(data["repo"], user)
        data.update(await safe_fetch_plugin_github_metadata(data["repo"], settings, user))
        plugin = await call_store(request, "submit_plugin", user, data)
        if settings.plugin_auto_approve_enabled:
            listed = await call_store(
                request, "update_plugin_status", plugin["id"], "listed", user["id"]
            )
            return listed or plugin
        return plugin

    @app.get("/v1/plugins/{plugin_id}")
    async def plugin_detail(request: Request, plugin_id: str) -> dict[str, Any]:
        plugin = await get_plugin_or_404(request, plugin_id)
        user = await current_user(request)
        return await plugin_with_interaction_state(request, plugin, user)

    @app.patch("/v1/plugins/{plugin_id}")
    async def update_plugin(
        request: Request, plugin_id: str, payload: PluginPatch
    ) -> dict[str, Any]:
        user = await require_user(request)
        plugin = await get_plugin_or_404(request, plugin_id)
        if not can_edit_plugin(user, plugin):
            raise error(403, "Forbidden")
        patch = payload.model_dump(exclude_unset=True)
        if "name" in patch:
            validate_plugin_name(patch["name"])
        if "repo" in patch:
            validate_github_repo(patch["repo"])
            validate_repo_owner(patch["repo"], user)
            patch.update(
                await safe_fetch_plugin_github_metadata(patch["repo"], get_settings(request), user)
            )
        if "tags" in patch:
            validate_plugin_tag_count(patch.get("tags") or [], get_settings(request))
        updated = await call_store(
            request,
            "update_plugin_metadata",
            plugin_id,
            {
                **patch,
                "owner_user_id": plugin["owner_user_id"],
                "owner_github_login": plugin["owner_github_login"],
            },
        )
        return updated or {}

    @app.post("/v1/plugins/{plugin_id}/refresh-github")
    async def refresh_own_plugin_github_metadata(
        request: Request,
        plugin_id: str,
        payload: PluginGithubRefreshPayload | None = None,
    ) -> dict[str, Any]:
        user = await require_user(request)
        plugin = await get_plugin_or_404(request, plugin_id)
        if not can_edit_plugin(user, plugin):
            raise error(403, "Forbidden")
        refresh_payload = payload or PluginGithubRefreshPayload()
        updated = await refresh_plugin_github_metadata(
            request,
            plugin_id,
            user,
            token=refresh_payload.github_token,
            save_token=refresh_payload.save_token,
            refresh_interval_seconds=refresh_payload.refresh_interval_seconds,
            raise_errors=True,
        )
        return updated or plugin

    @app.post("/v1/plugins/{plugin_id}/like")
    async def like_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        if not get_settings(request).market_likes_enabled:
            raise error(403, "Plugin likes are closed")
        user = await require_user(request)
        await get_plugin_or_404(request, plugin_id)
        plugin = await call_store(request, "like_plugin", plugin_id, user["id"])
        return await plugin_with_interaction_state(request, plugin, user)

    @app.post("/v1/plugins/{plugin_id}/unlike")
    async def unlike_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        if not get_settings(request).market_likes_enabled:
            raise error(403, "Plugin likes are closed")
        user = await require_user(request)
        await get_plugin_or_404(request, plugin_id)
        plugin = await call_store(request, "unlike_plugin", plugin_id, user["id"])
        return await plugin_with_interaction_state(request, plugin, user)

    @app.post("/v1/plugins/{plugin_id}/comments", status_code=201)
    async def add_comment(
        request: Request, plugin_id: str, payload: CommentCreate
    ) -> dict[str, Any]:
        if not get_settings(request).market_comments_enabled:
            raise error(403, "Plugin comments are closed")
        user = await require_user(request)
        await get_plugin_or_404(request, plugin_id)
        if not payload.body:
            raise error(400, "Comment body is required")
        muted_until = parse_iso_datetime(user.get("muted_until"))
        if muted_until and muted_until > datetime.now(UTC):
            raise error(403, "User is muted")
        return await call_store(
            request, "add_comment", plugin_id, user["id"], payload.body, payload.parent_id
        )

    @app.post("/v1/comments/{comment_id}/like")
    async def like_comment(request: Request, comment_id: str) -> dict[str, Any]:
        if not get_settings(request).market_likes_enabled:
            raise error(403, "Comment likes are closed")
        user = await require_user(request)
        comment = await call_store(request, "like_comment", comment_id, user["id"])
        if not comment:
            raise error(404, "Comment not found")
        return with_comment_permissions(comment, user, liked=True)

    @app.post("/v1/comments/{comment_id}/unlike")
    async def unlike_comment(request: Request, comment_id: str) -> dict[str, Any]:
        if not get_settings(request).market_likes_enabled:
            raise error(403, "Comment likes are closed")
        user = await require_user(request)
        comment = await call_store(request, "unlike_comment", comment_id, user["id"])
        if not comment:
            raise error(404, "Comment not found")
        return with_comment_permissions(comment, user, liked=False)

    @app.delete("/v1/comments/{comment_id}")
    async def delete_own_comment(request: Request, comment_id: str) -> dict[str, Any]:
        user = await require_user(request)
        return await delete_comment_by_user(request, comment_id, user)

    @app.post("/v1/plugins/{plugin_id}/reindex")
    async def reindex_plugin(request: Request, plugin_id: str) -> dict[str, bool]:
        user = await require_user(request)
        plugin = await get_plugin_or_404(request, plugin_id)
        if not can_manage_plugin_submission(user, plugin):
            raise error(403, "Forbidden")
        return {"ok": True}

    @app.get("/v1/admin/users")
    async def admin_users(request: Request) -> dict[str, list[dict[str, Any]]]:
        await require_admin(request)
        return {"items": await call_store(request, "list_users")}

    @app.get("/v1/admin/plugins")
    async def admin_plugins(request: Request) -> dict[str, list[dict[str, Any]]]:
        await require_admin(request)
        return {"items": await call_store(request, "list_plugins")}

    @app.get("/v1/admin/summary")
    async def admin_summary(request: Request) -> dict[str, Any]:
        user = await require_admin(request)
        summary = await call_store(request, "summary")
        return {**summary, "role": user["role"]}

    @app.post("/v1/admin/plugins/{plugin_id}/list")
    async def list_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise error(403, "Forbidden")
        plugin = await get_plugin_or_404(request, plugin_id)
        previous_status = plugin.get("status")
        await refresh_plugin_github_metadata(request, plugin_id, user)
        updated = await call_store(request, "update_plugin_status", plugin_id, "listed", user["id"])
        if not updated:
            raise error(404, "Plugin not found")
        if previous_status != "listed" and updated.get("owner_user_id"):
            plugin_name = updated.get("display_name") or updated.get("name") or plugin_id
            await call_store(
                request,
                "create_notification",
                updated["owner_user_id"],
                "插件已上架",
                f"{plugin_name} 已通过审核并上架。",
                "plugin_listed",
                {
                    "plugin_id": plugin_id,
                    "plugin_name": updated.get("name") or plugin_id,
                    "moderator_user_id": user["id"],
                },
            )
        return updated

    @app.post("/v1/admin/plugins/{plugin_id}/refresh-github")
    async def refresh_admin_plugin_github_metadata(
        request: Request,
        plugin_id: str,
        payload: PluginGithubRefreshPayload | None = None,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise error(403, "Forbidden")
        refresh_payload = payload or PluginGithubRefreshPayload()
        updated = await refresh_plugin_github_metadata(
            request,
            plugin_id,
            user,
            token=refresh_payload.github_token,
            save_token=refresh_payload.save_token,
            refresh_interval_seconds=refresh_payload.refresh_interval_seconds,
            raise_errors=True,
        )
        if not updated:
            raise error(404, "Plugin not found")
        return updated

    @app.post("/v1/admin/plugins/{plugin_id}/unlist")
    async def unlist_plugin(
        request: Request,
        plugin_id: str,
        payload: PluginUnlistPayload,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise error(403, "Forbidden")
        if not payload.reason:
            raise error(400, "Unlist reason is required")
        plugin = await get_plugin_or_404(request, plugin_id)
        updated = await call_store(request, "unlist_plugin", plugin_id, user["id"], payload.reason)
        if not updated:
            raise error(404, "Plugin not found")
        if plugin.get("owner_user_id"):
            plugin_name = plugin.get("display_name") or plugin.get("name") or plugin_id
            await call_store(
                request,
                "create_notification",
                plugin["owner_user_id"],
                "插件已下架",
                f"{plugin_name} 已被管理员下架。原因：{payload.reason}",
                "plugin_unlisted",
                {
                    "plugin_id": plugin_id,
                    "plugin_name": plugin.get("name") or plugin_id,
                    "reason": payload.reason,
                    "moderator_user_id": user["id"],
                },
            )
        return updated

    @app.delete("/v1/admin/comments/{comment_id}")
    async def delete_comment(request: Request, comment_id: str) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_community(user):
            raise error(403, "Forbidden")
        return await delete_comment_by_user(request, comment_id, user)

    @app.post("/v1/admin/users/{user_id}/mute")
    async def mute_user(request: Request, user_id: str, payload: MuteUserPayload) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_community(user):
            raise error(403, "Forbidden")
        muted_until = payload.muted_until or (datetime.now(UTC) + timedelta(days=1)).isoformat()
        muted = await call_store(request, "mute_user", user_id, muted_until, user["id"])
        if not muted:
            raise error(404, "User not found")
        return muted

    @app.post("/v1/core/admins/{user_id}")
    async def update_admin(
        request: Request, user_id: str, payload: RoleUpdatePayload
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_manage_admins(user):
            raise error(403, "Forbidden")
        target = await call_store(request, "get_user_by_id", user_id)
        if not target:
            raise error(404, "User not found")
        updated = await call_store(
            request, "update_user_role", user_id, "admin" if payload.role == "admin" else "user"
        )
        return updated or {}

    @app.post("/v1/core/announcements", status_code=201)
    async def create_announcement(request: Request, payload: AnnouncementCreate) -> dict[str, Any]:
        user = await require_user(request)
        if not can_publish_announcement(user):
            raise error(403, "Forbidden")
        if not payload.title or not payload.body:
            raise error(400, "Announcement title and body are required")
        return await call_store(
            request, "publish_announcement", payload.title, payload.body, user["id"]
        )

    @app.get("/v1/announcements")
    async def announcements(request: Request) -> dict[str, list[dict[str, Any]]]:
        return {"items": await call_store(request, "list_announcements")}

    @app.post("/v1/api-keys", status_code=201)
    async def issue_api_key(request: Request, payload: ApiKeyCreate) -> dict[str, Any]:
        user = await require_user(request)
        if not is_admin(user):
            raise error(403, "Forbidden")
        return await call_store(request, "issue_api_key", payload.name, user["id"], payload.scopes)

    @app.get("/v1/api-keys")
    async def api_keys(request: Request) -> dict[str, list[dict[str, Any]]]:
        keys = await all_api_keys(request)
        ok, status, message = require_api_key(
            request.headers.get("authorization"), keys, "market:read"
        )
        if not ok:
            raise error(status, message)
        return {"items": [public_api_key(key) for key in keys]}


def register_market_web_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def market_web_index() -> Response:
        return serve_market_web_file("")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def market_web_fallback(full_path: str) -> Response:
        if is_reserved_api_path(full_path):
            raise error(404, "Not found")
        return serve_market_web_file(full_path)


def serve_market_web_file(full_path: str) -> FileResponse:
    index_file = MARKET_WEB_DIST / "index.html"
    if not index_file.is_file():
        raise error(404, "Market web build is missing. Run npm run build:web first.")

    requested_file = resolve_market_web_file(full_path)
    return FileResponse(requested_file or index_file)


def resolve_market_web_file(full_path: str) -> Path | None:
    dist_dir = MARKET_WEB_DIST.resolve()
    candidate = (dist_dir / full_path).resolve()
    try:
        candidate.relative_to(dist_dir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def is_reserved_api_path(full_path: str) -> bool:
    path = full_path.strip("/")
    return path in RESERVED_WEB_PATHS or path.startswith(RESERVED_WEB_PREFIXES)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_runtime_settings(request: Request) -> Settings:
    settings = get_settings(request)
    runtime_config = read_runtime_config(settings.runtime_config_path)
    if not runtime_config:
        return settings
    return settings.with_updates(
        github_client_id=runtime_config.get("GITHUB_CLIENT_ID", settings.github_client_id),
        github_client_secret=runtime_config.get(
            "GITHUB_CLIENT_SECRET",
            settings.github_client_secret,
        ),
        github_callback_url=runtime_config.get(
            "GITHUB_CALLBACK_URL",
            settings.github_callback_url,
        ),
        github_scope=runtime_config.get("GITHUB_SCOPE", settings.github_scope),
        github_api_token=runtime_config.get("GITHUB_API_TOKEN", settings.github_api_token),
        github_metadata_sync_enabled=parse_bool(
            runtime_config.get("GITHUB_METADATA_SYNC_ENABLED"),
            settings.github_metadata_sync_enabled,
        ),
        github_metadata_sync_interval_seconds=clamp_sync_interval(
            runtime_config.get(
                "GITHUB_METADATA_SYNC_INTERVAL_SECONDS",
                str(settings.github_metadata_sync_interval_seconds),
            )
        ),
        github_login_enabled=parse_bool(
            runtime_config.get("GITHUB_LOGIN_ENABLED"),
            settings.github_login_enabled,
        ),
        public_login_enabled=parse_bool(
            runtime_config.get("PUBLIC_LOGIN_ENABLED"),
            settings.public_login_enabled,
        ),
    )


def github_callback_url_for_request(request: Request, settings: Settings) -> str:
    if not is_loopback_url(settings.github_callback_url):
        return settings.github_callback_url
    return f"{public_request_origin(request)}/v1/auth/github/callback"


def public_request_origin(request: Request) -> str:
    forwarded_proto = first_forwarded_header(request, "x-forwarded-proto")
    forwarded_host = first_forwarded_header(request, "x-forwarded-host")
    forwarded_port = first_forwarded_header(request, "x-forwarded-port")
    proto = forwarded_proto or request.url.scheme
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    if forwarded_port and ":" not in host:
        host = f"{host}:{forwarded_port}"
    return f"{proto}://{host}".rstrip("/")


def first_forwarded_header(request: Request, name: str) -> str:
    return request.headers.get(name, "").split(",", 1)[0].strip()


def is_loopback_url(value: str) -> bool:
    hostname = urlparse(value).hostname or ""
    return hostname == "localhost" or hostname == "::1" or hostname.startswith("127.")


def get_store(request: Request) -> InMemoryMarketStore | PgRedisMarketStore:
    return request.app.state.store


async def call_store(request: Request, method_name: str, *args: Any) -> Any:
    method = getattr(get_store(request), method_name)
    result = method(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def current_user(request: Request) -> dict[str, Any] | None:
    settings = get_settings(request)
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token:
        user = await call_store(request, "get_user_by_session", session_token)
        if user:
            return user

    if not settings.enable_dev_auth:
        return None
    dev_login = request.headers.get("x-dev-github-login", "").strip()
    if not dev_login:
        return None
    return await call_store(request, "upsert_github_user", {"login": dev_login, "name": dev_login})


async def require_user(request: Request) -> dict[str, Any]:
    user = await current_user(request)
    if not user:
        raise error(401, "Not authenticated")
    return user


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {**user, "has_github_token": bool(user.get("github_token"))}.items()
        if key not in {"password_hash", "github_token"}
    }


async def require_admin(request: Request) -> dict[str, Any]:
    user = await require_user(request)
    if not is_admin(user):
        raise error(403, "Forbidden")
    return user


async def get_plugin_or_404(request: Request, plugin_id: str) -> dict[str, Any]:
    plugin = await call_store(request, "get_plugin", plugin_id)
    if not plugin:
        raise error(404, "Plugin not found")
    return plugin


async def plugin_with_interaction_state(
    request: Request,
    plugin: dict[str, Any] | None,
    user: dict[str, Any] | None,
) -> dict[str, Any]:
    if not plugin:
        raise error(404, "Plugin not found")
    comments = await call_store(request, "list_comments", plugin["id"])
    liked_comment_ids = set()
    plugin_liked = False
    if user:
        plugin_liked = await call_store(request, "has_plugin_like", plugin["id"], user["id"])
        liked_comment_ids = set(
            await call_store(request, "list_liked_comment_ids", plugin["id"], user["id"])
        )
    return {
        **plugin,
        "liked": plugin_liked,
        "comments": [
            with_comment_permissions(
                comment,
                user,
                comment["id"] in liked_comment_ids,
                plugin,
                index + 1,
            )
            for index, comment in enumerate(comments)
        ],
    }


async def delete_comment_by_user(
    request: Request,
    comment_id: str,
    user: dict[str, Any],
) -> dict[str, Any]:
    comment = await call_store(request, "get_comment", comment_id)
    if not comment:
        raise error(404, "Comment not found")
    if comment.get("user_id") != user["id"] and not can_moderate_community(user):
        raise error(403, "Forbidden")
    deleted = await call_store(request, "delete_comment", comment_id, user["id"])
    if not deleted:
        raise error(404, "Comment not found")
    return deleted


def with_comment_permissions(
    comment: dict[str, Any],
    user: dict[str, Any] | None,
    liked: bool,
    plugin: dict[str, Any] | None = None,
    floor: int | None = None,
) -> dict[str, Any]:
    return {
        **comment,
        "liked": liked,
        "floor": floor,
        "is_admin": normalize_role(comment.get("role")) in {Role.CORE_ADMIN, Role.ADMIN},
        "is_plugin_author": is_plugin_author_comment(comment, plugin),
        "can_delete": bool(user)
        and (comment.get("user_id") == user.get("id") or can_moderate_community(user)),
    }


def is_plugin_author_comment(
    comment: dict[str, Any],
    plugin: dict[str, Any] | None,
) -> bool:
    if not plugin:
        return False
    if plugin.get("owner_user_id") and comment.get("user_id") == plugin.get("owner_user_id"):
        return True
    return bool(
        plugin.get("owner_github_login")
        and comment.get("github_login")
        and plugin["owner_github_login"] == comment["github_login"]
    )


def validate_plugin_submission(payload: dict[str, Any], settings: Settings | None = None) -> None:
    for field in ("name", "repo", "desc", "author"):
        if not payload.get(field):
            raise error(400, "Missing required plugin fields")
    validate_plugin_name(payload["name"])
    validate_github_repo(payload["repo"])
    validate_plugin_tag_count(payload.get("tags") or [], settings)


def validate_plugin_tag_count(tags: list[str], settings: Settings | None = None) -> None:
    if settings and settings.max_plugin_tags and len(tags) > settings.max_plugin_tags:
        raise error(400, f"Plugin can have at most {settings.max_plugin_tags} tags")


def validate_plugin_name(name: str) -> None:
    if not PLUGIN_NAME_PATTERN.match(name or ""):
        raise error(400, "Plugin name must use astrbot_plugin_ prefix")


def validate_github_repo(repo: str) -> re.Match[str]:
    match = GITHUB_REPO_PATTERN.match(repo or "")
    if not match:
        raise error(400, "Plugin repo must be a GitHub URL")
    return match


class GithubMetadataError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def github_metadata_sync_worker(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(GITHUB_METADATA_SYNC_WORKER_SLEEP_SECONDS)
        await sync_due_github_plugin_metadata_once(app, GITHUB_METADATA_SYNC_BATCH_SIZE)


async def sync_due_github_plugin_metadata_once(app: FastAPI, limit: int) -> int:
    settings = app.state.settings
    if not settings.github_metadata_sync_enabled:
        return 0
    plugins = await list_due_github_sync_plugins(app.state.store, limit)
    for plugin in plugins:
        try:
            owner = await get_plugin_owner_for_sync(app.state.store, plugin)
            await refresh_plugin_github_metadata_for_plugin(app, plugin, owner)
        except Exception as exc:
            await update_plugin_github_sync_failure(
                app.state.store,
                plugin,
                settings,
                safe_exception_message(exc),
            )
    return len(plugins)


async def list_due_github_sync_plugins(store: Any, limit: int) -> list[dict[str, Any]]:
    method = getattr(store, "list_due_github_sync_plugins", None)
    if method:
        return await resolve_optional_awaitable(method(limit))
    plugins = await resolve_optional_awaitable(store.list_public_plugins())
    due = [plugin for plugin in plugins if is_plugin_due_for_github_sync(plugin)]
    return due[:limit]


def is_plugin_due_for_github_sync(plugin: dict[str, Any]) -> bool:
    if plugin.get("status") != "listed":
        return False
    next_sync = parse_iso_datetime(plugin.get("github_next_sync_at"))
    return next_sync is None or next_sync <= datetime.now(UTC)


async def get_plugin_owner_for_sync(
    store: Any,
    plugin: dict[str, Any],
) -> dict[str, Any] | None:
    owner_id = plugin.get("owner_user_id")
    if not owner_id:
        return None
    method = getattr(store, "get_user_by_id", None)
    if not method:
        return None
    return await resolve_optional_awaitable(method(owner_id))


async def refresh_plugin_github_metadata(
    request: Request,
    plugin_id: str,
    user: dict[str, Any] | None = None,
    *,
    token: str = "",
    save_token: bool = False,
    refresh_interval_seconds: int | None = None,
    raise_errors: bool = False,
) -> dict[str, Any] | None:
    plugin = await call_store(request, "get_plugin", plugin_id)
    if not plugin:
        return None
    if user:
        user = await update_user_github_sync_preferences(
            request,
            user,
            token,
            save_token,
            refresh_interval_seconds,
        )
    return await refresh_plugin_github_metadata_for_plugin(
        request.app,
        plugin,
        user,
        token=token,
        raise_errors=raise_errors,
    )


async def update_user_github_sync_preferences(
    request: Request,
    user: dict[str, Any],
    token: str,
    save_token: bool,
    refresh_interval_seconds: int | None,
) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    if save_token and token:
        profile["github_token"] = token
    if refresh_interval_seconds is not None and (
        user.get("github_token") or profile.get("github_token")
    ):
        profile["github_refresh_interval_seconds"] = refresh_interval_seconds
    if not profile:
        return user
    return await call_store(request, "update_user_profile", user["id"], profile) or user


async def refresh_plugin_github_metadata_for_plugin(
    app: FastAPI,
    plugin: dict[str, Any],
    user: dict[str, Any] | None = None,
    *,
    token: str = "",
    raise_errors: bool = False,
) -> dict[str, Any] | None:
    settings = app.state.settings
    try:
        metadata = await fetch_plugin_github_metadata(
            plugin.get("repo") or "",
            settings,
            user,
            token=token,
        )
    except GithubMetadataError as exc:
        await update_plugin_github_sync_failure(app.state.store, plugin, settings, exc.message)
        if raise_errors:
            raise error(exc.status_code, exc.message) from exc
        return plugin
    if not metadata:
        return plugin
    metadata.update(github_sync_success_metadata(settings, user))
    return await resolve_optional_awaitable(
        app.state.store.update_plugin_metadata(plugin["id"], metadata)
    )


async def safe_fetch_plugin_github_metadata(
    repo: str,
    settings: Settings,
    user: dict[str, Any] | None = None,
    *,
    token: str = "",
) -> dict[str, Any]:
    try:
        return await fetch_plugin_github_metadata(repo, settings, user, token=token)
    except GithubMetadataError:
        return {}


async def fetch_plugin_github_metadata(
    repo: str,
    settings: Settings,
    user: dict[str, Any] | None = None,
    *,
    token: str = "",
) -> dict[str, Any]:
    if not settings.github_metadata_sync_enabled:
        return {}
    match = validate_github_repo(repo)
    owner = match.group("owner")
    repo_name = match.group("repo")
    headers = github_api_headers(user, settings, token)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            repository = await fetch_github_repository(client, owner, repo_name, headers)
            if not repository:
                return {}
            metadata = await fetch_github_plugin_metadata_files(client, owner, repo_name, headers)
            logo = await fetch_github_plugin_logo_url(
                client,
                owner,
                repo_name,
                repository.get("default_branch") or "main",
                headers,
            )
    except httpx.HTTPError as exc:
        raise GithubMetadataError("GitHub metadata fetch failed", 502) from exc
    payload: dict[str, Any] = {
        "stars": int(repository.get("stargazers_count") or 0),
        "updated_at": repository.get("updated_at") or "",
    }
    for field in PLUGIN_METADATA_SYNC_FIELDS:
        value = normalize_plugin_metadata_field(field, metadata.get(field))
        if has_metadata_value(value):
            payload[field] = value
    if logo:
        payload["logo"] = logo
    return payload


async def fetch_github_repository(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = await client.get(
        f"https://api.github.com/repos/{owner}/{repo}",
        headers=headers,
    )
    raise_for_github_rate_limit(response)
    if response.status_code != 200:
        return {}
    data = response.json()
    return data if isinstance(data, dict) else {}


async def fetch_github_plugin_metadata_files(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    for filename in ("metadata.yml", "metadata.yaml"):
        response = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{filename}",
            headers=headers,
        )
        raise_for_github_rate_limit(response)
        if response.status_code != 200:
            continue
        data = response.json()
        content = data.get("content") if isinstance(data, dict) else ""
        if not isinstance(content, str) or not content:
            continue
        try:
            text = base64.b64decode(content).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            continue
        metadata = parse_plugin_metadata_yaml(text)
        if metadata:
            return metadata
    return {}


async def fetch_github_plugin_logo_url(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    default_branch: str,
    headers: dict[str, str],
) -> str:
    response = await client.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/logo.png",
        headers=headers,
    )
    raise_for_github_rate_limit(response)
    if response.status_code == 200:
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/logo.png"
    return ""


def raise_for_github_rate_limit(response: Any) -> None:
    if response.status_code not in {403, 429}:
        return
    headers = getattr(response, "headers", {}) or {}
    message = github_response_message(response).lower()
    if headers.get("x-ratelimit-remaining") == "0" or "rate limit" in message:
        raise GithubMetadataError(GITHUB_RATE_LIMIT_MESSAGE, 429)


def github_response_message(response: Any) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("message") or "")
    return ""


def normalize_plugin_metadata_field(field: str, value: Any) -> Any:
    if field == "name" and value and not PLUGIN_NAME_PATTERN.match(str(value)):
        return ""
    if field in {"tags", "support_platforms"}:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []
    return value


def github_sync_success_metadata(
    settings: Settings,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    interval = github_sync_interval_seconds(settings, user)
    return {
        "github_synced_at": isoformat_utc(now),
        "github_next_sync_at": isoformat_utc(now + timedelta(seconds=interval)),
        "github_refresh_interval_seconds": interval,
        "github_sync_status": "success",
        "github_sync_error": "",
    }


async def update_plugin_github_sync_failure(
    store: Any,
    plugin: dict[str, Any],
    settings: Settings,
    message: str,
) -> None:
    now = datetime.now(UTC)
    interval = github_sync_interval_seconds(settings)
    await resolve_optional_awaitable(
        store.update_plugin_metadata(
            plugin["id"],
            {
                "github_synced_at": isoformat_utc(now),
                "github_next_sync_at": isoformat_utc(now + timedelta(seconds=interval)),
                "github_sync_status": "error",
                "github_sync_error": message,
            },
        )
    )


def github_sync_interval_seconds(
    settings: Settings,
    user: dict[str, Any] | None = None,
) -> int:
    if user and user.get("github_token"):
        return clamp_sync_interval(user.get("github_refresh_interval_seconds"))
    return clamp_sync_interval(settings.github_metadata_sync_interval_seconds)


def clamp_sync_interval(value: Any) -> int:
    try:
        seconds = int(value or 3600)
    except (TypeError, ValueError):
        seconds = 3600
    return min(max(seconds, 300), 86400)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_plugin_metadata_yaml(text: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = METADATA_FIELD_PATTERN.match(line)
        if not match:
            index += 1
            continue
        indent, key, raw_value = match.groups()
        value = parse_metadata_scalar(raw_value)
        if value == "" and key in {"support_platforms", "tags"}:
            value, index = parse_metadata_list(lines, index, len(indent))
        if has_metadata_value(value):
            metadata[key] = value
        index += 1
    return metadata


def parse_metadata_list(
    lines: list[str],
    start_index: int,
    parent_indent: int,
) -> tuple[list[str] | str, int]:
    items: list[str] = []
    index = start_index + 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= parent_indent:
            return items or "", index - 1
        stripped = line.strip()
        if not stripped.startswith("- "):
            return items or "", index - 1
        item = parse_metadata_scalar(stripped[2:])
        if item:
            items.append(str(item))
        index += 1
    return items or "", index - 1


def parse_metadata_scalar(value: str) -> Any:
    value = strip_yaml_comment(value).strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        items = [parse_metadata_scalar(item) for item in value[1:-1].split(",")]
        return [item for item in items if item]
    return value


def strip_yaml_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return value[:index].rstrip()
    return value.strip()


def has_metadata_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return value is not None


def validate_setup_payload(payload: SetupConfig) -> None:
    if not payload.admin.username:
        raise error(400, "Core admin username is required")
    if len(payload.admin.password) < 8:
        raise error(400, "Core admin password must be at least 8 characters")
    if not payload.postgres.host:
        raise error(400, "PostgreSQL host is required")
    if not payload.postgres.database:
        raise error(400, "PostgreSQL database is required")
    if not payload.postgres.username:
        raise error(400, "PostgreSQL username is required")
    if not payload.postgres.password:
        raise error(400, "PostgreSQL password is required")
    if not payload.redis.host:
        raise error(400, "Redis host is required")
    if not payload.site.name:
        raise error(400, "Site name is required")
    if not payload.site.icon_url:
        raise error(400, "Site icon URL is required")
    if not is_valid_site_icon_url(payload.site.icon_url):
        raise error(400, "Site icon URL must be an absolute URL or root-relative path")


async def initialize_setup_infrastructure(
    payload: SetupConfig,
    database_url: str,
    redis_url: str,
    core_admin_password_hash: str,
) -> PgRedisMarketStore:
    await ensure_postgres_database(payload.postgres.model_dump())
    store = PgRedisMarketStore(database_url, redis_url, session_ttl_seconds=60)
    try:
        await store.connect()
        await store.create_internal_admin(payload.admin.username, core_admin_password_hash)
        return store
    except HTTPException:
        await store.close()
        raise
    except asyncpg.PostgresError as exc:
        await store.close()
        raise error(
            400,
            f"PostgreSQL schema initialization failed: {safe_exception_message(exc)}",
        ) from exc
    except OSError as exc:
        await store.close()
        raise error(
            400, f"Infrastructure connection failed: {safe_exception_message(exc)}"
        ) from exc
    except Exception as exc:
        await store.close()
        raise error(
            400,
            f"PostgreSQL or Redis initialization failed: {safe_exception_message(exc)}",
        ) from exc


async def activate_setup_store(app: FastAPI, new_store: Any) -> None:
    if not new_store:
        await bootstrap_internal_core_admin(app)
        return

    old_store = app.state.store
    app.state.store = new_store
    if old_store is new_store:
        return
    close = getattr(old_store, "close", None)
    if close:
        await resolve_optional_awaitable(close())


async def close_setup_store(store: Any) -> None:
    close = getattr(store, "close", None)
    if close:
        await resolve_optional_awaitable(close())


async def ensure_postgres_database(config: dict[str, Any]) -> None:
    target_database = config["database"]
    try:
        connection = await connect_postgres_database(config, target_database)
    except asyncpg.InvalidCatalogNameError:
        await create_postgres_database(config, target_database)
        return
    except asyncpg.PostgresError as exc:
        raise error(
            400,
            f"PostgreSQL connection failed: {safe_exception_message(exc)}",
        ) from exc
    except OSError as exc:
        raise error(
            400,
            f"PostgreSQL connection failed: {safe_exception_message(exc)}",
        ) from exc
    else:
        await connection.close()


async def create_postgres_database(config: dict[str, Any], target_database: str) -> None:
    try:
        connection = await connect_postgres_database(config, POSTGRES_MAINTENANCE_DATABASE)
    except asyncpg.InvalidCatalogNameError as exc:
        raise error(
            400,
            "PostgreSQL maintenance database 'postgres' is unavailable; "
            "create the target database manually first.",
        ) from exc
    except asyncpg.PostgresError as exc:
        raise error(
            400,
            f"PostgreSQL database creation failed: {safe_exception_message(exc)}",
        ) from exc
    except OSError as exc:
        raise error(
            400,
            f"PostgreSQL database creation failed: {safe_exception_message(exc)}",
        ) from exc

    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            target_database,
        )
        if not exists:
            await connection.execute(
                f"CREATE DATABASE {quote_postgres_identifier(target_database)}"
            )
    except asyncpg.DuplicateDatabaseError:
        return
    except asyncpg.PostgresError as exc:
        raise error(
            400,
            f"PostgreSQL database creation failed: {safe_exception_message(exc)}",
        ) from exc
    finally:
        await connection.close()


async def connect_postgres_database(
    config: dict[str, Any],
    database: str,
) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=config["host"],
        port=config["port"],
        user=config["username"],
        password=config["password"],
        database=database,
        ssl=config["ssl"] or None,
    )


def quote_postgres_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def safe_exception_message(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__


def validate_system_settings_payload(
    payload: SystemSettingsPayload,
    runtime_config: dict[str, str],
    settings: Settings | None = None,
) -> None:
    if not payload.site.name:
        raise error(400, "Site name is required")
    if not payload.site.icon_url or not is_valid_site_icon_url(payload.site.icon_url):
        raise error(400, "Site icon URL must be an absolute URL or root-relative path")
    if payload.site.docs_url and not is_valid_public_url(payload.site.docs_url):
        raise error(400, "Documentation URL must be http(s)")
    if payload.site.contact_email and not is_valid_email(payload.site.contact_email):
        raise error(400, "Contact email is invalid")
    if payload.auth.login_agreement_enabled and not payload.auth.login_agreement_text:
        raise error(400, "Login agreement text is required when enabled")
    if payload.auth.service_terms_enabled and not payload.auth.service_terms_text:
        raise error(400, "Service terms text is required when enabled")
    if payload.auth.github_login_enabled:
        if not payload.github.client_id:
            raise error(400, "GitHub OAuth client ID is required when GitHub login is enabled")
        existing_github_secret = runtime_config.get("GITHUB_CLIENT_SECRET") or (
            settings.github_client_secret if settings else ""
        )
        if not has_secret_value(payload.github.client_secret, existing_github_secret):
            raise error(400, "GitHub OAuth client secret is required when GitHub login is enabled")
        if not payload.github.callback_url or not is_valid_public_url(payload.github.callback_url):
            raise error(400, "GitHub callback URL must be http(s)")
    if payload.email.provider == "smtp":
        if not payload.email.smtp.host:
            raise error(400, "SMTP host is required when SMTP email is enabled")
        if not payload.email.smtp.from_address or not is_valid_email(
            payload.email.smtp.from_address
        ):
            raise error(400, "SMTP from address is invalid")
    if payload.email.provider == "cloudflare":
        if not payload.email.cloudflare.account_id:
            raise error(400, "Cloudflare account ID is required")
        existing_cloudflare_token = runtime_config.get("CLOUDFLARE_EMAIL_API_TOKEN") or (
            settings.cloudflare_email_api_token if settings else ""
        )
        if not has_secret_value(payload.email.cloudflare.api_token, existing_cloudflare_token):
            raise error(400, "Cloudflare API token is required")
        if not payload.email.cloudflare.from_address or not is_valid_email(
            payload.email.cloudflare.from_address
        ):
            raise error(400, "Cloudflare from address is invalid")


def get_site_config(
    settings: Settings,
    runtime_config: dict[str, str] | None = None,
) -> dict[str, str]:
    if runtime_config is not None:
        return build_site_settings(settings, runtime_config)
    return {
        "name": settings.site_name,
        "icon_url": settings.site_icon_url,
        "subtitle": settings.site_subtitle,
        "description": settings.site_description,
        "contact_email": settings.site_contact_email,
        "docs_url": settings.site_docs_url,
    }


def get_public_auth_config(
    settings: Settings,
    runtime_config: dict[str, str] | None = None,
) -> dict[str, Any]:
    auth = (
        build_auth_settings(settings, runtime_config)
        if runtime_config is not None
        else {
            "github_login_enabled": settings.github_login_enabled,
            "public_login_enabled": settings.public_login_enabled,
            "login_agreement_enabled": settings.login_agreement_enabled,
            "login_agreement_text": settings.login_agreement_text,
            "service_terms_enabled": settings.service_terms_enabled,
            "service_terms_text": settings.service_terms_text,
        }
    )
    effective_settings = settings.with_updates(
        login_agreement_enabled=auth["login_agreement_enabled"],
        login_agreement_text=auth["login_agreement_text"],
        service_terms_enabled=auth["service_terms_enabled"],
        service_terms_text=auth["service_terms_text"],
    )
    return {**auth, "terms_revision": digest_terms(effective_settings)}


def get_public_market_config(
    settings: Settings,
    runtime_config: dict[str, str] | None = None,
) -> dict[str, Any]:
    if runtime_config is not None:
        market = build_market_settings(settings, runtime_config)
        return {
            "submissions_enabled": market["submissions_enabled"],
            "comments_enabled": market["comments_enabled"],
            "likes_enabled": market["likes_enabled"],
            "max_plugin_tags": market["max_plugin_tags"],
        }
    return {
        "submissions_enabled": settings.market_submissions_enabled,
        "comments_enabled": settings.market_comments_enabled,
        "likes_enabled": settings.market_likes_enabled,
        "max_plugin_tags": settings.max_plugin_tags,
    }


def digest_terms(settings: Settings) -> str:
    payload = json.dumps(
        {
            "login": settings.login_agreement_text if settings.login_agreement_enabled else "",
            "service": settings.service_terms_text if settings.service_terms_enabled else "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_saved_setup_config(
    settings: Settings,
    runtime_config: dict[str, str],
) -> dict[str, Any]:
    database_url = runtime_config.get("DATABASE_URL") or settings.database_url
    redis_url = runtime_config.get("REDIS_URL") or settings.redis_url
    system_settings = build_system_settings(settings, runtime_config, include_secrets=False)
    return {
        "postgres": build_saved_postgres_config(runtime_config, database_url),
        "redis": build_saved_redis_config(runtime_config, redis_url),
        **system_settings,
    }


def redact_setup_infrastructure(config: dict[str, Any]) -> dict[str, Any]:
    redacted = {
        "postgres": {**DEFAULT_POSTGRES_CONFIG},
        "redis": {**DEFAULT_REDIS_CONFIG},
        "site": {**config["site"]},
        "auth": {**config["auth"]},
        "github": redact_github_settings(config.get("github", {})),
        "market": {**config["market"]},
        "email": redact_email_settings(config.get("email", {})),
    }
    return redacted


def build_system_settings(
    settings: Settings,
    runtime_config: dict[str, str],
    *,
    include_secrets: bool,
) -> dict[str, Any]:
    config = {
        "site": build_site_settings(settings, runtime_config),
        "auth": build_auth_settings(settings, runtime_config),
        "github": build_github_settings(settings, runtime_config),
        "market": build_market_settings(settings, runtime_config),
        "email": build_email_settings(settings, runtime_config),
    }
    if include_secrets:
        return config
    config["github"] = redact_github_settings(config["github"])
    config["email"] = redact_email_settings(config["email"])
    return config


def build_site_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, str]:
    return {
        "name": runtime_config.get("SITE_NAME", settings.site_name),
        "icon_url": runtime_config.get("SITE_ICON_URL", settings.site_icon_url),
        "subtitle": runtime_config.get("SITE_SUBTITLE", settings.site_subtitle),
        "description": runtime_config.get("SITE_DESCRIPTION", settings.site_description),
        "contact_email": runtime_config.get("SITE_CONTACT_EMAIL", settings.site_contact_email),
        "docs_url": runtime_config.get("SITE_DOCS_URL", settings.site_docs_url),
    }


def build_auth_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, Any]:
    return {
        "github_login_enabled": parse_bool(
            runtime_config.get("GITHUB_LOGIN_ENABLED"), settings.github_login_enabled
        ),
        "public_login_enabled": parse_bool(
            runtime_config.get("PUBLIC_LOGIN_ENABLED"), settings.public_login_enabled
        ),
        "login_agreement_enabled": parse_bool(
            runtime_config.get("LOGIN_AGREEMENT_ENABLED"),
            settings.login_agreement_enabled,
        ),
        "login_agreement_text": runtime_config.get(
            "LOGIN_AGREEMENT_TEXT",
            settings.login_agreement_text,
        ),
        "service_terms_enabled": parse_bool(
            runtime_config.get("SERVICE_TERMS_ENABLED"),
            settings.service_terms_enabled,
        ),
        "service_terms_text": runtime_config.get(
            "SERVICE_TERMS_TEXT",
            settings.service_terms_text,
        ),
    }


def build_github_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, Any]:
    return {
        "client_id": runtime_config.get("GITHUB_CLIENT_ID", settings.github_client_id),
        "client_secret": runtime_config.get("GITHUB_CLIENT_SECRET", settings.github_client_secret),
        "callback_url": runtime_config.get("GITHUB_CALLBACK_URL", settings.github_callback_url),
        "scope": runtime_config.get("GITHUB_SCOPE", settings.github_scope),
        "admin_org": runtime_config.get("GITHUB_ADMIN_ORG", settings.github_admin_org),
        "api_token": runtime_config.get("GITHUB_API_TOKEN", settings.github_api_token),
        "metadata_sync_enabled": parse_bool(
            runtime_config.get("GITHUB_METADATA_SYNC_ENABLED"),
            settings.github_metadata_sync_enabled,
        ),
        "metadata_sync_interval_seconds": clamp_sync_interval(
            runtime_config.get(
                "GITHUB_METADATA_SYNC_INTERVAL_SECONDS",
                str(settings.github_metadata_sync_interval_seconds),
            )
        ),
    }


def build_market_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, Any]:
    return {
        "submissions_enabled": parse_bool(
            runtime_config.get("MARKET_SUBMISSIONS_ENABLED"),
            settings.market_submissions_enabled,
        ),
        "comments_enabled": parse_bool(
            runtime_config.get("MARKET_COMMENTS_ENABLED"),
            settings.market_comments_enabled,
        ),
        "likes_enabled": parse_bool(
            runtime_config.get("MARKET_LIKES_ENABLED"),
            settings.market_likes_enabled,
        ),
        "plugin_auto_approve_enabled": parse_bool(
            runtime_config.get("PLUGIN_AUTO_APPROVE_ENABLED"),
            settings.plugin_auto_approve_enabled,
        ),
        "max_plugin_tags": parse_int(
            runtime_config.get("MAX_PLUGIN_TAGS"),
            settings.max_plugin_tags,
        ),
    }


def build_email_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, Any]:
    return {
        "provider": normalize_email_provider(
            runtime_config.get("EMAIL_PROVIDER", settings.email_provider)
        ),
        "smtp": {
            "host": runtime_config.get("SMTP_HOST", settings.smtp_host),
            "port": parse_int(runtime_config.get("SMTP_PORT"), settings.smtp_port),
            "username": runtime_config.get("SMTP_USERNAME", settings.smtp_username),
            "password": runtime_config.get("SMTP_PASSWORD", settings.smtp_password),
            "from_address": runtime_config.get("SMTP_FROM", settings.smtp_from),
            "ssl": parse_bool(runtime_config.get("SMTP_SSL"), settings.smtp_ssl),
        },
        "cloudflare": {
            "account_id": runtime_config.get(
                "CLOUDFLARE_EMAIL_ACCOUNT_ID", settings.cloudflare_email_account_id
            ),
            "api_token": runtime_config.get(
                "CLOUDFLARE_EMAIL_API_TOKEN", settings.cloudflare_email_api_token
            ),
            "from_address": runtime_config.get(
                "CLOUDFLARE_EMAIL_FROM", settings.cloudflare_email_from
            ),
        },
        "daily_limit": parse_int(
            runtime_config.get("EMAIL_DAILY_LIMIT"), settings.email_daily_limit
        ),
        "verification_daily_limit_per_user": parse_int(
            runtime_config.get("EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER"),
            settings.email_verification_daily_limit_per_user,
        ),
    }


def redact_github_settings(config: dict[str, Any]) -> dict[str, Any]:
    return {
        **config,
        "client_secret": MASKED_SECRET if config.get("client_secret") else "",
        "client_secret_configured": bool(config.get("client_secret")),
        "api_token": MASKED_SECRET if config.get("api_token") else "",
        "api_token_configured": bool(config.get("api_token")),
    }


def redact_email_settings(config: dict[str, Any]) -> dict[str, Any]:
    smtp = {**config.get("smtp", {})}
    cloudflare = {**config.get("cloudflare", {})}
    smtp["password_configured"] = bool(smtp.get("password"))
    smtp["password"] = MASKED_SECRET if smtp.get("password") else ""
    cloudflare["api_token_configured"] = bool(cloudflare.get("api_token"))
    cloudflare["api_token"] = MASKED_SECRET if cloudflare.get("api_token") else ""
    return {**config, "smtp": smtp, "cloudflare": cloudflare}


def runtime_values_from_system_settings(
    payload: SystemSettingsPayload,
    runtime_config: dict[str, str],
) -> dict[str, str]:
    values = {
        "SITE_CONTACT_EMAIL": payload.site.contact_email,
        "SITE_DESCRIPTION": payload.site.description,
        "SITE_DOCS_URL": payload.site.docs_url,
        "SITE_ICON_URL": payload.site.icon_url,
        "SITE_NAME": payload.site.name,
        "SITE_SUBTITLE": payload.site.subtitle,
        "GITHUB_LOGIN_ENABLED": serialize_bool(payload.auth.github_login_enabled),
        "LOGIN_AGREEMENT_ENABLED": serialize_bool(payload.auth.login_agreement_enabled),
        "LOGIN_AGREEMENT_TEXT": payload.auth.login_agreement_text,
        "PUBLIC_LOGIN_ENABLED": serialize_bool(payload.auth.public_login_enabled),
        "SERVICE_TERMS_ENABLED": serialize_bool(payload.auth.service_terms_enabled),
        "SERVICE_TERMS_TEXT": payload.auth.service_terms_text,
        "GITHUB_ADMIN_ORG": payload.github.admin_org,
        "GITHUB_CALLBACK_URL": payload.github.callback_url,
        "GITHUB_CLIENT_ID": payload.github.client_id,
        "GITHUB_SCOPE": payload.github.scope,
        "GITHUB_METADATA_SYNC_ENABLED": serialize_bool(payload.github.metadata_sync_enabled),
        "GITHUB_METADATA_SYNC_INTERVAL_SECONDS": str(payload.github.metadata_sync_interval_seconds),
        **runtime_values_from_market_settings(payload.market),
        **runtime_values_from_email_settings(payload.email, runtime_config),
    }
    if should_write_secret(payload.github.client_secret):
        values["GITHUB_CLIENT_SECRET"] = payload.github.client_secret
    elif "GITHUB_CLIENT_SECRET" in runtime_config:
        values["GITHUB_CLIENT_SECRET"] = runtime_config["GITHUB_CLIENT_SECRET"]
    if should_write_secret(payload.github.api_token):
        values["GITHUB_API_TOKEN"] = payload.github.api_token
    elif "GITHUB_API_TOKEN" in runtime_config:
        values["GITHUB_API_TOKEN"] = runtime_config["GITHUB_API_TOKEN"]
    return values


def runtime_values_from_market_settings(payload: Any) -> dict[str, str]:
    return {
        "MARKET_COMMENTS_ENABLED": serialize_bool(payload.comments_enabled),
        "MARKET_LIKES_ENABLED": serialize_bool(payload.likes_enabled),
        "MARKET_SUBMISSIONS_ENABLED": serialize_bool(payload.submissions_enabled),
        "MAX_PLUGIN_TAGS": str(payload.max_plugin_tags),
        "PLUGIN_AUTO_APPROVE_ENABLED": serialize_bool(payload.plugin_auto_approve_enabled),
    }


def runtime_values_from_email_settings(
    payload: Any,
    runtime_config: dict[str, str],
) -> dict[str, str]:
    values = {
        "CLOUDFLARE_EMAIL_ACCOUNT_ID": payload.cloudflare.account_id,
        "CLOUDFLARE_EMAIL_FROM": payload.cloudflare.from_address,
        "EMAIL_DAILY_LIMIT": str(payload.daily_limit),
        "EMAIL_PROVIDER": payload.provider,
        "EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER": str(payload.verification_daily_limit_per_user),
        "SMTP_FROM": payload.smtp.from_address,
        "SMTP_HOST": payload.smtp.host,
        "SMTP_PORT": str(payload.smtp.port),
        "SMTP_SSL": serialize_bool(payload.smtp.ssl),
        "SMTP_USERNAME": payload.smtp.username,
    }
    if should_write_secret(payload.smtp.password):
        values["SMTP_PASSWORD"] = payload.smtp.password
    elif "SMTP_PASSWORD" in runtime_config:
        values["SMTP_PASSWORD"] = runtime_config["SMTP_PASSWORD"]
    if should_write_secret(payload.cloudflare.api_token):
        values["CLOUDFLARE_EMAIL_API_TOKEN"] = payload.cloudflare.api_token
    elif "CLOUDFLARE_EMAIL_API_TOKEN" in runtime_config:
        values["CLOUDFLARE_EMAIL_API_TOKEN"] = runtime_config["CLOUDFLARE_EMAIL_API_TOKEN"]
    return values


def settings_from_system_settings(
    current: Settings,
    payload: SystemSettingsPayload,
    runtime_config: dict[str, str],
) -> Settings:
    return current.with_updates(
        site_name=payload.site.name,
        site_icon_url=payload.site.icon_url,
        site_subtitle=payload.site.subtitle,
        site_description=payload.site.description,
        site_contact_email=payload.site.contact_email,
        site_docs_url=payload.site.docs_url,
        github_login_enabled=payload.auth.github_login_enabled,
        public_login_enabled=payload.auth.public_login_enabled,
        login_agreement_enabled=payload.auth.login_agreement_enabled,
        login_agreement_text=payload.auth.login_agreement_text,
        service_terms_enabled=payload.auth.service_terms_enabled,
        service_terms_text=payload.auth.service_terms_text,
        github_client_id=payload.github.client_id,
        github_client_secret=preserve_secret(
            payload.github.client_secret,
            runtime_config.get("GITHUB_CLIENT_SECRET") or current.github_client_secret,
        ),
        github_callback_url=payload.github.callback_url,
        github_scope=payload.github.scope,
        github_admin_org=payload.github.admin_org,
        github_api_token=preserve_secret(
            payload.github.api_token,
            runtime_config.get("GITHUB_API_TOKEN") or current.github_api_token,
        ),
        github_metadata_sync_enabled=payload.github.metadata_sync_enabled,
        github_metadata_sync_interval_seconds=payload.github.metadata_sync_interval_seconds,
        market_submissions_enabled=payload.market.submissions_enabled,
        market_comments_enabled=payload.market.comments_enabled,
        market_likes_enabled=payload.market.likes_enabled,
        plugin_auto_approve_enabled=payload.market.plugin_auto_approve_enabled,
        max_plugin_tags=payload.market.max_plugin_tags,
        email_provider=payload.email.provider,
        smtp_host=payload.email.smtp.host,
        smtp_port=payload.email.smtp.port,
        smtp_username=payload.email.smtp.username,
        smtp_password=preserve_secret(
            payload.email.smtp.password,
            runtime_config.get("SMTP_PASSWORD") or current.smtp_password,
        ),
        smtp_from=payload.email.smtp.from_address,
        smtp_ssl=payload.email.smtp.ssl,
        cloudflare_email_account_id=payload.email.cloudflare.account_id,
        cloudflare_email_api_token=preserve_secret(
            payload.email.cloudflare.api_token,
            runtime_config.get("CLOUDFLARE_EMAIL_API_TOKEN") or current.cloudflare_email_api_token,
        ),
        cloudflare_email_from=payload.email.cloudflare.from_address,
        email_daily_limit=payload.email.daily_limit,
        email_verification_daily_limit_per_user=payload.email.verification_daily_limit_per_user,
    )


def settings_restart_required(settings: Settings, runtime_config: dict[str, str]) -> bool:
    runtime_database_url = runtime_config.get("DATABASE_URL", "")
    runtime_redis_url = runtime_config.get("REDIS_URL", "")
    return bool(runtime_database_url or runtime_redis_url) and (
        runtime_database_url != settings.database_url or runtime_redis_url != settings.redis_url
    )


def build_saved_postgres_config(
    runtime_config: dict[str, str], database_url: str
) -> dict[str, Any]:
    parsed = parse_postgres_url(database_url)
    return {
        "host": runtime_config.get("POSTGRES_HOST", parsed["host"]),
        "port": parse_int(runtime_config.get("POSTGRES_PORT"), parsed["port"]),
        "database": runtime_config.get("POSTGRES_DATABASE", parsed["database"]),
        "username": runtime_config.get("POSTGRES_USER", parsed["username"]),
        "password": runtime_config.get("POSTGRES_PASSWORD", parsed["password"]),
        "ssl": parse_bool(runtime_config.get("POSTGRES_SSL"), parsed["ssl"]),
    }


def build_saved_redis_config(runtime_config: dict[str, str], redis_url: str) -> dict[str, Any]:
    parsed = parse_redis_url(redis_url)
    return {
        "host": runtime_config.get("REDIS_HOST", parsed["host"]),
        "port": parse_int(runtime_config.get("REDIS_PORT"), parsed["port"]),
        "database": parse_int(runtime_config.get("REDIS_DATABASE"), parsed["database"]),
        "password": runtime_config.get("REDIS_PASSWORD", parsed["password"]),
        "ssl": parse_bool(runtime_config.get("REDIS_SSL"), parsed["ssl"]),
    }


def build_postgres_url(config: dict[str, Any]) -> str:
    username = quote(config["username"], safe="")
    password = quote(config["password"], safe="")
    database = quote(config["database"], safe="")
    host = format_url_host(config["host"])
    query = urlencode({"sslmode": "require" if config["ssl"] else "disable"})
    return f"postgresql://{username}:{password}@{host}:{config['port']}/{database}?{query}"


def build_redis_url(config: dict[str, Any]) -> str:
    scheme = "rediss" if config["ssl"] else "redis"
    password = f":{quote(config['password'], safe='')}@" if config["password"] else ""
    database = int(config["database"])
    host = format_url_host(config["host"])
    return f"{scheme}://{password}{host}:{config['port']}/{database}"


def format_url_host(host: str) -> str:
    value = host.strip()
    if ":" in value and not value.startswith("["):
        return f"[{value}]"
    return value


def parse_postgres_url(value: str) -> dict[str, Any]:
    config = DEFAULT_POSTGRES_CONFIG.copy()
    parsed = urlparse(value or "")
    if not parsed.scheme.startswith("postgresql"):
        return config
    query = parse_qs(parsed.query)
    sslmode = (query.get("sslmode") or [""])[0].lower()
    config.update(
        {
            "host": parsed.hostname or config["host"],
            "port": parsed.port or config["port"],
            "database": unquote(parsed.path.lstrip("/")),
            "username": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "ssl": sslmode not in {"", "disable", "prefer"},
        }
    )
    return config


def parse_redis_url(value: str) -> dict[str, Any]:
    config = DEFAULT_REDIS_CONFIG.copy()
    parsed = urlparse(value or "")
    if parsed.scheme not in {"redis", "rediss"}:
        return config
    config.update(
        {
            "host": parsed.hostname or config["host"],
            "port": parsed.port or config["port"],
            "database": parse_int(parsed.path.lstrip("/"), config["database"]),
            "password": unquote(parsed.password or ""),
            "ssl": parsed.scheme == "rediss",
        }
    )
    return config


def is_valid_site_icon_url(value: str) -> bool:
    if value.startswith("/"):
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_valid_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_PATTERN.match(value or ""))


def has_secret_value(incoming: str, existing: str | None) -> bool:
    return should_write_secret(incoming) or bool(existing)


def should_write_secret(value: str | None) -> bool:
    return bool(value and value != MASKED_SECRET)


def preserve_secret(incoming: str, existing: str) -> str:
    return incoming if should_write_secret(incoming) else existing


def github_profile_payload(profile: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(profile["id"]),
        "login": profile["login"],
        "name": profile.get("name") or profile["login"],
        "avatar_url": profile.get("avatar_url") or "",
    }


async def link_github_profile_to_user(
    request: Request,
    user: dict[str, Any],
    profile: dict[str, str],
) -> dict[str, Any]:
    existing = await call_store(request, "get_user_by_github_login", profile["login"])
    if existing and existing["id"] != user["id"]:
        if not can_merge_github_user(user, existing):
            raise error(409, "This GitHub account is already linked to another user")
        await transfer_user_owned_records(request, existing["id"], user["id"])
    updated = await call_store(
        request,
        "update_user_profile",
        user["id"],
        {
            "auth_source": "github",
            "avatar_url": profile.get("avatar_url") or "",
            "github_id": profile["id"],
            "github_login": profile["login"],
            "github_name": profile.get("name") or profile["login"],
        },
    )
    if not updated:
        raise error(404, "User not found")
    return updated


def can_merge_github_user(current: dict[str, Any], existing: dict[str, Any]) -> bool:
    return (
        not existing.get("internal_username")
        and not existing.get("password_hash")
        and normalize_role(existing.get("role")) == Role.USER
        and normalize_role(current.get("role")) in {Role.CORE_ADMIN, Role.ADMIN}
    )


async def transfer_user_owned_records(
    request: Request,
    from_user_id: str,
    to_user_id: str,
) -> None:
    transfer = getattr(get_store(request), "merge_user_into_user", None)
    if transfer:
        await resolve_optional_awaitable(transfer(from_user_id, to_user_id))


def normalize_email_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    return provider if provider in {"disabled", "smtp", "cloudflare"} else "disabled"


async def send_email(
    app: FastAPI,
    settings: Settings,
    receiver: str,
    subject: str,
    content: str,
) -> None:
    if settings.email_provider == "disabled":
        raise error(400, "Email service is disabled")
    check_email_daily_limit(app, settings)
    if settings.email_provider == "cloudflare":
        await send_email_via_cloudflare(settings, receiver, subject, content)
    elif settings.email_provider == "smtp":
        await send_email_via_smtp(settings, receiver, subject, content)
    else:
        raise error(400, "Unsupported email provider")
    increment_email_daily_count(app, settings)


def check_email_daily_limit(app: FastAPI, settings: Settings) -> None:
    if settings.email_daily_limit <= 0:
        return
    today = datetime.now(UTC).strftime("%Y%m%d")
    counter = getattr(app.state, "email_daily_counter", {"date": "", "count": 0})
    if counter.get("date") != today:
        counter = {"date": today, "count": 0}
        app.state.email_daily_counter = counter
    if int(counter.get("count", 0)) >= settings.email_daily_limit:
        raise error(429, "Daily email limit exceeded")


def increment_email_daily_count(app: FastAPI, settings: Settings) -> None:
    if settings.email_daily_limit <= 0:
        return
    today = datetime.now(UTC).strftime("%Y%m%d")
    counter = getattr(app.state, "email_daily_counter", {"date": today, "count": 0})
    if counter.get("date") != today:
        counter = {"date": today, "count": 0}
    counter["count"] = int(counter.get("count", 0)) + 1
    app.state.email_daily_counter = counter


async def send_email_via_cloudflare(
    settings: Settings,
    receiver: str,
    subject: str,
    content: str,
) -> None:
    if not settings.cloudflare_email_account_id:
        raise error(400, "Cloudflare account ID is not configured")
    if not settings.cloudflare_email_api_token:
        raise error(400, "Cloudflare API token is not configured")
    if not settings.cloudflare_email_from:
        raise error(400, "Cloudflare from address is not configured")
    payload = {
        "to": receiver,
        "from": settings.cloudflare_email_from,
        "subject": subject[:998],
        "text": content,
        "html": html.escape(content).replace("\n", "<br>"),
    }
    endpoint = CLOUDFLARE_EMAIL_SEND_ENDPOINT.format(
        account_id=quote(settings.cloudflare_email_account_id, safe="")
    )
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            endpoint,
            headers={
                "authorization": f"Bearer {settings.cloudflare_email_api_token}",
                "content-type": "application/json",
            },
            json=payload,
        )
    data = response.json() if response.content else {}
    if response.status_code == 429:
        raise error(502, "Cloudflare email service is rate limited")
    if response.status_code in {401, 403}:
        raise error(502, "Cloudflare email authentication failed")
    if response.status_code >= 500:
        raise error(502, f"Cloudflare email service error: HTTP {response.status_code}")
    if response.status_code >= 400 or not data.get("success", False):
        raise error(502, cloudflare_email_error_message(data, response.status_code))
    permanent_bounces = (data.get("result") or {}).get("permanent_bounces") or []
    if permanent_bounces:
        raise error(502, f"Cloudflare email permanently bounced: {permanent_bounces}")


def cloudflare_email_error_message(data: dict[str, Any], status_code: int) -> str:
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors:
        messages = [
            f"[{item.get('code')}] {item.get('message')}"
            for item in errors
            if isinstance(item, dict)
        ]
        return "Cloudflare email API error: " + "; ".join(messages)
    return f"Cloudflare email API error: HTTP {status_code}"


async def send_email_via_smtp(
    settings: Settings,
    receiver: str,
    subject: str,
    content: str,
) -> None:
    if not settings.smtp_host:
        raise error(400, "SMTP host is not configured")
    if not settings.smtp_from:
        raise error(400, "SMTP from address is not configured")
    try:
        await asyncio.to_thread(send_email_via_smtp_sync, settings, receiver, subject, content)
    except smtplib.SMTPException as exc:
        raise error(502, f"SMTP send failed: {exc}") from exc


def send_email_via_smtp_sync(
    settings: Settings,
    receiver: str,
    subject: str,
    content: str,
) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = receiver
    message["Subject"] = subject
    message.set_content(content)
    if settings.smtp_ssl or settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10) as client:
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


def serialize_bool(value: bool) -> str:
    return "true" if value else "false"


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value: str | int | None, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_astrbot_plugin_source(plugins: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    feed: dict[str, dict[str, Any]] = {}
    for plugin in plugins:
        name = str(plugin.get("name") or plugin.get("id") or "").strip()
        if not name:
            continue
        feed[name] = format_astrbot_plugin(plugin, name)
    return dict(sorted(feed.items()))


def format_astrbot_plugin(plugin: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "name": name,
        "display_name": plugin.get("display_name") or name,
        "desc": plugin.get("desc") or "",
        "short_desc": plugin.get("short_desc") or plugin.get("desc") or "",
        "author": plugin.get("author") or plugin.get("owner_github_login") or "",
        "repo": plugin.get("repo") or "",
        "social_link": plugin.get("social_link") or "",
        "tags": plugin.get("tags") if isinstance(plugin.get("tags"), list) else [],
        "stars": int(plugin.get("stars") or 0),
        "updated_at": plugin.get("updated_at") or "",
        "version": plugin.get("version") or "1.0.0",
        "logo": plugin.get("logo") or "",
        "pinned": bool(plugin.get("pinned")),
        "download_url": plugin.get("download_url") or "",
        "i18n": plugin.get("i18n") if isinstance(plugin.get("i18n"), dict) else {},
        "astrbot_version": plugin.get("astrbot_version") or "",
        "category": plugin.get("category") or "",
        "support_platforms": plugin.get("support_platforms")
        if isinstance(plugin.get("support_platforms"), list)
        else [],
    }


def digest_plugin_source(feed: dict[str, dict[str, Any]]) -> str:
    payload = json.dumps(feed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def can_save_setup_without_auth(settings: Settings, runtime_config: dict[str, str]) -> bool:
    has_saved_config = bool(runtime_config.get("DATABASE_URL") or runtime_config.get("REDIS_URL"))
    return settings.is_setup_required() and not has_saved_config


def validate_repo_owner(repo: str, user: dict[str, Any]) -> None:
    owner = validate_github_repo(repo).group("owner")
    if not user.get("github_login"):
        raise error(403, "GitHub login is required to prove repository ownership")
    if owner.lower() == user["github_login"].lower():
        return
    raise error(403, "GitHub account must own the repository")


def set_cookie(
    response: Response,
    name: str,
    value: str,
    settings: Settings,
    max_age: int | None = None,
) -> None:
    response.set_cookie(
        name,
        value,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_same_site.lower(),
        max_age=max_age or settings.session_max_age_seconds,
        path="/",
    )


async def exchange_github_code(settings: Settings, code: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"accept": "application/json"},
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.github_callback_url,
            },
        )
    data = response.json()
    if response.status_code >= 400 or not data.get("access_token"):
        raise error(
            502,
            data.get("error_description")
            or data.get("error")
            or "GitHub OAuth token exchange failed",
        )
    return data["access_token"]


async def fetch_github_profile(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            "https://api.github.com/user",
            headers=github_headers(access_token),
        )
    data = response.json()
    if response.status_code >= 400 or not data.get("login"):
        raise error(502, data.get("message") or "GitHub profile lookup failed")
    return data


async def promote_org_admin_if_needed(
    request: Request,
    user: dict[str, Any],
    access_token: str,
) -> None:
    settings = get_settings(request)
    if not settings.github_admin_org or is_admin(user):
        return
    if await is_github_org_member(settings.github_admin_org, access_token):
        await call_store(request, "update_user_role", user["id"], "admin")


async def is_github_org_member(org: str, access_token: str) -> bool:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"https://api.github.com/user/memberships/orgs/{org}",
            headers=github_headers(access_token),
        )
    if response.status_code == 404:
        return False
    data = response.json()
    return response.status_code < 400 and data.get("state") == "active"


def github_headers(access_token: str) -> dict[str, str]:
    return {
        "accept": "application/vnd.github+json",
        "authorization": f"Bearer {access_token}",
        "user-agent": "astrbot-community-plugins",
    }


def github_public_headers() -> dict[str, str]:
    return {
        "accept": "application/vnd.github+json",
        "user-agent": "astrbot-community-plugins",
    }


def github_api_headers(
    user: dict[str, Any] | None = None,
    settings: Settings | None = None,
    token: str = "",
) -> dict[str, str]:
    token = str(token or (user or {}).get("github_token") or "").strip()
    if not token and settings:
        token = settings.github_api_token.strip()
    if not token:
        return github_public_headers()
    return {
        **github_public_headers(),
        "authorization": f"Bearer {token}",
    }


async def all_api_keys(request: Request) -> list[ApiKey | dict[str, Any]]:
    settings = get_settings(request)
    return [*settings.api_keys, *await call_store(request, "list_api_keys")]


def public_api_key(key: ApiKey | dict[str, Any]) -> dict[str, Any]:
    if isinstance(key, ApiKey):
        return {"name": key.name, "scopes": list(key.scopes)}
    return {"id": key.get("id"), "name": key.get("name"), "scopes": key.get("scopes", [])}


def error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


app = create_app()
