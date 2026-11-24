"""Base allowlisted HTTPS OAuth provider adapter and shared metadata/archive helpers."""

from __future__ import annotations

import base64
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
import yaml

from .models import (
    ALLOWED_API_HOSTS_BY_PROVIDER,
    GitSourceError,
    OAuthTokenSet,
    OAuthUserProfile,
    RepositoryProof,
    RepositoryRef,
    ResolvedGitArchiveSource,
)

README_CANDIDATES: tuple[str, ...] = (
    "README.md",
    "Readme.md",
    "readme.md",
    "README.MD",
    "README",
)
CHANGELOG_CANDIDATES: tuple[str, ...] = (
    "CHANGELOG.md",
    "Changelog.md",
    "changelog.md",
    "CHANGELOG.MD",
    "CHANGELOG",
    "changelog",
)
PLUGIN_I18N_LOCALE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
PLUGIN_I18N_MAX_FILES = 16
PLUGIN_I18N_MAX_BYTES = 1_000_000
MAX_TEXT_FILE_BYTES = 1_000_000
COMMIT_SHA_PATTERN = re.compile(r"^[a-f0-9]{40}$")
PLUGIN_METADATA_KEYS: tuple[str, ...] = (
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
    "support_platforms",
)


@dataclass(frozen=True, slots=True)
class ProviderOAuthConfig:
    provider: str
    enabled: bool
    client_id: str
    client_secret: str
    callback_url: str
    scope: str
    api_base_url: str = ""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def expires_at_from_seconds(expires_in: Any) -> str | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def parse_metadata_yaml_bytes(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_TEXT_FILE_BYTES:
        return {}
    try:
        loaded = yaml.safe_load(raw.decode("utf-8", errors="replace"))
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    result: dict[str, Any] = {}
    for key in PLUGIN_METADATA_KEYS:
        if key in loaded and loaded[key] is not None:
            result[key] = loaded[key]
    if "description" in loaded and not result.get("desc"):
        result["desc"] = loaded["description"]
    if "repo" in loaded and loaded["repo"]:
        result["repo"] = loaded["repo"]
    return result


def parse_i18n_metadata_json(raw: bytes) -> dict[str, str]:
    import json

    if not raw or len(raw) > PLUGIN_I18N_MAX_BYTES:
        return {}
    try:
        loaded = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    section = loaded.get("metadata") if isinstance(loaded.get("metadata"), dict) else loaded
    translated: dict[str, str] = {}
    for field in ("display_name", "desc", "short_desc"):
        value = str(section.get(field) or "").strip()
        if value:
            translated[field] = value
    return translated


def decode_base64_content(encoded: str) -> bytes:
    cleaned = re.sub(r"\s+", "", encoded or "")
    if not cleaned:
        return b""
    try:
        return base64.b64decode(cleaned)
    except ValueError:
        return b""


class BaseGitProviderAdapter(ABC):
    provider: str = ""
    label: str = ""
    instance_url: str = ""
    authorize_url: str = ""
    token_url: str = ""

    def __init__(
        self,
        config: ProviderOAuthConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return ALLOWED_API_HOSTS_BY_PROVIDER[self.provider]

    def build_authorize_url(self, *, state: str) -> str:
        params = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.callback_url,
                "response_type": "code",
                "scope": self.config.scope,
                "state": state,
            }
        )
        return f"{self.authorize_url}?{params}"

    def _validate_target_url(self, url: str) -> str:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise GitSourceError(
                f"{self.provider}_redirect_blocked",
                f"{self.label} 请求或重定向目标端口格式无效",
                status_code=503,
            ) from exc
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() != "https"
            or hostname not in self.allowed_hosts
            or port not in {None, 443}
        ):
            raise GitSourceError(
                f"{self.provider}_redirect_blocked",
                f"{self.label} 请求或重定向目标不在允许的白名单域名内",
                status_code=503,
            )
        return url

    async def safe_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> httpx.Response:
        current_url = self._validate_target_url(url)
        current_headers = dict(headers or {})
        current_params = dict(params) if params else None
        initial_host = (urlsplit(current_url).hostname or "").lower()
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=timeout,
                follow_redirects=False,
            ) as client:
                response: httpx.Response | None = None
                for _ in range(5):
                    response = await client.request(
                        method,
                        current_url,
                        headers=current_headers,
                        params=current_params,
                        data=data,
                        json=json_body,
                    )
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        return response
                    location = response.headers.get("location", "").strip()
                    if not location:
                        break
                    location = urljoin(current_url, location)
                    next_url = self._validate_target_url(location)
                    next_host = (urlsplit(next_url).hostname or "").lower()
                    if next_host != initial_host:
                        current_headers.pop("Authorization", None)
                        current_headers.pop("authorization", None)
                    current_url = next_url
                    current_params = None
                    if response.status_code == 303:
                        method, data, json_body = "GET", None, None
                raise GitSourceError(
                    f"{self.provider}_redirect_limit",
                    f"{self.label} 返回过多重定向",
                    status_code=503,
                    retryable=True,
                )
        except GitSourceError:
            raise
        except httpx.TimeoutException as exc:
            raise GitSourceError(
                f"{self.provider}_timeout",
                f"{self.label} 请求超时，请稍后重试",
                status_code=503,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise GitSourceError(
                f"{self.provider}_unavailable",
                f"{self.label} 服务暂时不可用，请稍后重试",
                status_code=503,
                retryable=True,
            ) from exc

    async def exchange_code(self, code: str) -> OAuthTokenSet:
        response = await self.safe_request(
            "POST",
            self.token_url,
            headers={"Accept": "application/json", "User-Agent": "Astrhub-OAuth"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "redirect_uri": self.config.callback_url,
            },
        )
        return self._parse_token_response(response)

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        if not refresh_token:
            raise GitSourceError(
                f"{self.provider}_authorization_required",
                f"{self.label} 授权已过期，请重新连接",
                status_code=409,
            )
        response = await self.safe_request(
            "POST",
            self.token_url,
            headers={"Accept": "application/json", "User-Agent": "Astrhub-OAuth"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "redirect_uri": self.config.callback_url,
            },
        )
        token_set = self._parse_token_response(response, fallback_refresh_token=refresh_token)
        return token_set

    def _parse_token_response(
        self,
        response: httpx.Response,
        *,
        fallback_refresh_token: str = "",
    ) -> OAuthTokenSet:
        if response.status_code in {400, 401, 403}:
            raise GitSourceError(
                f"{self.provider}_authorization_required",
                f"{self.label} OAuth 令牌换取或刷新失败，请重新授权",
                status_code=409,
            )
        if response.status_code != 200:
            raise GitSourceError(
                f"{self.provider}_unavailable",
                f"{self.label} 令牌服务返回异常状态（HTTP {response.status_code}）",
                status_code=503,
                retryable=True,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitSourceError(
                f"{self.provider}_unavailable",
                f"{self.label} 返回的令牌响应无效",
                status_code=503,
            ) from exc
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise GitSourceError(
                f"{self.provider}_authorization_required",
                f"{self.label} 未返回有效的访问令牌",
                status_code=409,
            )
        refresh_token = str(payload.get("refresh_token") or "").strip() or fallback_refresh_token
        return OAuthTokenSet(
            provider=self.provider,
            access_token=access_token,
            refresh_token=refresh_token,
            scope=str(payload.get("scope") or self.config.scope),
            expires_at=expires_at_from_seconds(payload.get("expires_in")),
        )

    async def stream_archive(
        self,
        source: ResolvedGitArchiveSource,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes]:
        current_url = self._validate_target_url(source.archive_url)
        current_headers = {"User-Agent": "Astrhub-ArtifactWorker", **source.headers}
        initial_host = (urlsplit(current_url).hostname or "").lower()
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(30.0, read=120.0),
                follow_redirects=False,
            ) as client:
                for _ in range(5):
                    async with client.stream("GET", current_url, headers=current_headers) as resp:
                        if resp.status_code in {301, 302, 303, 307, 308}:
                            location = resp.headers.get("location", "").strip()
                            if not location:
                                break
                            location = urljoin(current_url, location)
                            next_url = self._validate_target_url(location)
                            if (urlsplit(next_url).hostname or "").lower() != initial_host:
                                current_headers.pop("Authorization", None)
                                current_headers.pop("authorization", None)
                            current_url = next_url
                            continue
                        if resp.status_code == 401:
                            raise GitSourceError(
                                f"{self.provider}_authorization_required",
                                f"{self.label} 授权已失效，无法下载归档",
                                status_code=409,
                            )
                        if resp.status_code == 404:
                            raise GitSourceError(
                                f"{self.provider}_archive_not_found",
                                f"{self.label} 未找到对应提交的源码归档",
                                status_code=404,
                            )
                        if resp.status_code != 200:
                            raise GitSourceError(
                                f"{self.provider}_archive_unavailable",
                                f"{self.label} 归档下载失败（HTTP {resp.status_code}）",
                                status_code=503,
                                retryable=resp.status_code >= 500 or resp.status_code == 429,
                            )
                        async for chunk in resp.aiter_bytes(chunk_size):
                            if chunk:
                                yield chunk
                        return
                raise GitSourceError(
                    f"{self.provider}_redirect_limit",
                    f"{self.label} 归档下载重定向次数过多",
                    status_code=503,
                    retryable=True,
                )
        except GitSourceError:
            raise
        except httpx.TimeoutException as exc:
            raise GitSourceError(
                f"{self.provider}_archive_timeout",
                f"{self.label} 归档下载超时",
                status_code=503,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise GitSourceError(
                f"{self.provider}_archive_unavailable",
                f"{self.label} 归档下载失败",
                status_code=503,
                retryable=True,
            ) from exc

    @abstractmethod
    async def fetch_user_profile(self, access_token: str) -> OAuthUserProfile: ...

    @abstractmethod
    async def list_manageable_repositories(
        self,
        access_token: str,
        *,
        page: int = 1,
        submitted_normalized_repos: set[str] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def verify_repository_access(
        self,
        ref: RepositoryRef,
        *,
        user_id: str,
        token_set: OAuthTokenSet,
        credential_ref: str = "",
        expected_repository_id: str = "",
    ) -> RepositoryProof: ...

    @abstractmethod
    async def fetch_repository_metadata(
        self,
        ref: RepositoryRef,
        *,
        access_token: str = "",
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def fetch_readme_document(
        self,
        ref: RepositoryRef,
        *,
        path: str = "",
        access_token: str = "",
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    async def resolve_archive_source(
        self,
        ref: RepositoryRef,
        *,
        access_token: str = "",
        requested_ref: str = "",
    ) -> ResolvedGitArchiveSource: ...
