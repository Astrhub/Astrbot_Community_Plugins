from __future__ import annotations

import hashlib
import inspect
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .auth import (
    can_edit_plugin,
    can_manage_admins,
    can_manage_plugin_submission,
    can_moderate_community,
    can_moderate_plugins,
    can_publish_announcement,
    hash_password,
    is_admin,
    is_core_admin,
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
    PluginPatch,
    PluginSubmission,
    RoleUpdatePayload,
    SetupConfig,
)
from .store import InMemoryMarketStore
from .store import PgRedisMarketStore

GITHUB_REPO_PATTERN = re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/?$")
PLUGIN_NAME_PATTERN = re.compile(r"^astrbot_plugin_[a-z0-9_-]+$", re.IGNORECASE)
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
    try:
        yield None
    finally:
        await maybe_call_store_lifecycle(app, "close")


async def maybe_call_store_lifecycle(app: FastAPI, method_name: str) -> None:
    method = getattr(app.state.store, method_name, None)
    if not method:
        return
    result = method()
    if inspect.isawaitable(result):
        await result


async def bootstrap_internal_core_admin(app: FastAPI) -> None:
    settings = app.state.settings
    if not settings.core_admin_username or not settings.core_admin_password_hash:
        return
    result = app.state.store.create_internal_admin(
        settings.core_admin_username,
        settings.core_admin_password_hash,
    )
    if inspect.isawaitable(result):
        await result


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
        return {
            **get_site_config(settings),
            "auth": get_public_auth_config(settings),
        }

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
            "restart_required": bool(runtime_database_url or runtime_redis_url)
            and (
                runtime_database_url != settings.database_url
                or runtime_redis_url != settings.redis_url
                or saved_setup["site"] != get_site_config(settings)
            ),
        }

    @app.post("/v1/setup")
    async def save_setup(request: Request, payload: SetupConfig) -> dict[str, Any]:
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
                "GITHUB_LOGIN_ENABLED": serialize_bool(payload.auth.github_login_enabled),
                "LOGIN_AGREEMENT_ENABLED": serialize_bool(payload.auth.login_agreement_enabled),
                "LOGIN_AGREEMENT_TEXT": payload.auth.login_agreement_text,
                "PUBLIC_LOGIN_ENABLED": serialize_bool(payload.auth.public_login_enabled),
                "SERVICE_TERMS_ENABLED": serialize_bool(payload.auth.service_terms_enabled),
                "SERVICE_TERMS_TEXT": payload.auth.service_terms_text,
            },
        )
        await call_store(
            request,
            "create_internal_admin",
            payload.admin.username,
            core_admin_password_hash,
        )
        return {
            "saved": True,
            "restart_required": True,
            "message": "Configuration saved. Restart the API process to use PostgreSQL and Redis.",
        }

    @app.get("/v1/me")
    async def me(request: Request) -> dict[str, Any]:
        return public_user(await require_user(request))

    @app.post("/v1/auth/internal/login")
    async def internal_login(request: Request, payload: InternalLoginPayload) -> Response:
        settings = get_settings(request)
        if not settings.public_login_enabled:
            raise error(403, "Login is closed")
        user = await call_store(request, "get_user_by_internal_username", payload.username)
        if not user or not verify_password(payload.password, user.get("password_hash", "")):
            raise error(401, "Invalid username or password")
        session = await call_store(request, "create_session", user["id"])
        response = JSONResponse({"user": public_user(user), "session": session})
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        return response

    @app.get("/v1/auth/github/login")
    async def github_login(request: Request) -> Response:
        settings = get_settings(request)
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
                "redirect_uri": settings.github_callback_url,
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
        settings = get_settings(request)
        expected_state = request.cookies.get(settings.oauth_state_cookie_name)
        if not code or not state or not expected_state or state != expected_state:
            raise error(400, "Invalid OAuth callback")

        access_token = await exchange_github_code(settings, code)
        profile = await fetch_github_profile(access_token)
        user = await call_store(
            request,
            "upsert_github_user",
            {
                "id": str(profile["id"]),
                "login": profile["login"],
                "name": profile.get("name") or profile["login"],
                "avatar_url": profile.get("avatar_url") or "",
            },
        )
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
        data = payload.model_dump()
        validate_plugin_submission(data)
        validate_repo_owner(data["repo"], user)
        return await call_store(request, "submit_plugin", user, data)

    @app.get("/v1/plugins/{plugin_id}")
    async def plugin_detail(request: Request, plugin_id: str) -> dict[str, Any]:
        plugin = await get_plugin_or_404(request, plugin_id)
        return {**plugin, "comments": await call_store(request, "list_comments", plugin_id)}

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

    @app.post("/v1/plugins/{plugin_id}/like")
    async def like_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        plugin = await get_plugin_or_404(request, plugin_id)
        return (
            await call_store(
                request, "update_plugin_metadata", plugin_id, {"likes": plugin["likes"] + 1}
            )
            or {}
        )

    @app.post("/v1/plugins/{plugin_id}/unlike")
    async def unlike_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        plugin = await get_plugin_or_404(request, plugin_id)
        return (
            await call_store(
                request,
                "update_plugin_metadata",
                plugin_id,
                {"likes": max(0, plugin["likes"] - 1)},
            )
            or {}
        )

    @app.post("/v1/plugins/{plugin_id}/comments", status_code=201)
    async def add_comment(
        request: Request, plugin_id: str, payload: CommentCreate
    ) -> dict[str, Any]:
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
        updated = await call_store(request, "update_plugin_status", plugin_id, "listed", user["id"])
        if not updated:
            raise error(404, "Plugin not found")
        return updated

    @app.post("/v1/admin/plugins/{plugin_id}/unlist")
    async def unlist_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise error(403, "Forbidden")
        updated = await call_store(
            request, "update_plugin_status", plugin_id, "unlisted", user["id"]
        )
        if not updated:
            raise error(404, "Plugin not found")
        return updated

    @app.delete("/v1/admin/comments/{comment_id}")
    async def delete_comment(request: Request, comment_id: str) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_community(user):
            raise error(403, "Forbidden")
        deleted = await call_store(request, "delete_comment", comment_id, user["id"])
        if not deleted:
            raise error(404, "Comment not found")
        return deleted

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
    return {key: value for key, value in user.items() if key not in {"password_hash"}}


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


def validate_plugin_submission(payload: dict[str, Any]) -> None:
    for field in ("name", "repo", "desc", "author"):
        if not payload.get(field):
            raise error(400, "Missing required plugin fields")
    validate_plugin_name(payload["name"])
    validate_github_repo(payload["repo"])


def validate_plugin_name(name: str) -> None:
    if not PLUGIN_NAME_PATTERN.match(name or ""):
        raise error(400, "Plugin name must use astrbot_plugin_ prefix")


def validate_github_repo(repo: str) -> re.Match[str]:
    match = GITHUB_REPO_PATTERN.match(repo or "")
    if not match:
        raise error(400, "Plugin repo must be a GitHub URL")
    return match


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
    if payload.auth.login_agreement_enabled and not payload.auth.login_agreement_text:
        raise error(400, "Login agreement text is required when enabled")
    if payload.auth.service_terms_enabled and not payload.auth.service_terms_text:
        raise error(400, "Service terms text is required when enabled")


def get_site_config(settings: Settings) -> dict[str, str]:
    return {
        "name": settings.site_name,
        "icon_url": settings.site_icon_url,
    }


def get_public_auth_config(settings: Settings) -> dict[str, Any]:
    return {
        "github_login_enabled": settings.github_login_enabled,
        "public_login_enabled": settings.public_login_enabled,
        "login_agreement_enabled": settings.login_agreement_enabled,
        "login_agreement_text": settings.login_agreement_text,
        "service_terms_enabled": settings.service_terms_enabled,
        "service_terms_text": settings.service_terms_text,
        "terms_revision": digest_terms(settings),
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
    return {
        "postgres": build_saved_postgres_config(runtime_config, database_url),
        "redis": build_saved_redis_config(runtime_config, redis_url),
        "site": {
            "name": runtime_config.get("SITE_NAME", settings.site_name),
            "icon_url": runtime_config.get("SITE_ICON_URL", settings.site_icon_url),
        },
        "auth": {
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
        },
    }


def redact_setup_infrastructure(config: dict[str, Any]) -> dict[str, Any]:
    redacted = {
        "postgres": {**DEFAULT_POSTGRES_CONFIG},
        "redis": {**DEFAULT_REDIS_CONFIG},
        "site": {**config["site"]},
        "auth": {**config["auth"]},
    }
    return redacted


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
