from __future__ import annotations

import html
from contextlib import suppress
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any
from urllib.parse import quote

import aiosmtplib
import httpx

from ..config import Settings, normalize_smtp_auth_method, normalize_smtp_encryption

CLOUDFLARE_EMAIL_SEND_ENDPOINT = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/email/sending/send"
)


class ArtifactMailError(RuntimeError):
    pass


async def send_artifact_status_email(
    settings: Settings,
    *,
    receiver: str,
    subject: str,
    content: str,
) -> None:
    if settings.email_provider == "disabled":
        return
    if settings.email_provider == "smtp":
        await _send_smtp(settings, receiver, subject, content)
        return
    if settings.email_provider == "cloudflare":
        await _send_cloudflare(settings, receiver, subject, content)
        return
    raise ArtifactMailError("Unsupported email provider")


async def _send_smtp(settings: Settings, receiver: str, subject: str, content: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise ArtifactMailError("SMTP is not fully configured")
    message = EmailMessage()
    message["From"] = formataddr(
        (
            settings.smtp_from_name or "AstrBot Community Plugins",
            settings.smtp_from,
        )
    )
    message["To"] = receiver
    message["Subject"] = subject[:998]
    message.set_content(content)
    encryption = normalize_smtp_encryption(settings.smtp_encryption)
    options: dict[str, Any] = {
        "hostname": settings.smtp_host,
        "port": settings.smtp_port,
        "timeout": 10,
        "use_tls": encryption == "ssl_tls",
        "validate_certs": settings.smtp_validate_certs,
    }
    if encryption == "starttls":
        options["start_tls"] = True
    elif encryption == "none":
        options["start_tls"] = False
    client = aiosmtplib.SMTP(**options)
    try:
        await client.connect()
        await _authenticate_smtp(client, settings)
        await client.send_message(message)
    except (aiosmtplib.errors.SMTPException, OSError, TimeoutError, ValueError) as exc:
        raise ArtifactMailError("SMTP status email delivery failed") from exc
    finally:
        if client.is_connected:
            with suppress(aiosmtplib.errors.SMTPException, OSError, TimeoutError):
                await client.quit()


async def _authenticate_smtp(client: aiosmtplib.SMTP, settings: Settings) -> None:
    method = normalize_smtp_auth_method(settings.smtp_auth_method)
    if method == "none" or not settings.smtp_username:
        return
    if method == "login":
        await client.auth_login(settings.smtp_username, settings.smtp_password)
    elif method == "plain":
        await client.auth_plain(settings.smtp_username, settings.smtp_password)
    else:
        await client.login(settings.smtp_username, settings.smtp_password)


async def _send_cloudflare(settings: Settings, receiver: str, subject: str, content: str) -> None:
    if not all(
        (
            settings.cloudflare_email_account_id,
            settings.cloudflare_email_api_token,
            settings.cloudflare_email_from,
        )
    ):
        raise ArtifactMailError("Cloudflare email is not fully configured")
    endpoint = CLOUDFLARE_EMAIL_SEND_ENDPOINT.format(
        account_id=quote(settings.cloudflare_email_account_id, safe="")
    )
    payload = {
        "to": receiver,
        "from": {
            "email": settings.cloudflare_email_from,
            "name": settings.cloudflare_email_from_name or "AstrBot Community Plugins",
        },
        "subject": subject[:998],
        "text": content,
        "html": html.escape(content).replace("\n", "<br>"),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                endpoint,
                headers={
                    "authorization": f"Bearer {settings.cloudflare_email_api_token}",
                    "content-type": "application/json",
                },
                json=payload,
            )
    except httpx.TransportError as exc:
        raise ArtifactMailError("Cloudflare status email delivery failed") from exc
    data = response.json() if response.content else {}
    if response.status_code >= 400 or not isinstance(data, dict) or not data.get("success", False):
        raise ArtifactMailError(f"Cloudflare status email failed: HTTP {response.status_code}")
