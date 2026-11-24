"""HKDF-isolated credential encryption for storage profiles reusing OAUTH_CREDENTIAL_KEY."""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Mapping
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .models import ObjectStorageError, validate_profile_id

MIN_MASTER_KEY_LENGTH = 16
STORAGE_CREDENTIAL_KDF_PREFIX = "astrhub/storage-credential"


def require_storage_credential_key(master_key: str | None) -> str:
    cleaned = str(master_key or "").strip()
    if len(cleaned) < MIN_MASTER_KEY_LENGTH:
        raise ObjectStorageError(
            "storage_credential_key_unconfigured",
            "存储凭据加密主密钥（OAUTH_CREDENTIAL_KEY）尚未配置或长度不足",
        )
    return cleaned


def derive_storage_credential_cipher(master_key: str | None, *, profile_id: str) -> Fernet:
    key_material = require_storage_credential_key(master_key)
    normalized_id = validate_profile_id(profile_id)
    context = f"{STORAGE_CREDENTIAL_KDF_PREFIX}/{normalized_id}/v1".encode("utf-8")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=context,
    ).derive(key_material.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_storage_secret(
    master_key: str | None,
    *,
    profile_id: str,
    secret_payload: Mapping[str, Any],
) -> str:
    normalized_id = validate_profile_id(profile_id)
    envelope = {
        "profile_id": normalized_id,
        "secret": dict(secret_payload),
    }
    raw = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    cipher = derive_storage_credential_cipher(master_key, profile_id=normalized_id)
    return cipher.encrypt(raw).decode("ascii")


def decrypt_storage_secret(
    master_key: str | None,
    *,
    profile_id: str,
    ciphertext: str,
) -> dict[str, Any]:
    normalized_id = validate_profile_id(profile_id)
    token = str(ciphertext or "").strip()
    if not token:
        raise ObjectStorageError(
            "storage_credential_missing", "Storage credential ciphertext is empty"
        )
    cipher = derive_storage_credential_cipher(master_key, profile_id=normalized_id)
    try:
        raw = cipher.decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise ObjectStorageError(
            "storage_credential_decrypt_failed",
            "存储凭据解密失败，请检查 OAUTH_CREDENTIAL_KEY 配置是否变更",
        ) from exc
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ObjectStorageError(
            "storage_credential_corrupted", "Storage credential payload is corrupted"
        ) from exc
    if not isinstance(envelope, dict) or envelope.get("profile_id") != normalized_id:
        raise ObjectStorageError(
            "storage_credential_profile_mismatch", "Storage credential does not match profile"
        )
    secret = envelope.get("secret")
    if not isinstance(secret, dict):
        raise ObjectStorageError(
            "storage_credential_corrupted", "Storage credential payload is invalid"
        )
    return dict(secret)


class StorageSecretStore(Protocol):
    def save_secret(
        self,
        *,
        profile_id: str,
        secret_payload: Mapping[str, Any],
        secret_ref: str = "",
    ) -> str: ...

    def load_secret(self, secret_ref: str, *, profile_id: str) -> dict[str, Any] | None: ...

    def delete_secret(self, secret_ref: str) -> bool: ...


class InMemoryStorageSecretStore:
    """In-memory encrypted secret store for Stage 1 before database persistence lands."""

    def __init__(self, master_key: str | None) -> None:
        self.master_key = master_key
        self._records: dict[str, tuple[str, str]] = {}

    def save_secret(
        self,
        *,
        profile_id: str,
        secret_payload: Mapping[str, Any],
        secret_ref: str = "",
    ) -> str:
        normalized_id = validate_profile_id(profile_id)
        ref = str(secret_ref or "").strip() or f"storage_secret_{secrets.token_hex(16)}"
        ciphertext = encrypt_storage_secret(
            self.master_key,
            profile_id=normalized_id,
            secret_payload=secret_payload,
        )
        self._records[ref] = (normalized_id, ciphertext)
        return ref

    def load_secret(self, secret_ref: str, *, profile_id: str) -> dict[str, Any] | None:
        ref = str(secret_ref or "").strip()
        record = self._records.get(ref)
        if record is None:
            return None
        stored_profile_id, ciphertext = record
        if stored_profile_id != validate_profile_id(profile_id):
            raise ObjectStorageError(
                "storage_credential_profile_mismatch", "Storage credential does not match profile"
            )
        return decrypt_storage_secret(
            self.master_key,
            profile_id=profile_id,
            ciphertext=ciphertext,
        )

    def delete_secret(self, secret_ref: str) -> bool:
        ref = str(secret_ref or "").strip()
        return self._records.pop(ref, None) is not None
