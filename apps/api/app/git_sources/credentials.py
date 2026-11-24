"""Independent OAuth credential encryption and persistent CredentialStore."""

from __future__ import annotations

import base64
import inspect
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException

from .models import OAuthTokenSet

DEFAULT_CREDENTIAL_KDF_INFO = b"astrhub/oauth-credential/v1"
MIN_MASTER_KEY_LENGTH = 16


def _credential_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "error": message})


async def _store_call(store: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    fn = getattr(store, method)
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def require_oauth_credential_key(master_key: str | None) -> str:
    cleaned = str(master_key or "").strip()
    if len(cleaned) < MIN_MASTER_KEY_LENGTH:
        raise _credential_error(
            503,
            "oauth_credential_key_unconfigured",
            "OAuth 凭据加密主密钥（OAUTH_CREDENTIAL_KEY）尚未配置或长度不足",
        )
    return cleaned


def oauth_credential_cipher(
    master_key: str | None,
    *,
    context: bytes = DEFAULT_CREDENTIAL_KDF_INFO,
) -> Fernet:
    key_material = require_oauth_credential_key(master_key)
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=context,
    ).derive(key_material.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


class CredentialStore:
    """Encrypts and persists OAuth access/refresh tokens using OAUTH_CREDENTIAL_KEY."""

    def __init__(self, store: Any, master_key: str | None) -> None:
        self.store = store
        self.master_key = master_key

    def _cipher(self, provider: str) -> Fernet:
        normalized = str(provider or "").strip().lower()
        return oauth_credential_cipher(
            self.master_key,
            context=f"astrhub/oauth-credential/{normalized}/v1".encode("utf-8"),
        )

    async def save_token_set(
        self,
        *,
        user_id: str,
        token_set: OAuthTokenSet,
        credential_ref: str = "",
    ) -> str:
        ref = str(credential_ref or "").strip() or f"cred_{secrets.token_hex(16)}"
        payload = json.dumps(
            {
                "user_id": str(user_id),
                "provider": token_set.provider,
                "access_token": token_set.access_token,
                "refresh_token": token_set.refresh_token,
                "scope": token_set.scope,
                "expires_at": token_set.expires_at,
                "provider_user_id": token_set.provider_user_id,
                "provider_login": token_set.provider_login,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = self._cipher(token_set.provider).encrypt(payload).decode("ascii")
        await _store_call(
            self.store,
            "upsert_oauth_credential",
            ref,
            user_id=str(user_id),
            provider=token_set.provider,
            ciphertext=ciphertext,
            expires_at=token_set.expires_at,
        )
        return ref

    async def load_token_set(
        self,
        credential_ref: str,
        *,
        expected_user_id: str,
        expected_provider: str,
    ) -> OAuthTokenSet | None:
        ref = str(credential_ref or "").strip()
        if not ref:
            return None
        record = await _store_call(self.store, "get_oauth_credential", ref)
        if not record:
            return None
        ciphertext = str(record.get("ciphertext") or "")
        if not ciphertext:
            return None
        cipher = self._cipher(expected_provider)
        try:
            raw = cipher.decrypt(ciphertext.encode("ascii"))
            data = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, UnicodeDecodeError):
            await self.revoke(ref)
            return None
        if (
            str(data.get("user_id") or "") != str(expected_user_id)
            or str(data.get("provider") or "").lower() != str(expected_provider).lower()
            or not str(data.get("access_token") or "").strip()
        ):
            await self.revoke(ref)
            return None
        return OAuthTokenSet(
            provider=str(data["provider"]).lower(),
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or ""),
            scope=str(data.get("scope") or ""),
            expires_at=data.get("expires_at"),
            provider_user_id=str(data.get("provider_user_id") or ""),
            provider_login=str(data.get("provider_login") or ""),
        )

    async def revoke(self, credential_ref: str) -> bool:
        ref = str(credential_ref or "").strip()
        if not ref or not hasattr(self.store, "delete_oauth_credential"):
            return False
        return bool(await _store_call(self.store, "delete_oauth_credential", ref))
