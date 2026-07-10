from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .archive import normalize_github_repo

GITHUB_API_BASE = "https://api.github.com"
GITHUB_ARCHIVE_BASE = "https://codeload.github.com"
COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")


class GithubSourceError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ResolvedGithubSource:
    repo_url: str
    owner: str
    repo_name: str
    requested_ref: str
    commit_sha: str

    @property
    def archive_url(self) -> str:
        owner = quote(self.owner, safe="")
        repo = quote(self.repo_name, safe="")
        commit = quote(self.commit_sha, safe="")
        return f"{GITHUB_ARCHIVE_BASE}/{owner}/{repo}/zip/{commit}"


class GithubSourceClient:
    def __init__(self, token: str = "", client: httpx.AsyncClient | None = None) -> None:
        headers = {
            "accept": "application/vnd.github+json",
            "user-agent": "AstrBot-Community-Plugins/ArtifactWorker",
            "x-github-api-version": "2022-11-28",
        }
        if token:
            headers["authorization"] = f"Bearer {token}"
        self.client = client or httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,
        )
        self._owns_client = client is None
        self.archive_client = httpx.AsyncClient(
            headers={"user-agent": "AstrBot-Community-Plugins/ArtifactWorker"},
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
        await self.archive_client.aclose()

    async def resolve(self, repo_url: str, requested_ref: str = "") -> ResolvedGithubSource:
        canonical = normalize_github_repo(repo_url)
        owner, repo_name = canonical.removeprefix("https://github.com/").split("/", 1)
        repository = await self._get_json(f"/repos/{owner}/{repo_name}")
        if bool(repository.get("private")):
            raise GithubSourceError("private_repo_not_supported", "P1 仅支持公开 GitHub 仓库")
        default_branch = str(repository.get("default_branch") or "").strip()
        target_ref = requested_ref.strip() or default_branch
        if not target_ref:
            raise GithubSourceError("github_default_branch_missing", "GitHub 仓库缺少默认分支")
        commit = await self._get_json(
            f"/repos/{owner}/{repo_name}/commits/{quote(target_ref, safe='')}"
        )
        commit_sha = str(commit.get("sha") or "").lower()
        if not COMMIT_PATTERN.fullmatch(commit_sha):
            raise GithubSourceError("github_commit_invalid", "GitHub 未返回有效 commit SHA")
        return ResolvedGithubSource(
            repo_url=canonical,
            owner=owner,
            repo_name=repo_name,
            requested_ref=target_ref,
            commit_sha=commit_sha,
        )

    async def stream_archive(
        self, source: ResolvedGithubSource, *, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        try:
            async with self.archive_client.stream("GET", source.archive_url) as response:
                _raise_for_status(response)
                async for chunk in response.aiter_bytes(chunk_size):
                    if chunk:
                        yield chunk
        except httpx.TimeoutException as exc:
            raise GithubSourceError(
                "github_archive_timeout", "GitHub 插件包下载超时", retryable=True
            ) from exc
        except httpx.TransportError as exc:
            raise GithubSourceError(
                "github_archive_unavailable", "GitHub 插件包下载失败", retryable=True
            ) from exc

    async def _get_json(self, path: str) -> dict:
        try:
            response = await self.client.get(f"{GITHUB_API_BASE}{path}")
        except httpx.TimeoutException as exc:
            raise GithubSourceError(
                "github_api_timeout", "GitHub API 请求超时", retryable=True
            ) from exc
        except httpx.TransportError as exc:
            raise GithubSourceError(
                "github_api_unavailable", "GitHub API 暂时不可用", retryable=True
            ) from exc
        _raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise GithubSourceError("github_response_invalid", "GitHub API 响应格式无效")
        return payload


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    if response.status_code == 404:
        raise GithubSourceError("github_source_not_found", "GitHub 仓库、引用或提交不存在")
    if response.status_code in {401, 403}:
        remaining = response.headers.get("x-ratelimit-remaining")
        code = "github_rate_limited" if remaining == "0" else "github_access_denied"
        raise GithubSourceError(code, "GitHub 拒绝访问或已达到速率限制", retryable=True)
    retryable = response.status_code == 429 or response.status_code >= 500
    raise GithubSourceError(
        "github_request_failed",
        f"GitHub 请求失败（HTTP {response.status_code}）",
        retryable=retryable,
    )
