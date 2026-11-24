"""Shared /api/v5 OAuth2 & repository adapters for Gitee and AtomGit (formerly GitCode)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from .base import (
    CHANGELOG_CANDIDATES,
    COMMIT_SHA_PATTERN,
    PLUGIN_I18N_LOCALE_PATTERN,
    PLUGIN_I18N_MAX_FILES,
    README_CANDIDATES,
    BaseGitProviderAdapter,
    ProviderOAuthConfig,
    decode_base64_content,
    parse_i18n_metadata_json,
    parse_metadata_yaml_bytes,
    utc_now_iso,
)
from .models import (
    ATOMGIT_DEFAULT_API_BASE,
    GitSourceError,
    OAuthTokenSet,
    OAuthUserProfile,
    RepositoryProof,
    RepositoryRef,
    ResolvedGitArchiveSource,
    normalize_atomgit_api_base,
    parse_repository_ref,
)


class _V5GitAdapter(BaseGitProviderAdapter):
    api_origin: str = ""
    web_origin: str = ""

    def _headers(self, access_token: str = "") -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": f"Astrhub-{self.label}-Client"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _api(self, path: str) -> str:
        return f"{self.api_origin}/api/v5{path}"

    def _check_response_errors(self, response: Any, *, not_found_message: str = "") -> None:
        if response.status_code == 401:
            raise GitSourceError(
                f"{self.provider}_authorization_required",
                f"{self.label} 授权已失效，请重新连接",
                status_code=409,
            )
        if response.status_code == 429:
            raise GitSourceError(
                f"{self.provider}_rate_limited",
                f"{self.label} API 请求频率受限，请稍后重试",
                status_code=503,
                retryable=True,
            )
        if response.status_code == 403:
            raise GitSourceError(
                f"{self.provider}_authorization_denied",
                f"{self.label} 未允许访问该资源，请检查授权范围",
                status_code=403,
            )
        if response.status_code == 404 and not_found_message:
            raise GitSourceError(
                f"{self.provider}_repository_unavailable",
                not_found_message,
                status_code=403,
            )
        if response.status_code >= 500:
            raise GitSourceError(
                f"{self.provider}_unavailable",
                f"{self.label} 服务暂时不可用，请稍后重试",
                status_code=503,
                retryable=True,
            )

    @staticmethod
    def _is_public_repo(repo: Mapping[str, Any]) -> bool:
        if repo.get("private") is True:
            return False
        if repo.get("public") is False:
            return False
        return True

    @staticmethod
    def _permission_level(repo: Mapping[str, Any], *, current_user_id: str = "") -> str:
        perm = repo.get("permission") or repo.get("permissions") or {}
        if isinstance(perm, Mapping):
            if perm.get("admin") is True:
                return "admin"
            if perm.get("push") is True or perm.get("write") is True:
                return "push"
        owner = repo.get("owner") or {}
        if current_user_id and str(owner.get("id") or "") == str(current_user_id):
            return "owner"
        return ""

    async def _check_collaborator_permission(
        self,
        owner: str,
        repo_name: str,
        login: str,
        access_token: str,
    ) -> str:
        if not login:
            return ""
        resp = await self.safe_request(
            "GET",
            self._api(
                f"/repos/{quote(owner, safe='')}/{quote(repo_name, safe='')}"
                f"/collaborators/{quote(login, safe='')}/permission"
            ),
            headers=self._headers(access_token),
        )
        if resp.status_code != 200 or not isinstance(resp.json(), dict):
            return ""
        permission = str(resp.json().get("permission") or "").strip().lower()
        if permission in {"admin", "write", "push", "owner", "maintainer"}:
            return permission
        return ""

    async def fetch_user_profile(self, access_token: str) -> OAuthUserProfile:
        response = await self.safe_request(
            "GET", self._api("/user"), headers=self._headers(access_token)
        )
        self._check_response_errors(response)
        if response.status_code != 200:
            raise GitSourceError(
                f"{self.provider}_unavailable", f"无法获取 {self.label} 用户信息", status_code=503
            )
        data = response.json()
        user_id = str(data.get("id") or "").strip()
        login = str(data.get("login") or data.get("username") or "").strip()
        if not user_id or not login:
            raise GitSourceError(
                f"{self.provider}_unavailable", f"{self.label} 用户信息不完整", status_code=503
            )
        return OAuthUserProfile(
            provider=self.provider,
            instance_url=self.instance_url,
            provider_user_id=user_id,
            provider_login=login,
            display_name=str(data.get("name") or login).strip(),
            email=str(data.get("email") or "").strip(),
            avatar_url=str(data.get("avatar_url") or "").strip(),
        )

    async def list_manageable_repositories(
        self,
        access_token: str,
        *,
        page: int = 1,
        submitted_normalized_repos: set[str] | None = None,
    ) -> dict[str, Any]:
        submitted = submitted_normalized_repos or set()
        response = await self.safe_request(
            "GET",
            self._api("/user/repos"),
            headers=self._headers(access_token),
            params={
                "visibility": "public",
                "sort": "pushed",
                "direction": "desc",
                "per_page": "100",
                "page": str(page),
            },
        )
        self._check_response_errors(response)
        if response.status_code != 200 or not isinstance(response.json(), list):
            raise GitSourceError(
                f"{self.provider}_unavailable",
                f"{self.label} 未返回有效的仓库列表",
                status_code=503,
            )
        raw_items = response.json()
        items: list[dict[str, Any]] = []
        for repo in raw_items:
            if not self._is_public_repo(repo) or repo.get("archived"):
                continue
            full_name = str(repo.get("full_name") or "").strip()
            name = str(repo.get("path") or repo.get("name") or "").strip()
            if not full_name and repo.get("namespace") and name:
                ns = repo["namespace"]
                ns_path = str(ns.get("path") if isinstance(ns, Mapping) else ns).strip()
                if ns_path:
                    full_name = f"{ns_path}/{name}"
            if not full_name or not name.lower().startswith("astrbot_plugin_"):
                continue
            perm = self._permission_level(repo)
            if not perm:
                continue
            try:
                ref = parse_repository_ref(f"{self.web_origin}/{full_name}")
            except GitSourceError:
                continue
            owner = repo.get("owner") or {}
            items.append(
                {
                    "id": str(repo.get("id") or ""),
                    "provider": self.provider,
                    "name": ref.repo_name,
                    "full_name": ref.full_path,
                    "repo": ref.repo_url,
                    "owner": ref.owner,
                    "owner_id": str(owner.get("id") or ""),
                    "owner_type": str(owner.get("type") or "User"),
                    "description": str(repo.get("description") or ""),
                    "fork": bool(repo.get("fork")),
                    "submitted": ref.normalized_repo in submitted,
                }
            )
        items.sort(key=lambda item: (item["submitted"], item["fork"]))
        next_page = page + 1 if len(raw_items) >= 100 else None
        return {"items": items, "next_page": next_page}

    async def verify_repository_access(
        self,
        ref: RepositoryRef,
        *,
        user_id: str,
        token_set: OAuthTokenSet,
        credential_ref: str = "",
        expected_repository_id: str = "",
    ) -> RepositoryProof:
        resp = await self.safe_request(
            "GET",
            self._api(f"/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"),
            headers=self._headers(token_set.access_token),
        )
        self._check_response_errors(
            resp, not_found_message=f"{self.label} 仓库不存在或当前授权无法访问"
        )
        if resp.status_code != 200:
            raise GitSourceError(
                f"{self.provider}_unavailable",
                f"{self.label} 无法验证仓库权限",
                status_code=503,
            )
        repo = resp.json()
        if not self._is_public_repo(repo) or repo.get("archived"):
            raise GitSourceError(
                f"{self.provider}_repository_unavailable",
                f"请选择公开、未归档的 {self.label} 仓库",
                status_code=403,
            )
        repo_id = str(repo.get("id") or "").strip()
        if expected_repository_id and repo_id != expected_repository_id:
            raise GitSourceError(
                f"{self.provider}_repository_changed",
                f"{self.label} 仓库身份已变化，请刷新列表重新选择",
                status_code=409,
            )
        perm = self._permission_level(repo, current_user_id=token_set.provider_user_id)
        if not perm:
            perm = await self._check_collaborator_permission(
                ref.owner, ref.repo_name, token_set.provider_login, token_set.access_token
            )
        if not perm:
            raise GitSourceError(
                f"{self.provider}_repository_permission_required",
                f"你当前没有该 {self.label} 仓库的写入或管理权限",
                status_code=403,
            )
        full_name = str(repo.get("full_name") or ref.full_path).strip()
        canonical_ref = parse_repository_ref(f"{self.web_origin}/{full_name}")
        owner = repo.get("owner") or {}
        return RepositoryProof(
            provider=self.provider,
            user_id=str(user_id),
            repo=canonical_ref.repo_url,
            normalized_repo=canonical_ref.normalized_repo,
            repo_external_id=repo_id,
            owner_id=str(owner.get("id") or ""),
            owner_login=canonical_ref.owner,
            repo_name=canonical_ref.repo_name,
            full_path=canonical_ref.full_path,
            provider_user_id=token_set.provider_user_id,
            permission_level=perm,
            verified_at=utc_now_iso(),
            credential_ref=credential_ref,
        )

    async def _fetch_content_entry(
        self,
        ref: RepositoryRef,
        path: str,
        branch: str,
        access_token: str = "",
    ) -> Any:
        encoded_path = quote(path, safe="/") if path else ""
        suffix = f"/{encoded_path}" if encoded_path else ""
        resp = await self.safe_request(
            "GET",
            self._api(
                f"/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}/contents{suffix}"
            ),
            headers=self._headers(access_token),
            params={"ref": branch} if branch else None,
        )
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def _fetch_file_bytes(
        self,
        ref: RepositoryRef,
        path: str,
        branch: str,
        access_token: str = "",
    ) -> tuple[bytes | None, str]:
        entry = await self._fetch_content_entry(ref, path, branch, access_token)
        if not isinstance(entry, Mapping):
            return None, ""
        content = str(entry.get("content") or "")
        if content:
            decoded = decode_base64_content(content)
            if decoded:
                source_url = str(
                    entry.get("download_url")
                    or entry.get("html_url")
                    or self._raw_web_url(ref, branch, path)
                )
                return decoded, source_url
        return None, ""

    def _raw_web_url(self, ref: RepositoryRef, branch: str, filename: str) -> str:
        return (
            f"{self.web_origin}/{ref.owner}/{ref.repo_name}/raw/"
            f"{quote(branch, safe='')}/{quote(filename, safe='/')}"
        )

    async def fetch_repository_metadata(
        self,
        ref: RepositoryRef,
        *,
        access_token: str = "",
    ) -> dict[str, Any]:
        resp = await self.safe_request(
            "GET",
            self._api(f"/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"),
            headers=self._headers(access_token),
        )
        if resp.status_code == 404:
            return {}
        self._check_response_errors(resp)
        if resp.status_code != 200:
            return {}
        repo = resp.json()
        default_branch = str(repo.get("default_branch") or "master").strip() or "master"
        raw_meta, _ = await self._fetch_file_bytes(
            ref, "metadata.yaml", default_branch, access_token
        )
        if raw_meta is None:
            raw_meta, _ = await self._fetch_file_bytes(
                ref, "metadata.yml", default_branch, access_token
            )
        parsed_meta = parse_metadata_yaml_bytes(raw_meta or b"")
        root_entries = await self._fetch_content_entry(ref, "", default_branch, access_token)
        entries_list = root_entries if isinstance(root_entries, list) else []
        root_files = {
            str(item.get("name") or ""): item
            for item in entries_list
            if isinstance(item, Mapping) and item.get("type") == "file" and item.get("name")
        }
        root_lower = {name.lower(): name for name in root_files}
        payload: dict[str, Any] = {
            "stars": int(repo.get("stargazers_count") or 0),
            "updated_at": str(repo.get("pushed_at") or repo.get("updated_at") or ""),
            "repo_provider": self.provider,
            "repo_external_id": str(repo.get("id") or ""),
            **parsed_meta,
        }
        logo_name = root_lower.get("logo.png")
        if logo_name:
            payload["logo"] = str(
                root_files[logo_name].get("download_url")
                or self._raw_web_url(ref, default_branch, logo_name)
            )
        readme_name = next((c for c in README_CANDIDATES if c in root_files), None) or (
            root_lower.get("readme.md") or root_lower.get("readme")
        )
        if readme_name:
            payload["readme_url"] = str(
                root_files[readme_name].get("download_url")
                or self._raw_web_url(ref, default_branch, readme_name)
            )
        changelog_name = next((c for c in CHANGELOG_CANDIDATES if c in root_files), None) or (
            root_lower.get("changelog.md") or root_lower.get("changelog")
        )
        if changelog_name:
            payload["changelog_url"] = str(
                root_files[changelog_name].get("download_url")
                or self._raw_web_url(ref, default_branch, changelog_name)
            )
        if any(
            isinstance(item, Mapping)
            and item.get("type") == "dir"
            and item.get("name") == ".astrbot-plugin"
            for item in entries_list
        ):
            i18n_entries = await self._fetch_content_entry(
                ref, ".astrbot-plugin/i18n", default_branch, access_token
            )
            i18n: dict[str, dict[str, str]] = {}
            if isinstance(i18n_entries, list):
                for entry in i18n_entries[:PLUGIN_I18N_MAX_FILES]:
                    if not isinstance(entry, Mapping) or entry.get("type") != "file":
                        continue
                    fname = str(entry.get("name") or "")
                    if not fname.endswith(".json"):
                        continue
                    locale = fname[:-5]
                    if not PLUGIN_I18N_LOCALE_PATTERN.fullmatch(locale):
                        continue
                    raw_i18n, _ = await self._fetch_file_bytes(
                        ref, f".astrbot-plugin/i18n/{fname}", default_branch, access_token
                    )
                    translated = parse_i18n_metadata_json(raw_i18n or b"")
                    if translated:
                        i18n[locale] = translated
            payload["i18n"] = i18n
        return payload

    async def fetch_readme_document(
        self,
        ref: RepositoryRef,
        *,
        path: str = "",
        access_token: str = "",
    ) -> dict[str, Any] | None:
        resp = await self.safe_request(
            "GET",
            self._api(f"/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"),
            headers=self._headers(access_token),
        )
        branch = "master"
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            branch = str(resp.json().get("default_branch") or "master").strip() or "master"
        candidates = (path,) if path else README_CANDIDATES
        for candidate in candidates:
            raw, source_url = await self._fetch_file_bytes(ref, candidate, branch, access_token)
            if raw:
                return {
                    "content": raw.decode("utf-8", errors="replace"),
                    "source_url": source_url,
                    "fetched_at": utc_now_iso(),
                    "cached": False,
                }
        return None

    def _archive_url(self, ref: RepositoryRef, commit_sha: str) -> str:
        return (
            f"{self.web_origin}/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"
            f"/repository/archive/{commit_sha}.zip"
        )

    async def resolve_archive_source(
        self,
        ref: RepositoryRef,
        *,
        access_token: str = "",
        requested_ref: str = "",
    ) -> ResolvedGitArchiveSource:
        resp = await self.safe_request(
            "GET",
            self._api(f"/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"),
            headers=self._headers(access_token),
        )
        self._check_response_errors(resp, not_found_message=f"{self.label} 仓库不存在或无法访问")
        if resp.status_code != 200:
            raise GitSourceError(
                f"{self.provider}_unavailable",
                f"无法获取 {self.label} 仓库信息",
                status_code=503,
            )
        repo = resp.json()
        if not self._is_public_repo(repo) or repo.get("archived"):
            raise GitSourceError(
                f"{self.provider}_repository_unavailable",
                f"CDN 仅支持公开且未归档的 {self.label} 仓库",
                status_code=403,
            )
        target_ref = requested_ref.strip() or str(repo.get("default_branch") or "").strip()
        if not target_ref:
            raise GitSourceError(
                f"{self.provider}_default_branch_missing",
                f"{self.label} 仓库缺少默认分支",
                status_code=409,
            )
        commit_resp = await self.safe_request(
            "GET",
            self._api(
                f"/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"
                f"/commits/{quote(target_ref, safe='')}"
            ),
            headers=self._headers(access_token),
        )
        self._check_response_errors(commit_resp, not_found_message=f"{self.label} 提交不存在")
        if commit_resp.status_code != 200:
            raise GitSourceError(
                f"{self.provider}_commit_invalid", f"{self.label} 无法解析提交", status_code=503
            )
        commit_sha = str(commit_resp.json().get("sha") or "").lower()
        if not COMMIT_SHA_PATTERN.fullmatch(commit_sha):
            raise GitSourceError(
                f"{self.provider}_commit_invalid",
                f"{self.label} 未返回有效 commit SHA",
                status_code=503,
            )
        full_name = str(repo.get("full_name") or ref.full_path).strip()
        canonical_ref = parse_repository_ref(f"{self.web_origin}/{full_name}")
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        return ResolvedGitArchiveSource(
            provider=self.provider,
            repo_url=canonical_ref.repo_url,
            normalized_repo=canonical_ref.normalized_repo,
            full_path=canonical_ref.full_path,
            repo_name=canonical_ref.repo_name,
            requested_ref=target_ref,
            commit_sha=commit_sha,
            archive_url=self._archive_url(canonical_ref, commit_sha),
            repo_external_id=str(repo.get("id") or ""),
            headers=headers,
        )


class GiteeAdapter(_V5GitAdapter):
    provider = "gitee"
    label = "Gitee"
    instance_url = "https://gitee.com"
    web_origin = "https://gitee.com"
    api_origin = "https://gitee.com"
    authorize_url = "https://gitee.com/oauth/authorize"
    token_url = "https://gitee.com/oauth/token"


class AtomGitAdapter(_V5GitAdapter):
    provider = "atomgit"
    label = "AtomGit"
    instance_url = "https://atomgit.com"
    web_origin = "https://atomgit.com"
    api_origin = ATOMGIT_DEFAULT_API_BASE
    authorize_url = "https://atomgit.com/oauth/authorize"
    token_url = "https://atomgit.com/oauth/token"

    def __init__(self, config: ProviderOAuthConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self.api_origin = normalize_atomgit_api_base(config.api_base_url)

    def _archive_url(self, ref: RepositoryRef, commit_sha: str) -> str:
        return self._api(
            f"/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"
            f"/zipball/{commit_sha}"
        )
