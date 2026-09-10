"""Ask the API to recheck user access without giving OAuth credentials to the worker."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx


class PublicationAuthorizationError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.retryable = code, retryable


class PublicationAuthorizationClient:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    async def verify(self, artifact: Mapping[str, Any]) -> dict[str, str]:
        if not self.url or not self.token:
            raise PublicationAuthorizationError(
                "github_authorization_unavailable", "发布权限检查尚未配置"
            )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.url}/v1/internal/artifacts/{quote(str(artifact['id']), safe='')}/authorize-publication",
                    headers={"Authorization": "Bearer " + self.token},
                )
        except httpx.HTTPError:
            raise PublicationAuthorizationError(
                "github_authorization_unavailable", "暂时无法核验发布权限", retryable=True
            ) from None
        try:
            data = (
                response.json()
                if "application/json" in response.headers.get("content-type", "")
                else {}
            )
            if not isinstance(data, dict):
                raise ValueError("invalid authorization response")
        except ValueError:
            raise PublicationAuthorizationError(
                "github_authorization_unavailable", "发布权限服务返回了无效响应", retryable=True
            ) from None
        if response.status_code != 200:
            raise PublicationAuthorizationError(
                str(data.get("code") or "github_authorization_denied"),
                str(data.get("error") or "发布权限检查未通过"),
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        if (
            data.get("artifact_id") != artifact["id"]
            or data.get("archive_sha256") != artifact["archive_sha256"]
        ):
            raise PublicationAuthorizationError(
                "github_authorization_invalid", "发布权限凭据与插件版本不一致"
            )
        return data
