"""Reusable email verification challenges, atomic consumption, and shared rate limits."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .oauth_identities import normalize_email

CHALLENGE_PURPOSES = frozenset({"register", "password_reset", "email_change"})
CHALLENGE_TTL_SECONDS = 600
CHALLENGE_MAX_ATTEMPTS = 5
CHALLENGE_RESEND_COOLDOWN_SECONDS = 60
CHALLENGE_IP_WINDOW_SECONDS = 600
CHALLENGE_IP_LIMIT = 15
DIRECT_REGISTER_IP_WINDOW_SECONDS = 600
DIRECT_REGISTER_IP_LIMIT = 10


class EmailChallengeError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


def generate_verification_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_verification_code(salt: str, purpose: str, code: str) -> str:
    message = f"{purpose}:{code.strip()}".encode("utf-8")
    return hmac.new(salt.encode("utf-8"), message, hashlib.sha256).hexdigest()


def seconds_until_utc_midnight() -> int:
    now = datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((tomorrow - now).total_seconds()))


def utc_day_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def render_verification_email(site_name: str, purpose: str, code: str) -> tuple[str, str]:
    brand = site_name.strip() or "Astrhub 插件市场"
    if purpose == "register":
        return (
            f"{brand} - 注册验证码",
            f"你的注册验证码为：{code}\n\n验证码 10 分钟内有效，请勿泄露给他人。如果不是本人操作，请忽略此邮件。",
        )
    if purpose == "password_reset":
        return (
            f"{brand} - 密码重置验证码",
            f"你的密码重置验证码为：{code}\n\n验证码 10 分钟内有效。完成重置后，该账号的全部已有登录会话将自动失效。如果不是本人操作，请忽略此邮件。",
        )
    if purpose == "email_change":
        return (
            f"{brand} - 换绑邮箱验证码",
            f"你正在申请将账号邮箱更换为此地址，验证码为：{code}\n\n验证码 10 分钟内有效。如果不是本人操作，请忽略此邮件。",
        )
    raise ValueError(f"Unsupported challenge purpose: {purpose}")


def render_email_changed_notice(site_name: str, new_email: str) -> tuple[str, str]:
    brand = site_name.strip() or "Astrhub 插件市场"
    return (
        f"{brand} - 账号邮箱已变更通知",
        f"你的账号登录邮箱已变更为：{new_email}。\n\n如果这不是你本人的操作，请尽快联系站点管理员处理。",
    )


async def _call_store(store: Any, method_name: str, *args: Any) -> Any:
    result = getattr(store, method_name)(*args)
    return await result if inspect.isawaitable(result) else result


async def enforce_site_email_daily_limit(store: Any, settings: Settings) -> None:
    limit = int(settings.email_daily_limit or 0)
    if limit <= 0:
        return
    key = f"email_daily_site:{utc_day_stamp()}"
    count = await _call_store(store, "get_rate_limit_count", key)
    if int(count or 0) >= limit:
        raise EmailChallengeError(
            429,
            "email_daily_limit_exceeded",
            "站点今日邮件发送额度已达上限，请明天再试",
            retry_after=seconds_until_utc_midnight(),
        )


async def record_site_email_sent(store: Any, settings: Settings) -> None:
    if int(settings.email_daily_limit or 0) <= 0:
        return
    key = f"email_daily_site:{utc_day_stamp()}"
    await _call_store(store, "increment_rate_limit", key, seconds_until_utc_midnight())


async def enforce_direct_register_rate_limit(store: Any, client_ip: str) -> None:
    ip = client_ip.strip() or "unknown"
    key = f"direct_register_ip:{ip}"
    count = await _call_store(store, "increment_rate_limit", key, DIRECT_REGISTER_IP_WINDOW_SECONDS)
    if count > DIRECT_REGISTER_IP_LIMIT:
        raise EmailChallengeError(
            429,
            "rate_limited",
            "注册请求过于频繁，请稍后再试",
            retry_after=DIRECT_REGISTER_IP_WINDOW_SECONDS,
        )


async def enforce_verification_request_limits(
    store: Any,
    settings: Settings,
    *,
    purpose: str,
    email: str,
    client_ip: str,
    check_cooldown: bool = True,
) -> None:
    normalized = normalize_email(email)
    ip = client_ip.strip() or "unknown"

    ip_key = f"email_challenge_ip:{purpose}:{ip}"
    ip_count = await _call_store(store, "increment_rate_limit", ip_key, CHALLENGE_IP_WINDOW_SECONDS)
    if ip_count > CHALLENGE_IP_LIMIT:
        raise EmailChallengeError(
            429,
            "rate_limited",
            "请求过于频繁，请稍后再试",
            retry_after=CHALLENGE_IP_WINDOW_SECONDS,
        )

    if check_cooldown:
        cooldown_key = f"email_challenge_cooldown:{purpose}:{normalized}"
        remaining = await _call_store(
            store,
            "check_and_set_cooldown",
            cooldown_key,
            CHALLENGE_RESEND_COOLDOWN_SECONDS,
        )
        if remaining > 0:
            raise EmailChallengeError(
                429,
                "verification_resend_cooldown",
                f"验证码发送过于频繁，请 {remaining} 秒后再试",
                retry_after=remaining,
            )

    daily_limit = int(settings.email_verification_daily_limit_per_user or 0)
    if daily_limit > 0:
        day_key = f"email_verification_daily:{utc_day_stamp()}:{normalized}"
        day_count = await _call_store(
            store,
            "increment_rate_limit",
            day_key,
            seconds_until_utc_midnight(),
        )
        if day_count > daily_limit:
            raise EmailChallengeError(
                429,
                "email_verification_daily_limit_exceeded",
                "该邮箱今日验证码发送次数已达上限，请明天再试",
                retry_after=seconds_until_utc_midnight(),
            )


async def create_email_challenge(
    store: Any,
    *,
    purpose: str,
    email: str,
    payload: dict[str, Any],
) -> tuple[str, str, int]:
    if purpose not in CHALLENGE_PURPOSES:
        raise ValueError(f"Unsupported challenge purpose: {purpose}")
    challenge_id = f"chg_{secrets.token_urlsafe(24)}"
    code = generate_verification_code()
    salt = secrets.token_hex(16)
    digest = hash_verification_code(salt, purpose, code)
    record = {
        "purpose": purpose,
        "email": normalize_email(email),
        "salt": salt,
        "code_digest": digest,
        "state": "pending",
        "attempts": 0,
        "payload": payload,
    }
    await _call_store(store, "save_email_challenge", challenge_id, record, CHALLENGE_TTL_SECONDS)
    return challenge_id, code, CHALLENGE_TTL_SECONDS


async def delete_email_challenge(store: Any, challenge_id: str) -> None:
    await _call_store(store, "delete_email_challenge", challenge_id)


async def clear_verification_cooldown(store: Any, *, purpose: str, email: str) -> None:
    if not hasattr(store, "clear_cooldown"):
        return
    cooldown_key = f"email_challenge_cooldown:{purpose}:{normalize_email(email)}"
    await _call_store(store, "clear_cooldown", cooldown_key)


async def reserve_email_challenge(
    store: Any,
    *,
    challenge_id: str,
    purpose: str,
    code: str,
) -> dict[str, Any]:
    clean_id = str(challenge_id or "").strip()
    clean_code = str(code or "").strip()
    if not clean_id or not clean_code:
        raise EmailChallengeError(400, "invalid_verification_code", "验证码无效或已过期")
    salt = await _call_store(store, "get_email_challenge_salt", clean_id)
    if not salt:
        raise EmailChallengeError(400, "challenge_expired", "验证码无效或已过期")
    expected_digest = hash_verification_code(salt, purpose, clean_code)
    status, payload = await _call_store(
        store,
        "reserve_email_challenge",
        clean_id,
        purpose,
        expected_digest,
        CHALLENGE_MAX_ATTEMPTS,
    )
    if status == "ok" and isinstance(payload, dict):
        return payload
    if status == "too_many_attempts":
        raise EmailChallengeError(
            429,
            "verification_attempts_exceeded",
            "验证码错误次数过多，请重新获取验证码",
        )
    if status == "in_progress":
        raise EmailChallengeError(409, "challenge_in_progress", "验证正在处理中，请勿重复提交")
    if status == "invalid_code":
        raise EmailChallengeError(400, "invalid_verification_code", "验证码错误，请重试")
    raise EmailChallengeError(400, "challenge_expired", "验证码无效或已过期")


async def complete_email_challenge(store: Any, challenge_id: str) -> None:
    await _call_store(store, "delete_email_challenge", challenge_id)


async def release_email_challenge(store: Any, challenge_id: str) -> None:
    await _call_store(store, "release_email_challenge", challenge_id)
