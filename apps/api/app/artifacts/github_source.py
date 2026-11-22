from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .archive import normalize_github_repo

GITHUB_API_BASE = "https://api.github.com"
GITHUB_ARCHIVE_BASE = "https://codeload.github.com"
GITHUB_DOWNLOAD_HOSTS = frozenset({"github.com", "www.github.com"})
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
    download_url: str = ""
    release_tag: str = ""
    release_asset_name: str = ""

    @property
    def archive_url(self) -> str:
        if self.download_url:
            return self.download_url
        owner = quote(self.owner, safe="")
        repo = quote(self.repo_name, safe="")
        commit = quote(self.commit_sha, safe="")
        return f"{GITHUB_ARCHIVE_BASE}/{owner}/{repo}/zip/{commit}"


def is_formal_release(release: Mapping[str, Any]) -> bool:
    tag_name = str(release.get("tag_name") or "").strip()
    return bool(tag_name) and not bool(release.get("draft")) and not bool(release.get("prerelease"))


def select_release_zip_asset(
    release: Mapping[str, Any],
    *,
    owner: str,
    repo_name: str,
) -> tuple[str, str] | None:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    candidates: list[tuple[str, str]] = []
    for item in assets:
        if not isinstance(item, Mapping):
            continue
        state = str(item.get("state") or "uploaded").strip().lower()
        if state != "uploaded":
            continue
        name = str(item.get("name") or "").strip()
        if not name.lower().endswith(".zip"):
            continue
        url = str(item.get("browser_download_url") or "").strip()
        if not _is_valid_release_asset_url(url, owner=owner, repo_name=repo_name):
            continue
        candidates.append((url, name))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    repo_matches = [item for item in candidates if repo_name.casefold() in item[1].casefold()]
    if len(repo_matches) == 1:
        return repo_matches[0]
    narrowed = repo_matches or candidates

    tag_name = str(release.get("tag_name") or "").strip()
    tag_tokens = {
        token for token in (tag_name.casefold(), tag_name.lstrip("vV").casefold()) if token
    }
    if tag_tokens:
        tag_matches = [
            item for item in narrowed if any(token in item[1].casefold() for token in tag_tokens)
        ]
        if len(tag_matches) == 1:
            return tag_matches[0]

    raise GithubSourceError(
        "github_release_asset_ambiguous",
        "GitHub Release 包含多个无法自动区分的 ZIP 附件",
    )


def _is_valid_release_asset_url(url: str, *, owner: str, repo_name: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in GITHUB_DOWNLOAD_HOSTS:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 6:
        return False
    return (
        parts[0].casefold() == owner.casefold()
        and parts[1].casefold() == repo_name.casefold()
        and parts[2] == "releases"
        and parts[3] == "download"
    )


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
        explicit_ref = requested_ref.strip()
        if not explicit_ref:
            release = await self._get_optional_json(f"/repos/{owner}/{repo_name}/releases/latest")
            if release and is_formal_release(release):
                return await self._resolve_release(canonical, owner, repo_name, release)
            default_branch = str(repository.get("default_branch") or "").strip()
            if not default_branch:
                raise GithubSourceError("github_default_branch_missing", "GitHub 仓库缺少默认分支")
            commit_sha = await self._resolve_commit_sha(owner, repo_name, default_branch)
            return ResolvedGithubSource(
                repo_url=canonical,
                owner=owner,
                repo_name=repo_name,
                requested_ref=default_branch,
                commit_sha=commit_sha,
            )

        if not COMMIT_PATTERN.fullmatch(explicit_ref.lower()):
            release = await self._get_optional_json(
                f"/repos/{owner}/{repo_name}/releases/tags/{quote(explicit_ref, safe='')}"
            )
            if (
                release
                and str(release.get("tag_name") or "").strip()
                and not bool(release.get("draft"))
            ):
                return await self._resolve_release(canonical, owner, repo_name, release)

        commit_sha = await self._resolve_commit_sha(owner, repo_name, explicit_ref)
        return ResolvedGithubSource(
            repo_url=canonical,
            owner=owner,
            repo_name=repo_name,
            requested_ref=explicit_ref,
            commit_sha=commit_sha,
        )

    async def _resolve_release(
        self,
        canonical: str,
        owner: str,
        repo_name: str,
        release: Mapping[str, Any],
    ) -> ResolvedGithubSource:
        tag_name = str(release["tag_name"]).strip()
        commit_sha = await self._resolve_commit_sha(owner, repo_name, tag_name)
        selected = select_release_zip_asset(release, owner=owner, repo_name=repo_name)
        download_url, asset_name = selected or ("", "")
        return ResolvedGithubSource(
            repo_url=canonical,
            owner=owner,
            repo_name=repo_name,
            requested_ref=tag_name,
            commit_sha=commit_sha,
            download_url=download_url,
            release_tag=tag_name,
            release_asset_name=asset_name,
        )

    async def _resolve_commit_sha(self, owner: str, repo_name: str, ref: str) -> str:
        commit = await self._get_json(f"/repos/{owner}/{repo_name}/commits/{quote(ref, safe='')}")
        commit_sha = str(commit.get("sha") or "").lower()
        if not COMMIT_PATTERN.fullmatch(commit_sha):
            raise GithubSourceError("github_commit_invalid", "GitHub 未返回有效 commit SHA")
        return commit_sha

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

    async def _get_json(self, path: str) -> dict[str, Any]:
        payload = await self._request_json(path, allow_not_found=False)
        assert payload is not None
        return payload

    async def _get_optional_json(self, path: str) -> dict[str, Any] | None:
        return await self._request_json(path, allow_not_found=True)

    async def _request_json(self, path: str, *, allow_not_found: bool) -> dict[str, Any] | None:
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
        if allow_not_found and response.status_code == 404:
            return None
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
