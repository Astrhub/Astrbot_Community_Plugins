"""Unified domain models and strict allowlisted HTTPS URL normalization for Git providers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

SUPPORTED_PROVIDERS: tuple[str, ...] = ("github", "gitlab", "gitee", "atomgit", "gitea")
THIRD_PARTY_PROVIDERS: tuple[str, ...] = ("gitlab", "gitee", "atomgit", "gitea")

PROVIDER_LABELS: dict[str, str] = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "gitee": "Gitee",
    "atomgit": "AtomGit",
    "gitea": "Gitea",
}

PROVIDER_CANONICAL_HOSTS: dict[str, str] = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "gitee": "gitee.com",
    "atomgit": "atomgit.com",
    "gitea": "gitea.com",
}

PROVIDER_INSTANCE_URLS: dict[str, str] = {
    provider: f"https://{host}" for provider, host in PROVIDER_CANONICAL_HOSTS.items()
}

PROVIDER_DEFAULT_SCOPES: dict[str, str] = {
    "github": "read:user user:email read:org",
    "gitlab": "read_user read_api",
    "gitee": "user_info projects",
    "atomgit": "read_user read_repository",
    "gitea": "read:user read:repository read:organization",
}

ATOMGIT_DEFAULT_API_BASE = "https://api.atomgit.com"
ATOMGIT_ALLOWED_API_BASES: frozenset[str] = frozenset(
    {
        "https://api.atomgit.com",
        "https://api.gitcode.com",
    }
)

ALLOWED_REPO_HOSTS: dict[str, str] = {
    "github.com": "github",
    "www.github.com": "github",
    "gitlab.com": "gitlab",
    "www.gitlab.com": "gitlab",
    "gitee.com": "gitee",
    "www.gitee.com": "gitee",
    "atomgit.com": "atomgit",
    "www.atomgit.com": "atomgit",
    "gitcode.com": "atomgit",
    "www.gitcode.com": "atomgit",
    "gitea.com": "gitea",
    "www.gitea.com": "gitea",
}

ALLOWED_API_HOSTS_BY_PROVIDER: dict[str, frozenset[str]] = {
    "github": frozenset(
        {
            "api.github.com",
            "github.com",
            "codeload.github.com",
            "raw.githubusercontent.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
        }
    ),
    "gitlab": frozenset({"gitlab.com"}),
    "gitee": frozenset({"gitee.com"}),
    "atomgit": frozenset(
        {
            "atomgit.com",
            "api.atomgit.com",
            "gitcode.com",
            "api.gitcode.com",
        }
    ),
    "gitea": frozenset({"gitea.com"}),
}

_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_GITLAB_PATH_DEPTH = 20


class GitSourceError(RuntimeError):
    """Structured error raised by allowlisted Git provider adapters and parsers."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    provider: str
    instance_url: str
    owner: str
    repo_name: str
    full_path: str
    repo_url: str
    normalized_repo: str
    external_id: str = ""
    transport: str = "https_api"


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    provider: str
    access_token: str
    refresh_token: str = ""
    scope: str = ""
    expires_at: str | None = None
    provider_user_id: str = ""
    provider_login: str = ""


@dataclass(frozen=True, slots=True)
class OAuthUserProfile:
    provider: str
    instance_url: str
    provider_user_id: str
    provider_login: str
    display_name: str = ""
    email: str = ""
    avatar_url: str = ""


@dataclass(frozen=True, slots=True)
class RepositoryProof:
    provider: str
    user_id: str
    repo: str
    normalized_repo: str
    repo_external_id: str
    owner_id: str
    owner_login: str
    repo_name: str
    full_path: str
    provider_user_id: str
    permission_level: str
    verification_method: str = "oauth"
    verified_at: str = ""
    credential_ref: str = ""
    binding_generation: int = 1
    mode: str = "oauth"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "mode": self.mode,
            "user_id": self.user_id,
            "repo": self.repo,
            "normalized_repo": self.normalized_repo,
            "id": self.repo_external_id,
            "repo_external_id": self.repo_external_id,
            "owner_id": self.owner_id,
            "owner": self.owner_login,
            "name": self.repo_name,
            "full_name": self.full_path,
            "provider_user_id": self.provider_user_id,
            "permission_level": self.permission_level,
            "verification_method": self.verification_method,
            "verified_at": self.verified_at,
            "credential_ref": self.credential_ref,
            "binding_generation": self.binding_generation,
        }


@dataclass(frozen=True, slots=True)
class ResolvedGitArchiveSource:
    provider: str
    repo_url: str
    normalized_repo: str
    full_path: str
    repo_name: str
    requested_ref: str
    commit_sha: str
    archive_url: str
    repo_external_id: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def normalize_atomgit_api_base(raw_url: str | None) -> str:
    cleaned = str(raw_url or "").strip().rstrip("/")
    if not cleaned:
        return ATOMGIT_DEFAULT_API_BASE
    parsed = urlsplit(cleaned)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GitSourceError(
            "invalid_atomgit_api_base",
            "AtomGit API 地址端口格式无效",
            status_code=400,
        ) from exc
    normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    if (
        port is not None
        or normalized not in ATOMGIT_ALLOWED_API_BASES
        or parsed.path not in {"", "/"}
    ):
        raise GitSourceError(
            "invalid_atomgit_api_base",
            "AtomGit API 地址仅支持 https://api.atomgit.com 或 https://api.gitcode.com",
            status_code=400,
        )
    return normalized


def parse_repository_ref(raw_url: str) -> RepositoryRef:
    value = str(raw_url or "").strip()
    if not value:
        raise GitSourceError("invalid_git_repository", "仓库地址不能为空", status_code=400)
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GitSourceError(
            "invalid_git_repository",
            "仓库地址端口格式无效",
            status_code=400,
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GitSourceError(
            "invalid_git_repository",
            "仓库地址必须为不含凭据、查询参数或片段的公网 HTTPS 地址",
            status_code=400,
        )
    hostname = (parsed.hostname or "").lower()
    provider = ALLOWED_REPO_HOSTS.get(hostname)
    if not provider:
        raise GitSourceError(
            "unsupported_git_provider",
            "暂仅支持 GitHub、GitLab.com、Gitee、AtomGit（GitCode）与 Gitea（gitea.com）公开仓库",
            status_code=400,
        )
    segments = [segment for segment in parsed.path.split("/") if segment]
    if any(
        segment in {".", ".."} or not _PATH_SEGMENT_PATTERN.fullmatch(segment)
        for segment in segments
    ):
        raise GitSourceError("invalid_git_repository", "仓库路径包含非法字符", status_code=400)
    if provider == "gitlab":
        if len(segments) < 2 or len(segments) > _MAX_GITLAB_PATH_DEPTH:
            raise GitSourceError(
                "invalid_git_repository",
                "GitLab 仓库路径必须包含有效的命名空间与仓库名",
                status_code=400,
            )
    elif len(segments) != 2:
        raise GitSourceError(
            "invalid_git_repository",
            f"{PROVIDER_LABELS[provider]} 仓库地址必须为 https://{PROVIDER_CANONICAL_HOSTS[provider]}/owner/repo 格式",
            status_code=400,
        )

    repo_name = segments[-1]
    if repo_name.lower().endswith(".git"):
        repo_name = repo_name[:-4]
    if not repo_name or repo_name in {".", ".."}:
        raise GitSourceError("invalid_git_repository", "仓库名无效", status_code=400)

    owner = "/".join(segments[:-1])
    full_path = f"{owner}/{repo_name}"
    canonical_host = PROVIDER_CANONICAL_HOSTS[provider]
    instance_url = f"https://{canonical_host}"
    repo_url = f"{instance_url}/{full_path}"
    return RepositoryRef(
        provider=provider,
        instance_url=instance_url,
        owner=owner,
        repo_name=repo_name,
        full_path=full_path,
        repo_url=repo_url,
        normalized_repo=repo_url.lower(),
    )


def normalize_git_repo_url(raw_url: str) -> str:
    return parse_repository_ref(raw_url).repo_url


def normalize_git_repo_key(raw_url: str) -> str:
    return parse_repository_ref(raw_url).normalized_repo


def is_supported_git_repo_url(raw_url: str) -> bool:
    try:
        parse_repository_ref(raw_url)
    except GitSourceError:
        return False
    return True
