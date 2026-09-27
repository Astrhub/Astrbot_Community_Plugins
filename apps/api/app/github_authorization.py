"""User OAuth management access and administrator-authorized publication checks."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException, Request

from .artifacts.archive import normalize_github_repo
from .auth import is_admin

OAUTH_TTL = 8 * 60 * 60


def auth_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "error": message})


async def store_call(store: Any, method: str, *args: Any) -> Any:
    result = getattr(store, method)(*args)
    return await result if inspect.isawaitable(result) else result


def authorization_reference(session_token: str) -> str:
    return hashlib.sha256(session_token.encode()).hexdigest()


def oauth_cipher(client_secret: str) -> Fernet:
    if not client_secret:
        raise auth_error(503, "github_oauth_unconfigured", "GitHub 授权服务尚未配置")
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"astrhub/github-user-oauth/v1"
    ).derive(client_secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


async def save_authorization(
    store: Any,
    settings: Any,
    session: str,
    user_id: str,
    github_id: str,
    login: str,
    access_token: str,
) -> None:
    payload = json.dumps(
        {
            "user_id": str(user_id),
            "github_id": str(github_id),
            "login": login,
            "access_token": access_token,
        }
    ).encode()
    ciphertext = oauth_cipher(settings.github_client_secret).encrypt(payload).decode()
    await store_call(
        store,
        "set_github_authorization",
        authorization_reference(session),
        ciphertext,
        min(OAUTH_TTL, settings.session_max_age_seconds),
    )
    await store_call(
        store,
        "set_latest_github_authorization",
        str(user_id),
        authorization_reference(session),
        min(OAUTH_TTL, settings.session_max_age_seconds),
    )


class GithubRepositoryAccess:
    def __init__(
        self,
        store: Any,
        settings: Any,
        user: Mapping[str, Any],
        reference: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store, self.settings, self.user, self.reference = store, settings, user, reference
        self.transport = transport
        self._credential: dict[str, Any] | None = None
        self._organization_admin: dict[str, bool] = {}

    async def credential(self) -> dict[str, Any]:
        if self._credential is not None:
            return self._credential
        encrypted = await store_call(self.store, "get_github_authorization", self.reference)
        try:
            if not encrypted or not self.user.get("github_login"):
                raise InvalidToken()
            payload = json.loads(
                oauth_cipher(self.settings.github_client_secret).decrypt(
                    encrypted.encode(), ttl=OAUTH_TTL
                )
            )
            github_id = str(self.user.get("github_id") or self.user.get("id") or "")
            if payload["user_id"] != str(self.user["id"]) or payload["github_id"] != github_id:
                raise InvalidToken()
        except (InvalidToken, ValueError, KeyError):
            await store_call(self.store, "delete_github_authorization", self.reference)
            raise auth_error(
                409, "github_authorization_required", "请连接 GitHub 后选择仓库"
            ) from None
        self._credential = payload
        return payload

    async def request(self, path: str, *, params: dict | None = None) -> httpx.Response:
        credential = await self.credential()
        url = "https://api.github.com" + path
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=15,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Astrhub-Repository-Access",
                    "Authorization": "Bearer " + credential["access_token"],
                },
            ) as client:
                for _ in range(4):
                    response = await client.get(url, params=params)
                    if response.status_code not in {301, 302, 307, 308}:
                        break
                    target = urlsplit(response.headers.get("location", ""))
                    if target.scheme != "https" or target.netloc != "api.github.com":
                        raise auth_error(
                            503, "github_unavailable", "GitHub 仓库地址发生了无法验证的跳转"
                        )
                    url, params = target.geturl(), None
        except httpx.HTTPError:
            raise auth_error(
                503, "github_unavailable", "暂时无法验证 GitHub 权限，请稍后重试"
            ) from None
        if response.status_code == 401:
            await store_call(self.store, "delete_github_authorization", self.reference)
            raise auth_error(409, "github_authorization_required", "GitHub 授权已失效，请重新连接")
        if response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0":
            raise auth_error(503, "github_rate_limited", "GitHub 请求额度暂时不足，请稍后重试")
        if response.status_code == 403:
            raise auth_error(
                403,
                "github_authorization_denied",
                "GitHub 未允许访问，请确认该组织已允许此登录应用",
            )
        if response.status_code >= 500 or response.is_redirect:
            raise auth_error(503, "github_unavailable", "GitHub 暂时无法验证仓库权限")
        return response

    async def can_manage(self, repository: Mapping[str, Any]) -> bool:
        owner = repository.get("owner") or {}
        credential = await self.credential()
        if str(owner.get("id")) == credential["github_id"]:
            return True
        permissions = repository.get("permissions") or {}
        if any(permissions.get(key) is True for key in ("push", "maintain", "admin")):
            return True
        if owner.get("type") != "Organization":
            return False
        login = str(owner.get("login") or "")
        if login not in self._organization_admin:
            response = await self.request("/user/memberships/orgs/" + quote(login, safe=""))
            membership = response.json() if response.status_code == 200 else {}
            self._organization_admin[login] = bool(
                membership.get("state") == "active"
                and membership.get("role") == "admin"
                and str((membership.get("organization") or {}).get("id")) == str(owner.get("id"))
            )
        return self._organization_admin[login]

    @staticmethod
    def public_repository(repository: Mapping[str, Any]) -> dict[str, Any]:
        owner = repository["owner"]
        return {
            "id": str(repository["id"]),
            "name": repository["name"],
            "full_name": repository["full_name"],
            "repo": normalize_github_repo("https://github.com/" + repository["full_name"]),
            "owner": owner["login"],
            "owner_id": str(owner["id"]),
            "owner_type": owner["type"],
            "description": repository.get("description") or "",
        }

    async def list_repositories(self, page: int) -> dict[str, Any]:
        response = await self.request(
            "/user/repos",
            params={
                "visibility": "public",
                "affiliation": "owner,collaborator,organization_member",
                "per_page": 100,
                "page": page,
                "sort": "pushed",
                "direction": "desc",
            },
        )
        if response.status_code != 200 or not isinstance(response.json(), list):
            raise auth_error(503, "github_unavailable", "GitHub 未返回有效的仓库列表")
        items = []
        for repository in response.json():
            if not (
                str(repository.get("name", "")).startswith("astrbot_plugin_")
                and not repository.get("private")
                and not repository.get("archived")
                and not repository.get("disabled")
            ):
                continue
            try:
                if await self.can_manage(repository):
                    items.append(self.public_repository(repository))
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                # One restricted organization must not hide other accessible repositories.
        return {"items": items, "next_page": page + 1 if "next" in response.links else None}

    async def verify(self, repo: str, expected_repository_id: str = "") -> dict[str, Any]:
        canonical = normalize_github_repo(repo)
        response = await self.request("/repos/" + canonical.removeprefix("https://github.com/"))
        if response.status_code == 404:
            raise auth_error(403, "github_repository_unavailable", "仓库不存在或当前授权无法访问")
        if response.status_code != 200:
            raise auth_error(503, "github_unavailable", "GitHub 无法验证仓库权限")
        repository = response.json()
        if repository.get("private") or repository.get("archived") or repository.get("disabled"):
            raise auth_error(
                403, "github_repository_unavailable", "请选择公开、未归档的 GitHub 仓库"
            )
        if expected_repository_id and str(repository["id"]) != expected_repository_id:
            raise auth_error(409, "github_repository_changed", "仓库身份已变化，请刷新列表重新选择")
        if not await self.can_manage(repository):
            raise auth_error(
                403, "github_repository_permission_required", "你当前没有该仓库的写入或管理权限"
            )
        credential = await self.credential()
        return {
            **self.public_repository(repository),
            "github_user_id": credential["github_id"],
            "auth_reference": self.reference,
            "user_id": str(self.user["id"]),
            "verified_at": datetime.now(UTC).isoformat(),
        }


def access_for_request(request: Request, user: Mapping[str, Any]) -> GithubRepositoryAccess:
    factory = getattr(request.app.state, "github_access_factory", GithubRepositoryAccess)
    settings = request.app.state.settings
    session = request.cookies.get(settings.session_cookie_name, "")
    return factory(request.app.state.store, settings, user, authorization_reference(session))


async def require_repository_access(
    request: Request,
    user: dict[str, Any],
    repo: str,
    *,
    admin_override: bool = False,
    expected_repository_id: str = "",
) -> dict[str, Any]:
    if admin_override and is_admin(user):
        proof = {
            "mode": "site_admin",
            "user_id": str(user["id"]),
            "repo": normalize_github_repo(repo),
        }
    else:
        proof = await access_for_request(request, user).verify(repo, expected_repository_id)
    user["_repository_authorization"] = proof
    return proof


async def _verify_admin_publication(
    store: Any,
    user: Mapping[str, Any],
    artifact: Mapping[str, Any],
    plugin: Mapping[str, Any],
    proof: Mapping[str, Any],
    approval: Mapping[str, Any],
    loader: Callable[[str], Awaitable[Mapping[str, Any]]] | None,
) -> None:
    reviewer = await store_call(store, "get_user_by_id", approval.get("reviewer_user_id"))
    if not reviewer or not is_admin(reviewer):
        raise auth_error(403, "reviewer_role_changed", "批准人的管理权限已失效")
    if str(user.get("github_id") or user["id"]) != str(proof.get("github_user_id") or ""):
        raise auth_error(403, "github_identity_changed", "提交者的 GitHub 身份已变化")
    if not proof.get("id") or not proof.get("owner_id") or not proof.get("verified_at"):
        raise auth_error(403, "github_authorization_required", "缺少提交时的仓库权限证据")
    approved_sha = (approval.get("metadata") or {}).get("archive_sha256")
    if approved_sha and approved_sha != artifact["archive_sha256"]:
        raise auth_error(409, "artifact_identity_changed", "批准的插件包与当前包不一致")
    canonical = normalize_github_repo(plugin["repo"])
    if any(
        normalize_github_repo(str(value or "")) != canonical
        for value in (proof.get("repo"), artifact.get("source_repo"))
    ):
        raise auth_error(409, "github_repository_changed", "提交后仓库地址已变化")
    if loader is None:
        raise auth_error(503, "site_github_authorization_unavailable", "站点仓库复核尚未配置")
    current = await loader(canonical)
    if (
        current.get("private") is not False
        or current.get("archived") is not False
        or current.get("disabled", False) is not False
    ):
        raise auth_error(403, "github_repository_unavailable", "仓库必须公开且未归档或禁用")
    if (
        str(current.get("id") or "") != str(proof["id"])
        or str((current.get("owner") or {}).get("id") or "") != str(proof["owner_id"])
        or "https://github.com/" + str(current.get("full_name") or "") != canonical
    ):
        raise auth_error(409, "github_repository_changed", "GitHub 仓库身份或所属方已变化")


async def verify_publication_access(
    request: Request,
    artifact_id: str,
    *,
    site_repository_loader: Callable[[str], Awaitable[Mapping[str, Any]]] | None = None,
) -> dict[str, str]:
    repository = request.app.state.artifact_runtime.repository
    artifact = await repository.get_artifact(artifact_id)
    if not artifact:
        raise auth_error(404, "artifact_not_found", "插件版本不存在")
    proof = (artifact.get("submitted_by_snapshot") or {}).get("repository_authorization")
    if not proof:
        raise auth_error(403, "github_authorization_required", "该版本缺少 GitHub 权限凭据")
    store = request.app.state.store
    user = await store_call(store, "get_user_by_id", artifact.get("submitted_by"))
    plugin = await store_call(store, "get_plugin", artifact["plugin_id"])
    if (
        not user
        or not plugin
        or str(plugin.get("owner_user_id")) != proof.get("plugin_owner_user_id")
    ):
        raise auth_error(403, "plugin_owner_changed", "插件维护者已变化，需要重新提交")
    if str(user["id"]) != proof.get("user_id"):
        raise auth_error(403, "github_identity_changed", "提交身份已变化")
    admin_approval = None
    if artifact.get("review_status") == "approved":
        decisions = await repository.list_review_decisions(artifact_id)
        admin_approval = next(
            (
                decision
                for decision in reversed(decisions)
                if decision.get("source") == "admin"
                and decision.get("action") in {"approve", "manual_approve"}
                and decision.get("to_status") == "approved"
            ),
            None,
        )
    from .cdn import CdnSubscriptions, cdn_enabled, verify_subscription

    if not cdn_enabled(plugin):
        raise auth_error(409, "cdn_disabled_or_changed", "该插件已关闭 CDN")
    if proof.get("mode") == "cdn_subscription":
        subscription = await CdnSubscriptions(store).get(plugin["id"])
        if (
            subscription is None
            or site_repository_loader is None
            or artifact.get("review_status") != "approved"
        ):
            raise auth_error(403, "cdn_authorization_required", "缺少有效的 CDN 授权或批准")
        actor, _ = await verify_subscription(store, plugin, subscription, site_repository_loader)
        bound = subscription["authorization"]
        if (
            str(actor["id"]) != str(user["id"])
            or artifact["source_repo"] != plugin["repo"]
            or any(str(proof.get(key)) != str(bound.get(key)) for key in ("id", "owner_id", "repo"))
        ):
            raise auth_error(403, "cdn_identity_changed", "CDN 提交来源已变化")
        if admin_approval:
            reviewer = await store_call(
                store, "get_user_by_id", admin_approval.get("reviewer_user_id")
            )
            if not reviewer or not is_admin(reviewer):
                raise auth_error(403, "reviewer_role_changed", "批准人的管理权限已失效")
            approved_sha = (admin_approval.get("metadata") or {}).get("archive_sha256")
            if approved_sha and approved_sha != artifact["archive_sha256"]:
                raise auth_error(409, "artifact_identity_changed", "批准包已变化")
        return {
            "artifact_id": artifact_id,
            "archive_sha256": artifact["archive_sha256"],
            "repo": plugin["repo"],
            "owner_user_id": str(plugin["owner_user_id"]),
        }
    if proof.get("mode") == "site_admin":
        if not is_admin(user) or normalize_github_repo(plugin["repo"]) != proof["repo"]:
            raise auth_error(403, "github_identity_changed", "管理权限或插件仓库已变化")
    elif admin_approval is not None:
        await _verify_admin_publication(
            store, user, artifact, plugin, proof, admin_approval, site_repository_loader
        )
    else:
        factory = getattr(request.app.state, "github_access_factory", GithubRepositoryAccess)
        access = factory(store, request.app.state.settings, user, proof["auth_reference"])
        try:
            current = await access.verify(plugin["repo"], proof["id"])
        except HTTPException as exc:
            if (
                not isinstance(exc.detail, dict)
                or exc.detail.get("code") != "github_authorization_required"
            ):
                raise
            reference = await store_call(store, "get_latest_github_authorization", str(user["id"]))
            if not reference or reference == proof["auth_reference"]:
                raise
            current = await factory(store, request.app.state.settings, user, reference).verify(
                plugin["repo"], proof["id"]
            )
        if (
            current["github_user_id"] != proof["github_user_id"]
            or current["owner_id"] != proof["owner_id"]
        ):
            raise auth_error(403, "github_repository_changed", "GitHub 身份或仓库所属方已变化")
    return {
        "artifact_id": artifact_id,
        "archive_sha256": artifact["archive_sha256"],
        "repo": plugin["repo"],
        "owner_user_id": str(plugin["owner_user_id"]),
    }
