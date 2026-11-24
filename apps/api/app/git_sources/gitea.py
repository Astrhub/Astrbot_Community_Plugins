"""Official hosted Gitea (gitea.com) OAuth2 and REST API v1 repository adapter."""

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


class GiteaAdapter(BaseGitProviderAdapter):
    provider = "gitea"
    label = "Gitea"
    instance_url = "https://gitea.com"
    authorize_url = "https://gitea.com/login/oauth/authorize"
    token_url = "https://gitea.com/login/oauth/access_token"
    api_base = "https://gitea.com/api/v1"

    def _headers(self, access_token: str = "") -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "Astrhub-Gitea-Client"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _check_response_errors(self, response: Any, *, not_found_message: str = "") -> None:
        if response.status_code == 401:
            raise GitSourceError(
                "gitea_authorization_required",
                "Gitea 授权已失效，请重新连接",
                status_code=409,
            )
        if response.status_code == 429:
            raise GitSourceError(
                "gitea_rate_limited",
                "Gitea API 请求频率受限，请稍后重试",
                status_code=503,
                retryable=True,
            )
        if response.status_code == 403:
            raise GitSourceError(
                "gitea_authorization_denied",
                "Gitea 未允许访问该资源，请检查授权范围",
                status_code=403,
            )
        if response.status_code == 404 and not_found_message:
            raise GitSourceError(
                "gitea_repository_unavailable",
                not_found_message,
                status_code=403,
            )
        if response.status_code >= 500:
            raise GitSourceError(
                "gitea_unavailable",
                "Gitea 服务暂时不可用，请稍后重试",
                status_code=503,
                retryable=True,
            )

    @staticmethod
    def _permission_level(repo: Mapping[str, Any], *, current_user_id: str = "") -> str:
        permissions = repo.get("permissions") or {}
        if isinstance(permissions, Mapping):
            if permissions.get("admin") is True:
                return "admin"
            if permissions.get("push") is True:
                return "push"
        owner = repo.get("owner") or {}
        if current_user_id and str(owner.get("id") or "") == str(current_user_id):
            return "owner"
        return ""

    async def fetch_user_profile(self, access_token: str) -> OAuthUserProfile:
        response = await self.safe_request(
            "GET", f"{self.api_base}/user", headers=self._headers(access_token)
        )
        self._check_response_errors(response)
        if response.status_code != 200:
            raise GitSourceError("gitea_unavailable", "无法获取 Gitea 用户信息", status_code=503)
        data = response.json()
        user_id = str(data.get("id") or "").strip()
        login = str(data.get("login") or data.get("username") or "").strip()
        if not user_id or not login:
            raise GitSourceError("gitea_unavailable", "Gitea 用户信息不完整", status_code=503)
        return OAuthUserProfile(
            provider=self.provider,
            instance_url=self.instance_url,
            provider_user_id=user_id,
            provider_login=login,
            display_name=str(data.get("full_name") or login).strip(),
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
            f"{self.api_base}/user/repos",
            headers=self._headers(access_token),
            params={"page": str(page), "limit": "50"},
        )
        self._check_response_errors(response)
        if response.status_code != 200 or not isinstance(response.json(), list):
            raise GitSourceError("gitea_unavailable", "Gitea 未返回有效的仓库列表", status_code=503)
        raw_items = response.json()
        items: list[dict[str, Any]] = []
        for repo in raw_items:
            if repo.get("private") or repo.get("archived"):
                continue
            name = str(repo.get("name") or "").strip()
            full_name = str(repo.get("full_name") or "").strip()
            if not name.lower().startswith("astrbot_plugin_") or not full_name:
                continue
            if not self._permission_level(repo):
                continue
            try:
                ref = parse_repository_ref(f"https://gitea.com/{full_name}")
            except GitSourceError:
                continue
            owner = repo.get("owner") or {}
            items.append(
                {
                    "id": str(repo["id"]),
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
        next_page = page + 1 if len(raw_items) >= 50 else None
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
            f"{self.api_base}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}",
            headers=self._headers(token_set.access_token),
        )
        self._check_response_errors(resp, not_found_message="Gitea 仓库不存在或当前授权无法访问")
        if resp.status_code != 200:
            raise GitSourceError("gitea_unavailable", "Gitea 无法验证仓库权限", status_code=503)
        repo = resp.json()
        if repo.get("private") or repo.get("archived"):
            raise GitSourceError(
                "gitea_repository_unavailable",
                "请选择公开、未归档的 Gitea 仓库",
                status_code=403,
            )
        repo_id = str(repo.get("id") or "").strip()
        if expected_repository_id and repo_id != expected_repository_id:
            raise GitSourceError(
                "gitea_repository_changed",
                "Gitea 仓库身份已变化，请刷新列表重新选择",
                status_code=409,
            )
        perm = self._permission_level(repo, current_user_id=token_set.provider_user_id)
        if not perm:
            raise GitSourceError(
                "gitea_repository_permission_required",
                "你当前没有该 Gitea 仓库的写入或管理权限",
                status_code=403,
            )
        full_name = str(repo.get("full_name") or ref.full_path).strip()
        canonical_ref = parse_repository_ref(f"https://gitea.com/{full_name}")
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

    async def _fetch_raw_file(
        self,
        ref: RepositoryRef,
        path: str,
        branch: str,
        access_token: str = "",
    ) -> bytes | None:
        resp = await self.safe_request(
            "GET",
            f"{self.api_base}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"
            f"/raw/{quote(path, safe='/')}",
            headers=self._headers(access_token),
            params={"ref": branch} if branch else None,
        )
        if resp.status_code != 200:
            return None
        return bytes(resp.content or b"")

    async def _fetch_contents_dir(
        self,
        ref: RepositoryRef,
        path: str,
        branch: str,
        access_token: str = "",
    ) -> list[dict[str, Any]]:
        suffix = f"/{quote(path, safe='/')}" if path else ""
        resp = await self.safe_request(
            "GET",
            f"{self.api_base}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"
            f"/contents{suffix}",
            headers=self._headers(access_token),
            params={"ref": branch} if branch else None,
        )
        if resp.status_code != 200 or not isinstance(resp.json(), list):
            return []
        return list(resp.json())

    @staticmethod
    def _raw_web_url(ref: RepositoryRef, branch: str, filename: str) -> str:
        return (
            f"https://gitea.com/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"
            f"/raw/branch/{quote(branch, safe='')}/{quote(filename, safe='/')}"
        )

    async def fetch_repository_metadata(
        self,
        ref: RepositoryRef,
        *,
        access_token: str = "",
    ) -> dict[str, Any]:
        resp = await self.safe_request(
            "GET",
            f"{self.api_base}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}",
            headers=self._headers(access_token),
        )
        if resp.status_code == 404:
            return {}
        self._check_response_errors(resp)
        if resp.status_code != 200:
            return {}
        repo = resp.json()
        default_branch = str(repo.get("default_branch") or "main").strip() or "main"
        raw_meta = await self._fetch_raw_file(ref, "metadata.yaml", default_branch, access_token)
        if raw_meta is None:
            raw_meta = await self._fetch_raw_file(ref, "metadata.yml", default_branch, access_token)
        parsed_meta = parse_metadata_yaml_bytes(raw_meta or b"")
        root_entries = await self._fetch_contents_dir(ref, "", default_branch, access_token)
        root_files = {
            str(item.get("name") or ""): item
            for item in root_entries
            if item.get("type") == "file" and item.get("name")
        }
        root_lower = {name.lower(): name for name in root_files}
        payload: dict[str, Any] = {
            "stars": int(repo.get("stars_count") or 0),
            "updated_at": str(repo.get("updated_at") or ""),
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
            item.get("type") == "dir" and item.get("name") == ".astrbot-plugin"
            for item in root_entries
        ):
            i18n_entries = await self._fetch_contents_dir(
                ref, ".astrbot-plugin/i18n", default_branch, access_token
            )
            i18n: dict[str, dict[str, str]] = {}
            for entry in i18n_entries[:PLUGIN_I18N_MAX_FILES]:
                fname = str(entry.get("name") or "")
                if entry.get("type") != "file" or not fname.endswith(".json"):
                    continue
                locale = fname[:-5]
                if not PLUGIN_I18N_LOCALE_PATTERN.fullmatch(locale):
                    continue
                raw_i18n = await self._fetch_raw_file(
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
            f"{self.api_base}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}",
            headers=self._headers(access_token),
        )
        branch = "main"
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            branch = str(resp.json().get("default_branch") or "main").strip() or "main"
        candidates = (path,) if path else README_CANDIDATES
        for candidate in candidates:
            raw = await self._fetch_raw_file(ref, candidate, branch, access_token)
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
        resp = await self.safe_request(
            "GET",
            f"{self.api_base}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}",
            headers=self._headers(access_token),
        )
        self._check_response_errors(resp, not_found_message="Gitea 仓库不存在或无法访问")
        if resp.status_code != 200:
            raise GitSourceError("gitea_unavailable", "无法获取 Gitea 仓库信息", status_code=503)
        repo = resp.json()
        if repo.get("private") or repo.get("archived"):
            raise GitSourceError(
                "gitea_repository_unavailable",
                "CDN 仅支持公开且未归档的 Gitea 仓库",
                status_code=403,
            )
        target_ref = requested_ref.strip() or str(repo.get("default_branch") or "").strip()
        if not target_ref:
            raise GitSourceError(
                "gitea_default_branch_missing", "Gitea 仓库缺少默认分支", status_code=409
            )
        commit_resp = await self.safe_request(
            "GET",
            f"{self.api_base}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo_name, safe='')}"
            f"/git/commits/{quote(target_ref, safe='')}",
            headers=self._headers(access_token),
        )
        self._check_response_errors(commit_resp, not_found_message="Gitea 提交不存在")
        if commit_resp.status_code != 200:
            raise GitSourceError("gitea_commit_invalid", "Gitea 无法解析提交", status_code=503)
        commit_sha = str(commit_resp.json().get("sha") or "").lower()
        if not COMMIT_SHA_PATTERN.fullmatch(commit_sha):
            raise GitSourceError(
                "gitea_commit_invalid", "Gitea 未返回有效 commit SHA", status_code=503
            )
        full_name = str(repo.get("full_name") or ref.full_path).strip()
        canonical_ref = parse_repository_ref(f"https://gitea.com/{full_name}")
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
                f"{self.api_base}/repos/{quote(canonical_ref.owner, safe='')}"
                f"/{quote(canonical_ref.repo_name, safe='')}/archive/{commit_sha}.zip"
            ),
            repo_external_id=str(repo.get("id") or ""),
            headers=headers,
        )
