"""Independent OAuth credential encryption and persistent CredentialStore."""

from __future__ import annotations

import base64
import inspect
import json
import logging
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException

from .models import OAuthTokenSet

logger = logging.getLogger(__name__)

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

    def decrypt_record(
        self,
        record: dict[str, Any] | None,
        *,
        expected_user_id: str,
        expected_provider: str,
    ) -> OAuthTokenSet | None:
        if not record:
            return None
        ref = str(record.get("credential_ref") or "").strip()
        ciphertext = str(record.get("ciphertext") or "")
        if not ciphertext:
            return None
        cipher = self._cipher(expected_provider)
        try:
            raw = cipher.decrypt(ciphertext.encode("ascii"))
        except InvalidToken as exc:
            logger.warning(
                "Failed to decrypt OAuth credential %s for provider %s; preserving stored row",
                ref,
                expected_provider,
            )
            raise _credential_error(
                503,
                "oauth_credential_decrypt_failed",
                "OAuth 凭据解密失败，请检查服务端 OAUTH_CREDENTIAL_KEY 配置是否变更",
            ) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if (
            str(data.get("user_id") or "") != str(expected_user_id)
            or str(data.get("provider") or "").lower() != str(expected_provider).lower()
            or not str(data.get("access_token") or "").strip()
        ):
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
        return self.decrypt_record(
            record,
            expected_user_id=expected_user_id,
            expected_provider=expected_provider,
        )

    async def rebind_user_credentials(self, *, from_user_id: str, to_user_id: str) -> int:
        if not hasattr(self.store, "list_oauth_credentials_by_user"):
            return 0
        records = await _store_call(self.store, "list_oauth_credentials_by_user", str(to_user_id))
        if not records:
            return 0
        updated = 0
        for record in records:
            ref = str(record.get("credential_ref") or "").strip()
            provider = str(record.get("provider") or "").strip().lower()
            ciphertext = str(record.get("ciphertext") or "")
            if not ref or not provider or not ciphertext:
                continue
            cipher = self._cipher(provider)
            try:
                raw = cipher.decrypt(ciphertext.encode("ascii"))
            except InvalidToken as exc:
                logger.warning(
                    "Failed to decrypt OAuth credential %s during user merge; preserving stored row",
                    ref,
                )
                raise _credential_error(
                    503,
                    "oauth_credential_decrypt_failed",
                    "OAuth 凭据解密失败，请检查服务端 OAUTH_CREDENTIAL_KEY 配置是否变更",
                ) from exc
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if str(data.get("user_id") or "") != str(from_user_id):
                continue
            data["user_id"] = str(to_user_id)
            reencrypted = cipher.encrypt(
                json.dumps(data, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            await _store_call(
                self.store,
                "upsert_oauth_credential",
                ref,
                user_id=str(to_user_id),
                provider=provider,
                ciphertext=reencrypted,
                expires_at=record.get("expires_at"),
            )
            updated += 1
        return updated

    async def revoke(self, credential_ref: str) -> bool:
        ref = str(credential_ref or "").strip()
        if not ref or not hasattr(self.store, "delete_oauth_credential"):
            return False
        return bool(await _store_call(self.store, "delete_oauth_credential", ref))
