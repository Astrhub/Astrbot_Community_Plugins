"""Email registration, verification challenges, password reset, email change, and OAuth unbinding routes."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .auth import hash_password, verify_password
from .config import Settings
from .email_challenges import (
    CHALLENGE_TTL_SECONDS,
    EmailChallengeError,
    clear_verification_cooldown,
    complete_email_challenge,
    create_email_challenge,
    delete_email_challenge,
    enforce_direct_register_rate_limit,
    enforce_verification_request_limits,
    release_email_challenge,
    release_rate_limit_reservation,
    render_email_changed_notice,
    render_verification_email,
    reserve_email_challenge,
)
from .github_authorization import revoke_github_identity_authorizations
from .oauth_identities import (
    can_unbind_oauth_identity,
    clean_email,
    list_active_oauth_identities,
    normalize_email,
    public_oauth_identity,
    validate_public_username,
)
from .schemas import (
    EmailChangeRequestPayload,
    EmailChangeVerifyPayload,
    EmailRegisterPayload,
    EmailRegisterRequestPayload,
    EmailRegisterVerifyPayload,
    PasswordChangePayload,
    PasswordResetRequestPayload,
    PasswordResetVerifyPayload,
)
from .store import UserIdentityConflict

LOGGER = logging.getLogger(__name__)
MIN_PASSWORD_LENGTH = 8


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client and request.client.host else "unknown"


def _http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retry_after: int | None = None,
) -> HTTPException:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "error": message},
        headers=headers,
    )


def _raise_challenge_error(exc: EmailChallengeError) -> None:
    raise _http_error(
        exc.status_code,
        exc.code,
        exc.message,
        retry_after=exc.retry_after,
    ) from exc


def build_email_account_router(
    *,
    runtime_settings_for_app: Callable[[Any], Awaitable[Settings]],
    call_store: Callable[..., Awaitable[Any]],
    require_user: Callable[[Request], Awaitable[dict[str, Any]]],
    enrich_private_user: Callable[[Request, dict[str, Any]], Awaitable[dict[str, Any]]],
    set_cookie: Callable[..., None],
    is_valid_email: Callable[[str], bool],
    send_email: Callable[[Any, Settings, str, str, str], Awaitable[None]],
) -> APIRouter:
    router = APIRouter()

    def _validate_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise _http_error(
                400,
                "password_too_short",
                f"密码长度不能少于 {MIN_PASSWORD_LENGTH} 个字符",
            )
        if len(password) > 128:
            raise _http_error(400, "password_too_long", "密码长度不能超过 128 个字符")

    def _validate_username_and_email(username: str, email: str) -> tuple[str, str]:
        try:
            clean_username = validate_public_username(username)
        except ValueError as exc:
            raise _http_error(400, "invalid_username", str(exc)) from exc
        cleaned_email = clean_email(email)
        if not cleaned_email or not is_valid_email(cleaned_email):
            raise _http_error(400, "invalid_email", "请输入有效的邮箱地址")
        return clean_username, cleaned_email

    async def _send_email_best_effort(
        request: Request,
        settings: Settings,
        receiver: str,
        subject: str,
        body: str,
    ) -> None:
        if not receiver or settings.email_provider == "disabled" or not is_valid_email(receiver):
            return
        try:
            await send_email(request.app, settings, receiver, subject, body)
        except Exception:
            LOGGER.warning("Failed to send account notice email", exc_info=False)

    @router.post(
        "/v1/auth/email/register",
        status_code=201,
        tags=["auth"],
        summary="邮箱直接注册（关闭邮箱验证时）",
        description="当站点开启邮箱注册且关闭邮箱验证开关时，直接创建注册用户账号并建立会话。",
    )
    async def email_register_direct(
        request: Request,
        payload: EmailRegisterPayload,
    ) -> Response:
        settings = await runtime_settings_for_app(request.app)
        if not settings.email_registration_enabled:
            raise _http_error(403, "email_registration_disabled", "当前站点未开启邮箱自助注册")
        if settings.email_verification_required:
            raise _http_error(
                400,
                "email_verification_required",
                "当前站点要求邮箱验证，请先获取邮箱验证码完成注册",
            )
        try:
            await enforce_direct_register_rate_limit(request.app.state.store, _client_ip(request))
        except EmailChallengeError as exc:
            _raise_challenge_error(exc)

        clean_username, cleaned_email = _validate_username_and_email(
            payload.username, payload.email
        )
        _validate_password(payload.password)

        user = await call_store(
            request,
            "create_email_user",
            username=clean_username,
            email=cleaned_email,
            password_hash=hash_password(payload.password),
            email_verified_at=None,
        )
        session = await call_store(request, "create_session", user["id"])
        enriched = await enrich_private_user(request, user)
        response = JSONResponse({"user": enriched, "session": session}, status_code=201)
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        return response

    @router.post(
        "/v1/auth/email/register/request",
        status_code=202,
        tags=["auth"],
        summary="请求邮箱注册验证码",
        description="创建注册验证挑战并向目标邮箱发送 6 位验证码。统一返回受理状态，避免枚举已注册邮箱。",
    )
    async def email_register_request(
        request: Request,
        payload: EmailRegisterRequestPayload,
    ) -> dict[str, Any]:
        settings = await runtime_settings_for_app(request.app)
        if not settings.email_registration_enabled:
            raise _http_error(403, "email_registration_disabled", "当前站点未开启邮箱自助注册")
        if not settings.email_verification_required:
            raise _http_error(
                400,
                "email_verification_not_required",
                "当前站点未开启邮箱验证，请直接提交注册",
            )
        if settings.email_provider == "disabled":
            raise _http_error(
                503,
                "email_service_unavailable",
                "邮件服务尚未启用，暂时无法发送注册验证码",
            )

        clean_username, cleaned_email = _validate_username_and_email(
            payload.username, payload.email
        )
        _validate_password(payload.password)

        try:
            reservation_key = await enforce_verification_request_limits(
                request.app.state.store,
                settings,
                purpose="register",
                email=cleaned_email,
                client_ip=_client_ip(request),
            )
        except EmailChallengeError as exc:
            _raise_challenge_error(exc)

        existing_username = await call_store(
            request, "get_user_by_public_username", clean_username
        ) or await call_store(request, "get_user_by_internal_username", clean_username)
        if existing_username:
            await release_rate_limit_reservation(request.app.state.store, reservation_key)
            raise _http_error(409, "username_already_exists", "用户名已被占用")

        existing_email = await call_store(request, "get_user_by_email", cleaned_email)
        if existing_email:
            await release_rate_limit_reservation(request.app.state.store, reservation_key)
            return {
                "accepted": True,
                "registration_id": f"chg_{secrets.token_urlsafe(24)}",
                "expires_in": CHALLENGE_TTL_SECONDS,
            }

        challenge_id = ""
        try:
            challenge_id, code, expires_in = await create_email_challenge(
                request.app.state.store,
                purpose="register",
                email=cleaned_email,
                payload={
                    "username": clean_username,
                    "email": cleaned_email,
                    "password_hash": hash_password(payload.password),
                },
            )
            subject, body = render_verification_email(settings.site_name, "register", code)
            await send_email(request.app, settings, cleaned_email, subject, body)
        except Exception:
            if challenge_id:
                await delete_email_challenge(request.app.state.store, challenge_id)
            await clear_verification_cooldown(
                request.app.state.store, purpose="register", email=cleaned_email
            )
            await release_rate_limit_reservation(request.app.state.store, reservation_key)
            raise

        return {
            "accepted": True,
            "registration_id": challenge_id,
            "expires_in": expires_in,
        }

    @router.post(
        "/v1/auth/email/register/verify",
        status_code=201,
        tags=["auth"],
        summary="验证注册验证码并创建账号",
        description="原子消费注册验证码挑战、创建已验证邮箱账号并签发登录会话。",
    )
    async def email_register_verify(
        request: Request,
        payload: EmailRegisterVerifyPayload,
    ) -> Response:
        settings = await runtime_settings_for_app(request.app)
        if not settings.email_registration_enabled:
            raise _http_error(403, "email_registration_disabled", "当前站点未开启邮箱自助注册")

        try:
            challenge_payload = await reserve_email_challenge(
                request.app.state.store,
                challenge_id=payload.registration_id,
                purpose="register",
                code=payload.code,
            )
        except EmailChallengeError as exc:
            _raise_challenge_error(exc)

        try:
            user = await call_store(
                request,
                "create_email_user",
                username=str(challenge_payload["username"]),
                email=str(challenge_payload["email"]),
                password_hash=str(challenge_payload["password_hash"]),
                email_verified_at=_utc_now(),
            )
        except UserIdentityConflict:
            await complete_email_challenge(request.app.state.store, payload.registration_id)
            raise
        except Exception:
            await release_email_challenge(request.app.state.store, payload.registration_id)
            raise

        await complete_email_challenge(request.app.state.store, payload.registration_id)
        session = await call_store(request, "create_session", user["id"])
        enriched = await enrich_private_user(request, user)
        response = JSONResponse({"user": enriched, "session": session}, status_code=201)
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        return response

    @router.post(
        "/v1/auth/email/password-reset/request",
        status_code=202,
        tags=["auth"],
        summary="请求找回密码验证码",
        description="向账号邮箱发送密码重置验证码（邮件服务启用时，未验证邮箱也可通过验证码找回密码）。统一返回受理状态。",
    )
    async def password_reset_request(
        request: Request,
        payload: PasswordResetRequestPayload,
    ) -> dict[str, Any]:
        settings = await runtime_settings_for_app(request.app)
        if settings.email_provider == "disabled":
            raise _http_error(
                503,
                "email_service_unavailable",
                "邮件服务尚未启用，暂时无法自助找回密码",
            )
        cleaned_email = clean_email(payload.email)
        if not cleaned_email or not is_valid_email(cleaned_email):
            raise _http_error(400, "invalid_email", "请输入有效的邮箱地址")

        try:
            reservation_key = await enforce_verification_request_limits(
                request.app.state.store,
                settings,
                purpose="password_reset",
                email=cleaned_email,
                client_ip=_client_ip(request),
            )
        except EmailChallengeError as exc:
            _raise_challenge_error(exc)

        user = await call_store(request, "get_user_by_email", cleaned_email)
        if not user:
            await release_rate_limit_reservation(request.app.state.store, reservation_key)
            return {
                "accepted": True,
                "reset_id": f"chg_{secrets.token_urlsafe(24)}",
                "expires_in": CHALLENGE_TTL_SECONDS,
            }

        challenge_id = ""
        try:
            challenge_id, code, expires_in = await create_email_challenge(
                request.app.state.store,
                purpose="password_reset",
                email=cleaned_email,
                payload={"user_id": str(user["id"]), "email": str(user["email"])},
            )
            subject, body = render_verification_email(settings.site_name, "password_reset", code)
            await send_email(request.app, settings, str(user["email"]), subject, body)
        except Exception:
            if challenge_id:
                await delete_email_challenge(request.app.state.store, challenge_id)
            await clear_verification_cooldown(
                request.app.state.store, purpose="password_reset", email=cleaned_email
            )
            await release_rate_limit_reservation(request.app.state.store, reservation_key)
            raise

        return {
            "accepted": True,
            "reset_id": challenge_id,
            "expires_in": expires_in,
        }

    @router.post(
        "/v1/auth/email/password-reset/verify",
        tags=["auth"],
        summary="验证并重置密码",
        description="核验重置验证码、更新密码、标记邮箱已验证并撤销该账号的全部已有会话。",
    )
    async def password_reset_verify(
        request: Request,
        payload: PasswordResetVerifyPayload,
    ) -> dict[str, Any]:
        _validate_password(payload.new_password)
        try:
            challenge_payload = await reserve_email_challenge(
                request.app.state.store,
                challenge_id=payload.reset_id,
                purpose="password_reset",
                code=payload.code,
            )
        except EmailChallengeError as exc:
            _raise_challenge_error(exc)

        user_id = str(challenge_payload.get("user_id") or "")
        try:
            updated = await call_store(
                request,
                "set_user_password",
                user_id,
                hash_password(payload.new_password),
                bump_session_version=True,
                verify_email_at=_utc_now(),
                expected_email=str(challenge_payload.get("email") or ""),
            )
        except Exception:
            await release_email_challenge(request.app.state.store, payload.reset_id)
            raise

        await complete_email_challenge(request.app.state.store, payload.reset_id)
        if not updated:
            current = await call_store(request, "get_user_by_id", user_id)
            if current:
                raise _http_error(409, "email_changed", "账号邮箱已变更，请重新获取重置验证码")
            raise _http_error(404, "user_not_found", "目标账号不存在")
        return {"reset": True}

    @router.post(
        "/v1/me/password",
        tags=["user"],
        summary="修改或设置本地密码",
        description="已设密码的用户需验证当前密码后改密；OAuth 建号用户可首次设置本地密码和邮箱。",
    )
    async def update_my_password(
        request: Request,
        payload: PasswordChangePayload,
    ) -> Response:
        user = await require_user(request)
        settings = await runtime_settings_for_app(request.app)
        _validate_password(payload.new_password)
        has_password = bool(str(user.get("password_hash") or "").strip())
        if has_password:
            if not payload.current_password or not verify_password(
                payload.current_password, str(user.get("password_hash") or "")
            ):
                raise _http_error(400, "invalid_current_password", "当前密码不正确")

        next_email: str | None = None
        if payload.email and not str(user.get("email") or "").strip():
            cleaned = clean_email(payload.email)
            if not is_valid_email(cleaned):
                raise _http_error(400, "invalid_email", "请输入有效的邮箱地址")
            next_email = cleaned

        updated = await call_store(
            request,
            "set_user_password",
            user["id"],
            hash_password(payload.new_password),
            bump_session_version=True,
            email=next_email,
        )
        if not updated:
            raise _http_error(404, "user_not_found", "用户不存在")
        session = await call_store(request, "create_session", updated["id"])
        enriched = await enrich_private_user(request, updated)
        response = JSONResponse({"updated": True, "user": enriched})
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        return response

    @router.post(
        "/v1/me/email/change/request",
        tags=["user"],
        summary="申请换绑邮箱",
        description="验证当前密码后申请更换登录邮箱。开启邮箱验证时发送验证码，关闭时直接换绑并重置验证状态。",
    )
    async def email_change_request(
        request: Request,
        payload: EmailChangeRequestPayload,
    ) -> dict[str, Any]:
        user = await require_user(request)
        settings = await runtime_settings_for_app(request.app)
        if not str(user.get("password_hash") or "").strip():
            raise _http_error(
                400,
                "local_password_required",
                "请先设置本地密码后再修改或绑定登录邮箱",
            )
        if not verify_password(payload.current_password, str(user.get("password_hash") or "")):
            raise _http_error(400, "invalid_current_password", "当前密码不正确")

        new_email = clean_email(payload.new_email)
        if not new_email or not is_valid_email(new_email):
            raise _http_error(400, "invalid_email", "请输入有效的邮箱地址")
        old_email = clean_email(user.get("email"))
        if old_email and normalize_email(old_email) == normalize_email(new_email):
            raise _http_error(400, "email_unchanged", "新邮箱不能与当前邮箱相同")

        if not settings.email_verification_required:
            updated = await call_store(
                request,
                "update_user_email",
                user["id"],
                new_email,
                email_verified_at=None,
            )
            if not updated:
                raise _http_error(404, "user_not_found", "用户不存在")
            if old_email:
                subject, body = render_email_changed_notice(settings.site_name, new_email)
                await _send_email_best_effort(request, settings, old_email, subject, body)
            return {
                "accepted": True,
                "completed": True,
                "verification_required": False,
                "user": await enrich_private_user(request, updated),
            }

        if settings.email_provider == "disabled":
            raise _http_error(
                503,
                "email_service_unavailable",
                "邮件服务尚未启用，暂时无法通过验证码换绑邮箱",
            )

        try:
            reservation_key = await enforce_verification_request_limits(
                request.app.state.store,
                settings,
                purpose="email_change",
                email=new_email,
                client_ip=_client_ip(request),
            )
        except EmailChallengeError as exc:
            _raise_challenge_error(exc)

        existing = await call_store(request, "get_user_by_email", new_email)
        if existing and existing["id"] != user["id"]:
            await release_rate_limit_reservation(request.app.state.store, reservation_key)
            return {
                "accepted": True,
                "completed": False,
                "verification_required": True,
                "change_id": f"chg_{secrets.token_urlsafe(24)}",
                "expires_in": CHALLENGE_TTL_SECONDS,
            }

        challenge_id = ""
        try:
            challenge_id, code, expires_in = await create_email_challenge(
                request.app.state.store,
                purpose="email_change",
                email=new_email,
                payload={
                    "user_id": str(user["id"]),
                    "old_email": old_email,
                    "new_email": new_email,
                },
            )
            subject, body = render_verification_email(settings.site_name, "email_change", code)
            await send_email(request.app, settings, new_email, subject, body)
        except Exception:
            if challenge_id:
                await delete_email_challenge(request.app.state.store, challenge_id)
            await clear_verification_cooldown(
                request.app.state.store, purpose="email_change", email=new_email
            )
            await release_rate_limit_reservation(request.app.state.store, reservation_key)
            raise

        return {
            "accepted": True,
            "completed": False,
            "verification_required": True,
            "change_id": challenge_id,
            "expires_in": expires_in,
        }

    @router.post(
        "/v1/me/email/change/verify",
        tags=["user"],
        summary="验证并完成换绑邮箱",
        description="核验新邮箱验证码并原子换绑邮箱，完成后通知旧邮箱。",
    )
    async def email_change_verify(
        request: Request,
        payload: EmailChangeVerifyPayload,
    ) -> dict[str, Any]:
        user = await require_user(request)
        settings = await runtime_settings_for_app(request.app)

        try:
            challenge_payload = await reserve_email_challenge(
                request.app.state.store,
                challenge_id=payload.change_id,
                purpose="email_change",
                code=payload.code,
            )
        except EmailChallengeError as exc:
            _raise_challenge_error(exc)

        if str(challenge_payload.get("user_id") or "") != str(user["id"]):
            await release_email_challenge(request.app.state.store, payload.change_id)
            raise _http_error(403, "challenge_user_mismatch", "验证码与当前登录账号不匹配")

        new_email = str(challenge_payload["new_email"])
        old_email = str(challenge_payload.get("old_email") or "")
        try:
            updated = await call_store(
                request,
                "update_user_email",
                user["id"],
                new_email,
                email_verified_at=_utc_now(),
            )
        except UserIdentityConflict:
            await complete_email_challenge(request.app.state.store, payload.change_id)
            raise
        except Exception:
            await release_email_challenge(request.app.state.store, payload.change_id)
            raise

        await complete_email_challenge(request.app.state.store, payload.change_id)
        if not updated:
            raise _http_error(404, "user_not_found", "用户不存在")
        if old_email:
            subject, body = render_email_changed_notice(settings.site_name, new_email)
            await _send_email_best_effort(request, settings, old_email, subject, body)
        return {"changed": True, "user": await enrich_private_user(request, updated)}

    @router.get(
        "/v1/me/oauth-identities",
        tags=["user"],
        summary="获取已绑定的 OAuth 身份",
        description="返回当前账号已绑定的第三方 OAuth 身份列表及可解绑状态。",
    )
    async def my_oauth_identities(request: Request) -> dict[str, Any]:
        user = await require_user(request)
        identities = await list_active_oauth_identities(request.app.state.store, str(user["id"]))
        has_password = bool(str(user.get("password_hash") or "").strip())
        return {
            "items": [public_oauth_identity(item) for item in identities],
            "has_password": has_password,
            "can_unbind_any": has_password or len(identities) > 1,
        }

    @router.delete(
        "/v1/me/oauth-identities/{identity_id}",
        tags=["user"],
        summary="解绑 OAuth 身份",
        description="解绑指定第三方 OAuth 身份。未设置本地密码且仅剩最后一个 OAuth 时禁止解绑；不影响名下既有插件。",
    )
    async def unbind_my_oauth_identity(
        request: Request,
        identity_id: str,
    ) -> dict[str, Any]:
        user = await require_user(request)
        identities = await list_active_oauth_identities(request.app.state.store, str(user["id"]))
        allowed, reason = can_unbind_oauth_identity(user, identities, identity_id)
        if not allowed:
            if reason == "not_found":
                raise _http_error(404, "oauth_identity_not_found", "未找到该 OAuth 绑定记录")
            raise _http_error(
                409,
                "last_auth_method_protected",
                "请先设置本地密码或绑定其他 OAuth，再解绑当前登录方式",
            )
        revoked = await call_store(request, "revoke_oauth_identity", user["id"], identity_id)
        if not revoked:
            raise _http_error(404, "oauth_identity_not_found", "未找到该 OAuth 绑定记录")
        if revoked.get("provider") == "github":
            await revoke_github_identity_authorizations(
                request.app.state.store,
                request.app.state.settings,
                str(user["id"]),
                str(revoked.get("provider_user_id") or ""),
            )
        cred_ref = str(revoked.get("credential_ref") or "").strip()
        if cred_ref and hasattr(request.app.state.store, "delete_oauth_credential"):
            await call_store(request, "delete_oauth_credential", cred_ref)
        refreshed = await call_store(request, "get_user_by_id", user["id"]) or user
        return {
            "revoked": True,
            "identity": public_oauth_identity(revoked),
            "user": await enrich_private_user(request, refreshed),
        }

    return router
