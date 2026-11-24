"""GitLab.com OAuth2 and REST API v4 repository adapter."""

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
    parse_i18n_metadata_json,
    parse_metadata_yaml_bytes,
    utc_now_iso,
)
from .models import (
    GitSourceError,
    OAuthTokenSet,
    OAuthUserProfile,
    RepositoryProof,
    RepositoryRef,
    ResolvedGitArchiveSource,
    parse_repository_ref,
)

GITLAB_MIN_MAINTAINER_LEVEL = 40


class GitLabAdapter(BaseGitProviderAdapter):
    provider = "gitlab"
    label = "GitLab"
    instance_url = "https://gitlab.com"
    authorize_url = "https://gitlab.com/oauth/authorize"
    token_url = "https://gitlab.com/oauth/token"
    api_base = "https://gitlab.com/api/v4"

    def _headers(self, access_token: str = "") -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "Astrhub-GitLab-Client"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _check_response_errors(self, response: Any, *, not_found_message: str = "") -> None:
        if response.status_code == 401:
            raise GitSourceError(
                "gitlab_authorization_required",
                "GitLab 授权已失效，请重新连接",
                status_code=409,
            )
        if response.status_code == 429:
            raise GitSourceError(
                "gitlab_rate_limited",
                "GitLab API 请求频率受限，请稍后重试",
                status_code=503,
                retryable=True,
            )
        if response.status_code == 403:
            raise GitSourceError(
                "gitlab_authorization_denied",
                "GitLab 未允许访问该资源，请检查授权范围",
                status_code=403,
            )
        if response.status_code == 404 and not_found_message:
            raise GitSourceError(
                "gitlab_repository_unavailable",
                not_found_message,
                status_code=403,
            )
        if response.status_code >= 500:
            raise GitSourceError(
                "gitlab_unavailable",
                "GitLab 服务暂时不可用，请稍后重试",
                status_code=503,
                retryable=True,
            )

    @staticmethod
    def _max_access_level(project: Mapping[str, Any]) -> int:
        permissions = project.get("permissions") or {}
        levels = []
        for key in ("project_access", "group_access"):
            entry = permissions.get(key)
            if isinstance(entry, Mapping):
                try:
                    levels.append(int(entry.get("access_level") or 0))
                except (TypeError, ValueError):
                    pass
        return max(levels, default=0)

    async def fetch_user_profile(self, access_token: str) -> OAuthUserProfile:
        response = await self.safe_request(
            "GET", f"{self.api_base}/user", headers=self._headers(access_token)
        )
        self._check_response_errors(response)
        if response.status_code != 200:
            raise GitSourceError("gitlab_unavailable", "无法获取 GitLab 用户信息", status_code=503)
        data = response.json()
        user_id = str(data.get("id") or "").strip()
        login = str(data.get("username") or "").strip()
        if not user_id or not login:
            raise GitSourceError("gitlab_unavailable", "GitLab 用户信息不完整", status_code=503)
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
            f"{self.api_base}/projects",
            headers=self._headers(access_token),
            params={
                "membership": "true",
                "min_access_level": str(GITLAB_MIN_MAINTAINER_LEVEL),
                "visibility": "public",
                "archived": "false",
                "order_by": "last_activity_at",
                "sort": "desc",
                "per_page": "100",
                "page": str(page),
            },
        )
        self._check_response_errors(response)
        if response.status_code != 200 or not isinstance(response.json(), list):
            raise GitSourceError(
                "gitlab_unavailable", "GitLab 未返回有效的仓库列表", status_code=503
            )
        items: list[dict[str, Any]] = []
        for project in response.json():
            path_slug = str(project.get("path") or "").strip()
            full_path = str(project.get("path_with_namespace") or "").strip()
            if not path_slug.lower().startswith("astrbot_plugin_") or not full_path:
                continue
            if project.get("visibility") != "public" or project.get("archived"):
                continue
            if self._max_access_level(project) < GITLAB_MIN_MAINTAINER_LEVEL:
                continue
            try:
                ref = parse_repository_ref(f"https://gitlab.com/{full_path}")
            except GitSourceError:
                continue
            namespace = project.get("namespace") or {}
            items.append(
                {
                    "id": str(project["id"]),
                    "provider": self.provider,
                    "name": ref.repo_name,
                    "full_name": ref.full_path,
                    "repo": ref.repo_url,
                    "owner": ref.owner,
                    "owner_id": str(namespace.get("id") or ""),
                    "owner_type": str(namespace.get("kind") or "group"),
                    "description": str(project.get("description") or ""),
                    "fork": bool(project.get("forked_from_project")),
                    "submitted": ref.normalized_repo in submitted,
                }
            )
        items.sort(key=lambda item: (item["submitted"], item["fork"]))
        next_page_header = str(response.headers.get("x-next-page") or "").strip()
        next_page = int(next_page_header) if next_page_header.isdigit() else None
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
        encoded = quote(ref.full_path, safe="")
        response = await self.safe_request(
            "GET",
            f"{self.api_base}/projects/{encoded}",
            headers=self._headers(token_set.access_token),
        )
        self._check_response_errors(
            response, not_found_message="GitLab 仓库不存在或当前授权无法访问"
        )
        if response.status_code != 200:
            raise GitSourceError("gitlab_unavailable", "GitLab 无法验证仓库权限", status_code=503)
        project = response.json()
        if project.get("visibility") != "public" or project.get("archived"):
            raise GitSourceError(
                "gitlab_repository_unavailable",
                "请选择公开、未归档的 GitLab 仓库",
                status_code=403,
            )
        repo_id = str(project.get("id") or "").strip()
        if expected_repository_id and repo_id != expected_repository_id:
            raise GitSourceError(
                "gitlab_repository_changed",
                "GitLab 仓库身份已变化，请刷新列表重新选择",
                status_code=409,
            )
        access_level = self._max_access_level(project)
        if access_level < GITLAB_MIN_MAINTAINER_LEVEL:
            raise GitSourceError(
                "gitlab_repository_permission_required",
                "需要该 GitLab 仓库的 Maintainer（access_level >= 40）及以上权限",
                status_code=403,
            )
        canonical_ref = parse_repository_ref(
            f"https://gitlab.com/{str(project.get('path_with_namespace') or ref.full_path)}"
        )
        namespace = project.get("namespace") or {}
        return RepositoryProof(
            provider=self.provider,
            user_id=str(user_id),
            repo=canonical_ref.repo_url,
            normalized_repo=canonical_ref.normalized_repo,
            repo_external_id=repo_id,
            owner_id=str(namespace.get("id") or ""),
            owner_login=canonical_ref.owner,
            repo_name=canonical_ref.repo_name,
            full_path=canonical_ref.full_path,
            provider_user_id=token_set.provider_user_id,
            permission_level="owner" if access_level >= 50 else "maintainer",
            verified_at=utc_now_iso(),
            credential_ref=credential_ref,
        )

    async def _fetch_raw_file(
        self,
        encoded_project: str,
        file_path: str,
        branch_or_sha: str,
        access_token: str = "",
    ) -> bytes | None:
        encoded_file = quote(file_path, safe="")
        resp = await self.safe_request(
            "GET",
            f"{self.api_base}/projects/{encoded_project}/repository/files/{encoded_file}/raw",
            headers=self._headers(access_token),
            params={"ref": branch_or_sha},
        )
        if resp.status_code != 200:
            return None
        return bytes(resp.content or b"")

    async def _fetch_tree(
        self,
        encoded_project: str,
        *,
        path: str = "",
        ref: str = "",
        access_token: str = "",
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"per_page": "100"}
        if path:
            params["path"] = path
        if ref:
            params["ref"] = ref
        resp = await self.safe_request(
            "GET",
            f"{self.api_base}/projects/{encoded_project}/repository/tree",
            headers=self._headers(access_token),
            params=params,
        )
        if resp.status_code != 200 or not isinstance(resp.json(), list):
            return []
        return list(resp.json())

    @staticmethod
    def _raw_web_url(ref: RepositoryRef, branch: str, filename: str) -> str:
        return (
            f"https://gitlab.com/{ref.full_path}/-/raw/"
            f"{quote(branch, safe='')}/{quote(filename, safe='/')}"
        )

    async def fetch_repository_metadata(
        self,
        ref: RepositoryRef,
        *,
        access_token: str = "",
    ) -> dict[str, Any]:
        encoded = quote(ref.full_path, safe="")
        resp = await self.safe_request(
            "GET",
            f"{self.api_base}/projects/{encoded}",
            headers=self._headers(access_token),
        )
        if resp.status_code == 404:
            return {}
        self._check_response_errors(resp)
        if resp.status_code != 200:
            return {}
        project = resp.json()
        default_branch = str(project.get("default_branch") or "main").strip() or "main"
        raw_meta = await self._fetch_raw_file(
            encoded, "metadata.yaml", default_branch, access_token
        )
        if raw_meta is None:
            raw_meta = await self._fetch_raw_file(
                encoded, "metadata.yml", default_branch, access_token
            )
        parsed_meta = parse_metadata_yaml_bytes(raw_meta or b"")
        root_entries = await self._fetch_tree(
            encoded, ref=default_branch, access_token=access_token
        )
        root_files = {
            str(item.get("name") or ""): item
            for item in root_entries
            if item.get("type") == "blob" and item.get("name")
        }
        root_lower = {name.lower(): name for name in root_files}
        payload: dict[str, Any] = {
            "stars": int(project.get("star_count") or 0),
            "updated_at": str(project.get("last_activity_at") or ""),
            "repo_provider": self.provider,
            "repo_external_id": str(project.get("id") or ""),
            **parsed_meta,
        }
        logo_name = root_lower.get("logo.png")
        if logo_name:
            payload["logo"] = self._raw_web_url(ref, default_branch, logo_name)
        readme_name = next((c for c in README_CANDIDATES if c in root_files), None) or (
            root_lower.get("readme.md") or root_lower.get("readme")
        )
        if readme_name:
            payload["readme_url"] = self._raw_web_url(ref, default_branch, readme_name)
        changelog_name = next((c for c in CHANGELOG_CANDIDATES if c in root_files), None) or (
            root_lower.get("changelog.md") or root_lower.get("changelog")
        )
        if changelog_name:
            payload["changelog_url"] = self._raw_web_url(ref, default_branch, changelog_name)
        if any(
            item.get("type") == "tree" and item.get("name") == ".astrbot-plugin"
            for item in root_entries
        ):
            i18n_entries = await self._fetch_tree(
                encoded, path=".astrbot-plugin/i18n", ref=default_branch, access_token=access_token
            )
            i18n: dict[str, dict[str, str]] = {}
            for entry in i18n_entries[:PLUGIN_I18N_MAX_FILES]:
                fname = str(entry.get("name") or "")
                if entry.get("type") != "blob" or not fname.endswith(".json"):
                    continue
                locale = fname[:-5]
                if not PLUGIN_I18N_LOCALE_PATTERN.fullmatch(locale):
                    continue
                raw_i18n = await self._fetch_raw_file(
                    encoded, f".astrbot-plugin/i18n/{fname}", default_branch, access_token
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
        encoded = quote(ref.full_path, safe="")
        resp = await self.safe_request(
            "GET", f"{self.api_base}/projects/{encoded}", headers=self._headers(access_token)
        )
        branch = "main"
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            branch = str(resp.json().get("default_branch") or "main").strip() or "main"
        candidates = (path,) if path else README_CANDIDATES
        for candidate in candidates:
            raw = await self._fetch_raw_file(encoded, candidate, branch, access_token)
            if raw:
                return {
                    "content": raw.decode("utf-8", errors="replace"),
                    "source_url": self._raw_web_url(ref, branch, candidate),
                    "fetched_at": utc_now_iso(),
                    "cached": False,
                }
        return None

    async def resolve_archive_source(
        self,
        ref: RepositoryRef,
        *,
        access_token: str = "",
        requested_ref: str = "",
    ) -> ResolvedGitArchiveSource:
        encoded = quote(ref.full_path, safe="")
        resp = await self.safe_request(
            "GET",
            f"{self.api_base}/projects/{encoded}",
            headers=self._headers(access_token),
        )
        self._check_response_errors(resp, not_found_message="GitLab 仓库不存在或无法访问")
        if resp.status_code != 200:
            raise GitSourceError("gitlab_unavailable", "无法获取 GitLab 仓库信息", status_code=503)
        project = resp.json()
        if project.get("visibility") != "public" or project.get("archived"):
            raise GitSourceError(
                "gitlab_repository_unavailable",
                "CDN 仅支持公开且未归档的 GitLab 仓库",
                status_code=403,
            )
        target_ref = requested_ref.strip() or str(project.get("default_branch") or "").strip()
        if not target_ref:
            raise GitSourceError(
                "gitlab_default_branch_missing", "GitLab 仓库缺少默认分支", status_code=409
            )
        commit_resp = await self.safe_request(
            "GET",
            f"{self.api_base}/projects/{encoded}/repository/commits/{quote(target_ref, safe='')}",
            headers=self._headers(access_token),
        )
        self._check_response_errors(commit_resp, not_found_message="GitLab 提交不存在")
        if commit_resp.status_code != 200:
            raise GitSourceError("gitlab_commit_invalid", "GitLab 无法解析提交", status_code=503)
        commit_sha = str(commit_resp.json().get("id") or "").lower()
        if not COMMIT_SHA_PATTERN.fullmatch(commit_sha):
            raise GitSourceError(
                "gitlab_commit_invalid", "GitLab 未返回有效 commit SHA", status_code=503
            )
        canonical_ref = parse_repository_ref(
            f"https://gitlab.com/{str(project.get('path_with_namespace') or ref.full_path)}"
        )
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        return ResolvedGitArchiveSource(
            provider=self.provider,
            repo_url=canonical_ref.repo_url,
            normalized_repo=canonical_ref.normalized_repo,
            full_path=canonical_ref.full_path,
            repo_name=canonical_ref.repo_name,
            requested_ref=target_ref,
            commit_sha=commit_sha,
            archive_url=(
                f"{self.api_base}/projects/{quote(canonical_ref.full_path, safe='')}"
                f"/repository/archive.zip?sha={commit_sha}"
            ),
            repo_external_id=str(project.get("id") or ""),
            headers=headers,
        )
