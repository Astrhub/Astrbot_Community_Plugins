from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile

from ..auth import can_edit_plugin, is_admin
from .archive import PLUGIN_NAME_PATTERN, PrecheckError, normalize_github_repo
from .comments import ReviewCommentError
from .content import ArtifactContentError
from .github_source import GithubSourceError
from .models import ArtifactStateError
from .schemas import (
    ArtifactDecisionPayload,
    ArtifactDiffContentResponse,
    ArtifactDiffListResponse,
    ArtifactDetailResponse,
    ArtifactEnvelope,
    ArtifactFileContentResponse,
    ArtifactFileListResponse,
    GithubArtifactSubmission,
    PluginRegistrationPayload,
    ReviewFindingListResponse,
    ReviewCommentAddressedPayload,
    ReviewCommentBodyMutationPayload,
    ReviewCommentCreatePayload,
    ReviewCommentEnvelope,
    ReviewCommentListResponse,
    ReviewCommentStateMutationPayload,
    ReviewRunListResponse,
)
from .service import (
    ArtifactService,
    ArtifactServiceError,
    public_artifact,
    public_review_finding,
    public_review_run,
)
from .storage import ArtifactStorageError

ZIP_FILENAME_PATTERN = re.compile(r"\.zip$", re.IGNORECASE)
PRIVATE_READ_HEADERS = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}


def build_artifact_router() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/plugins/registrations",
        tags=["artifacts"],
        summary="登记插件身份",
    )
    async def register_plugin(
        request: Request, payload: PluginRegistrationPayload
    ) -> dict[str, Any]:
        _require_service(request)
        user = await _require_user(request)
        _ensure_submission_allowed(user)
        data = payload.model_dump()
        data["category_explicit"] = "category" in payload.model_fields_set
        try:
            canonical_repo = normalize_github_repo(data["repo"])
        except PrecheckError as exc:
            raise _http_error(400, exc.code, str(exc)) from exc
        if not PLUGIN_NAME_PATTERN.fullmatch(data["name"]):
            raise _http_error(400, "plugin_name_invalid", "插件名必须使用 astrbot_plugin_ 小写命名")
        owner = canonical_repo.removeprefix("https://github.com/").split("/", 1)[0]
        if not is_admin(user) and owner.lower() != str(user.get("github_login") or "").lower():
            raise _http_error(403, "repo_owner_mismatch", "GitHub 账号必须拥有该仓库")
        data["repo"] = canonical_repo
        data["id"] = data["name"]
        try:
            plugin = await _call_store(request, "register_plugin", user, data)
        except PermissionError as exc:
            raise _http_error(403, "plugin_owner_mismatch", "插件已由其他用户登记") from exc
        return {"plugin": plugin}

    @router.post(
        "/v1/plugins/{plugin_id}/artifacts/upload",
        tags=["artifacts"],
        summary="上传插件 ZIP",
        status_code=202,
    )
    async def upload_artifact(
        request: Request,
        plugin_id: str,
        file: UploadFile = File(...),
        supersedes_artifact_id: str = Form(default=""),
    ) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        _ensure_submission_allowed(user)
        await _enforce_submission_rate_limit(request, user)
        plugin = await _owned_plugin(request, plugin_id, user)
        if not file.filename or not ZIP_FILENAME_PATTERN.search(file.filename):
            raise _http_error(400, "invalid_zip_filename", "仅接受 .zip 插件包")
        try:
            artifact = await service.submit_upload(
                plugin=plugin,
                user=user,
                stream=_upload_stream(file),
                supersedes_artifact_id=supersedes_artifact_id,
            )
        except Exception as exc:
            _raise_artifact_error(exc)
        finally:
            await file.close()
        return {"artifact": artifact}

    @router.post(
        "/v1/plugins/{plugin_id}/artifacts/github",
        tags=["artifacts"],
        summary="从 GitHub 创建插件包",
        status_code=202,
    )
    async def submit_github_artifact(
        request: Request,
        plugin_id: str,
        payload: GithubArtifactSubmission,
    ) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        _ensure_submission_allowed(user)
        await _enforce_submission_rate_limit(request, user)
        plugin = await _owned_plugin(request, plugin_id, user)
        try:
            artifact = await service.submit_github(
                plugin=plugin,
                user=user,
                source_ref=payload.source_ref,
                supersedes_artifact_id=payload.supersedes_artifact_id,
            )
        except Exception as exc:
            _raise_artifact_error(exc)
        return {"artifact": artifact}

    @router.get(
        "/v1/me/artifacts",
        tags=["artifacts"],
        summary="我的插件版本",
    )
    async def my_artifacts(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        items = await service.repository.list_user_artifacts(str(user["id"]), limit, offset)
        return {
            "items": [public_artifact(item) for item in items],
            "limit": limit,
            "offset": offset,
        }

    @router.get(
        "/v1/artifacts/{artifact_id}",
        tags=["artifacts"],
        summary="查看插件版本审查详情",
        response_model=ArtifactDetailResponse,
    )
    async def artifact_detail(request: Request, artifact_id: str) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        artifact = await _visible_artifact(service, artifact_id, user)
        detail = await service.artifact_detail(str(artifact["id"]))
        if not detail:
            raise _http_error(404, "artifact_not_found", "Artifact 不存在")
        return detail

    @router.get(
        "/v1/artifacts/{artifact_id}/runs",
        tags=["artifacts"],
        summary="查看自动审查运行记录",
        response_model=ReviewRunListResponse,
    )
    async def artifact_runs(request: Request, artifact_id: str) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        await _visible_artifact(service, artifact_id, user)
        return {
            "items": [
                public_review_run(run)
                for run in await service.repository.list_review_runs(artifact_id)
            ]
        }

    @router.get(
        "/v1/artifacts/{artifact_id}/findings",
        tags=["artifacts"],
        summary="查看结构化风险发现",
        response_model=ReviewFindingListResponse,
    )
    async def artifact_findings(request: Request, artifact_id: str) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        await _visible_artifact(service, artifact_id, user)
        return {
            "items": [
                public_review_finding(finding)
                for finding in await service.repository.list_findings(artifact_id)
            ]
        }

    @router.get(
        "/v1/artifacts/{artifact_id}/files",
        tags=["artifacts"],
        summary="查看 Artifact 文件树",
        response_model=ArtifactFileListResponse,
    )
    async def artifact_files(
        request: Request,
        response: Response,
        artifact_id: str,
        limit: int = Query(default=200),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        artifact = await _visible_artifact(service, artifact_id, user)
        _private_read_headers(response)
        try:
            return await service.artifact_files(artifact, limit=limit, offset=offset)
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.get(
        "/v1/artifacts/{artifact_id}/files/{file_id}/content",
        tags=["artifacts"],
        summary="分页读取 Artifact 文本文件",
        response_model=ArtifactFileContentResponse,
    )
    async def artifact_file_content(
        request: Request,
        response: Response,
        artifact_id: str,
        file_id: str,
        start_line: int = Query(default=1),
        line_limit: int = Query(default=200),
    ) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        artifact = await _visible_artifact(service, artifact_id, user)
        _private_read_headers(response)
        try:
            return await service.artifact_file_content(
                artifact,
                file_id,
                start_line=start_line,
                line_limit=line_limit,
            )
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.get(
        "/v1/artifacts/{artifact_id}/diff",
        tags=["artifacts"],
        summary="查看 Artifact 文件差异",
        response_model=ArtifactDiffListResponse,
    )
    async def artifact_diffs(
        request: Request,
        response: Response,
        artifact_id: str,
        limit: int = Query(default=200),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        artifact = await _visible_artifact(service, artifact_id, user)
        _private_read_headers(response)
        try:
            return await service.artifact_diffs(artifact, limit=limit, offset=offset)
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.get(
        "/v1/artifacts/{artifact_id}/diff/{diff_id}",
        tags=["artifacts"],
        summary="读取 Artifact 受限 Diff Hunks",
        response_model=ArtifactDiffContentResponse,
    )
    async def artifact_diff_content(
        request: Request,
        response: Response,
        artifact_id: str,
        diff_id: str,
        hunk_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        artifact = await _visible_artifact(service, artifact_id, user)
        _private_read_headers(response)
        try:
            return await service.artifact_diff_content(
                artifact,
                diff_id,
                hunk_id=hunk_id,
            )
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.get(
        "/v1/artifacts/{artifact_id}/comments",
        tags=["artifacts"],
        summary="查看 Artifact 行级审查评论",
        response_model=ReviewCommentListResponse,
    )
    async def artifact_comments(
        request: Request,
        response: Response,
        artifact_id: str,
        limit: int = Query(default=20),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        service = _require_service(request)
        user = await _require_user(request)
        artifact = await _visible_artifact(service, artifact_id, user)
        _private_read_headers(response)
        try:
            return await service.artifact_comments(artifact, limit=limit, offset=offset)
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.post(
        "/v1/admin/artifacts/{artifact_id}/comments",
        tags=["reviews"],
        summary="创建 Artifact 行级审查评论",
        response_model=ReviewCommentEnvelope,
        status_code=201,
    )
    async def create_artifact_comment(
        request: Request,
        response: Response,
        artifact_id: str,
        payload: ReviewCommentCreatePayload,
    ) -> dict[str, Any]:
        service = _require_service(request)
        actor = await _require_admin(request)
        artifact = await _visible_artifact(service, artifact_id, actor)
        _private_read_headers(response)
        try:
            comment = await service.create_review_comment(
                artifact=artifact,
                actor=actor,
                file_id=payload.file_id,
                side=payload.side,
                line_start=payload.line_start,
                line_end=payload.line_end,
                body=payload.body,
                diff_id=payload.diff_id,
                hunk_id=payload.hunk_id,
                source_thread_id=payload.source_thread_id,
                idempotency_key=_request_idempotency_key(request, payload.idempotency_key),
            )
            return {"comment": comment}
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.post(
        "/v1/artifacts/{artifact_id}/comments/{thread_id}/replies",
        tags=["artifacts"],
        summary="回复 Artifact 审查评论",
        response_model=ReviewCommentEnvelope,
    )
    async def reply_artifact_comment(
        request: Request,
        response: Response,
        artifact_id: str,
        thread_id: str,
        payload: ReviewCommentBodyMutationPayload,
    ) -> dict[str, Any]:
        service = _require_service(request)
        actor = await _require_user(request)
        artifact = await _visible_artifact(service, artifact_id, actor)
        _private_read_headers(response)
        try:
            comment = await service.mutate_review_comment(
                artifact=artifact,
                thread_id=thread_id,
                actor=actor,
                event_type="reply",
                expected_version=payload.expected_version,
                body=payload.body,
                idempotency_key=_request_idempotency_key(request, payload.idempotency_key),
            )
            return {"comment": comment}
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.post(
        "/v1/artifacts/{artifact_id}/comments/{thread_id}/author-addressed",
        tags=["artifacts"],
        summary="标记 Artifact 审查评论已处理",
        response_model=ReviewCommentEnvelope,
    )
    async def address_artifact_comment(
        request: Request,
        response: Response,
        artifact_id: str,
        thread_id: str,
        payload: ReviewCommentAddressedPayload,
    ) -> dict[str, Any]:
        service = _require_service(request)
        actor = await _require_user(request)
        artifact = await _visible_artifact(service, artifact_id, actor)
        _private_read_headers(response)
        try:
            comment = await service.mutate_review_comment(
                artifact=artifact,
                thread_id=thread_id,
                actor=actor,
                event_type="author_addressed",
                expected_version=payload.expected_version,
                body=payload.body,
                idempotency_key=_request_idempotency_key(request, payload.idempotency_key),
            )
            return {"comment": comment}
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.post(
        "/v1/admin/artifacts/{artifact_id}/comments/{thread_id}/edit",
        tags=["reviews"],
        summary="编辑自己的 Artifact 审查评论",
        response_model=ReviewCommentEnvelope,
    )
    async def edit_artifact_comment(
        request: Request,
        response: Response,
        artifact_id: str,
        thread_id: str,
        payload: ReviewCommentBodyMutationPayload,
    ) -> dict[str, Any]:
        return await _admin_comment_mutation(
            request,
            response,
            artifact_id,
            thread_id,
            payload.expected_version,
            payload.body,
            _request_idempotency_key(request, payload.idempotency_key),
            "edit",
        )

    @router.post(
        "/v1/admin/artifacts/{artifact_id}/comments/{thread_id}/resolve",
        tags=["reviews"],
        summary="解决 Artifact 审查评论",
        response_model=ReviewCommentEnvelope,
    )
    async def resolve_artifact_comment(
        request: Request,
        response: Response,
        artifact_id: str,
        thread_id: str,
        payload: ReviewCommentStateMutationPayload,
    ) -> dict[str, Any]:
        return await _admin_comment_mutation(
            request,
            response,
            artifact_id,
            thread_id,
            payload.expected_version,
            "",
            _request_idempotency_key(request, payload.idempotency_key),
            "resolve",
        )

    @router.post(
        "/v1/admin/artifacts/{artifact_id}/comments/{thread_id}/reopen",
        tags=["reviews"],
        summary="重开 Artifact 审查评论",
        response_model=ReviewCommentEnvelope,
    )
    async def reopen_artifact_comment(
        request: Request,
        response: Response,
        artifact_id: str,
        thread_id: str,
        payload: ReviewCommentStateMutationPayload,
    ) -> dict[str, Any]:
        return await _admin_comment_mutation(
            request,
            response,
            artifact_id,
            thread_id,
            payload.expected_version,
            "",
            _request_idempotency_key(request, payload.idempotency_key),
            "reopen",
        )

    async def _admin_comment_mutation(
        request: Request,
        response: Response,
        artifact_id: str,
        thread_id: str,
        expected_version: int,
        body: str,
        idempotency_key: str,
        event_type: str,
    ) -> dict[str, Any]:
        service = _require_service(request)
        actor = await _require_admin(request)
        artifact = await _visible_artifact(service, artifact_id, actor)
        _private_read_headers(response)
        try:
            comment = await service.mutate_review_comment(
                artifact=artifact,
                thread_id=thread_id,
                actor=actor,
                event_type=event_type,
                expected_version=expected_version,
                body=body,
                idempotency_key=idempotency_key,
            )
            return {"comment": comment}
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.get(
        "/v1/admin/artifacts",
        tags=["reviews"],
        summary="待审 Artifact 队列",
    )
    async def admin_artifacts(
        request: Request,
        review_status: str = Query(default=""),
        risk_level: str = Query(default=""),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        service = _require_service(request)
        await _require_admin(request)
        items = await service.repository.list_review_queue(
            review_status=review_status,
            risk_level=risk_level,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [public_artifact(item) for item in items],
            "limit": limit,
            "offset": offset,
        }

    @router.post(
        "/v1/admin/artifacts/{artifact_id}/approve",
        tags=["reviews"],
        summary="批准并排队发布 Artifact",
        response_model=ArtifactEnvelope,
    )
    async def approve_artifact(
        request: Request,
        artifact_id: str,
        payload: ArtifactDecisionPayload,
    ) -> dict[str, Any]:
        service = _require_service(request)
        reviewer = await _require_admin(request)
        try:
            artifact = await service.approve(
                artifact_id=artifact_id,
                reviewer=reviewer,
                reason=payload.reason,
                idempotency_key=payload.idempotency_key
                or request.headers.get("idempotency-key", "").strip(),
            )
            await service.enqueue_status_event(
                artifact=artifact,
                event_type="artifact_approved",
                suffix="approved",
            )
            return {"artifact": artifact}
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.post(
        "/v1/admin/artifacts/{artifact_id}/reject",
        tags=["reviews"],
        summary="拒绝 Artifact",
        response_model=ArtifactEnvelope,
    )
    async def reject_artifact(
        request: Request,
        artifact_id: str,
        payload: ArtifactDecisionPayload,
    ) -> dict[str, Any]:
        service = _require_service(request)
        reviewer = await _require_admin(request)
        try:
            artifact = await service.reject(
                artifact_id=artifact_id,
                reviewer=reviewer,
                reason=payload.reason,
                idempotency_key=payload.idempotency_key
                or request.headers.get("idempotency-key", "").strip(),
            )
            return {"artifact": artifact}
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.post(
        "/v1/admin/artifacts/{artifact_id}/request-changes",
        tags=["reviews"],
        summary="要求修改 Artifact",
        response_model=ArtifactEnvelope,
    )
    async def request_artifact_changes(
        request: Request,
        artifact_id: str,
        payload: ArtifactDecisionPayload,
    ) -> dict[str, Any]:
        service = _require_service(request)
        reviewer = await _require_admin(request)
        try:
            artifact = await service.request_changes(
                artifact_id=artifact_id,
                reviewer=reviewer,
                reason=payload.reason,
                idempotency_key=payload.idempotency_key
                or request.headers.get("idempotency-key", "").strip(),
            )
            return {"artifact": artifact}
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.post(
        "/v1/admin/artifacts/{artifact_id}/retry-publish",
        tags=["reviews"],
        summary="重试发布 Artifact",
    )
    async def retry_publish(request: Request, artifact_id: str) -> dict[str, Any]:
        service = _require_service(request)
        reviewer = await _require_admin(request)
        try:
            return await service.retry_publish(artifact_id, reviewer=reviewer)
        except Exception as exc:
            _raise_artifact_error(exc)

    @router.post(
        "/v1/admin/plugins/{plugin_id}/revoke-release",
        tags=["reviews"],
        summary="撤回当前 CDN 版本",
    )
    async def revoke_release(
        request: Request,
        plugin_id: str,
        payload: ArtifactDecisionPayload,
    ) -> dict[str, Any]:
        service = _require_service(request)
        reviewer = await _require_admin(request)
        plugin = await _call_store(request, "get_plugin", plugin_id)
        if not plugin:
            raise _http_error(404, "plugin_not_found", "插件不存在")
        artifact_id = str(plugin.get("current_artifact_id") or "")
        if not artifact_id:
            raise _http_error(409, "published_artifact_missing", "插件没有当前 CDN 版本")
        try:
            return await service.request_revoke(
                artifact_id=artifact_id,
                reason=payload.reason,
                idempotency_key=payload.idempotency_key
                or request.headers.get("idempotency-key", "").strip(),
                reviewer=reviewer,
            )
        except Exception as exc:
            _raise_artifact_error(exc)

    return router


async def _upload_stream(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(1024 * 1024):
        yield chunk


def _require_service(request: Request) -> ArtifactService:
    runtime = request.app.state.artifact_runtime
    if not runtime.available or runtime.service is None:
        raise _http_error(503, "artifact_service_unavailable", "插件包服务尚未启用或配置不完整")
    return runtime.service


async def _call_store(request: Request, method_name: str, *args: Any) -> Any:
    method = getattr(request.app.state.store, method_name)
    result = method(*args)
    return await result if inspect.isawaitable(result) else result


async def _require_user(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        user = await _call_store(request, "get_user_by_session", token)
        if user:
            return user
    if settings.enable_dev_auth:
        login = request.headers.get("x-dev-github-login", "").strip()
        if login:
            return await _call_store(
                request,
                "upsert_github_user",
                {"login": login, "name": login},
            )
    raise _http_error(401, "not_authenticated", "Not authenticated")


async def _require_admin(request: Request) -> dict[str, Any]:
    user = await _require_user(request)
    if not is_admin(user):
        raise _http_error(403, "admin_required", "Forbidden")
    return user


async def _owned_plugin(
    request: Request, plugin_id: str, user: Mapping[str, Any]
) -> dict[str, Any]:
    plugin = await _call_store(request, "get_plugin", plugin_id)
    if not plugin:
        raise _http_error(404, "plugin_not_found", "插件不存在")
    if not can_edit_plugin(user, plugin):
        raise _http_error(403, "plugin_owner_required", "只能提交自己名下的插件")
    return plugin


async def _visible_artifact(
    service: ArtifactService, artifact_id: str, user: Mapping[str, Any]
) -> dict[str, Any]:
    artifact = await service.repository.get_artifact(artifact_id)
    if not artifact:
        raise _http_error(404, "artifact_not_found", "Artifact 不存在")
    visible = is_admin(user) or str(artifact.get("submitted_by") or "") == str(user.get("id"))
    visible = visible or str(artifact.get("owner_user_id") or "") == str(user.get("id"))
    if not visible:
        raise _http_error(403, "artifact_forbidden", "无权查看该 Artifact")
    return artifact


def _raise_artifact_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, ArtifactServiceError):
        raise _http_error(exc.status_code, exc.code, str(exc)) from exc
    if isinstance(exc, ReviewCommentError):
        raise _http_error(
            exc.status_code,
            exc.code,
            str(exc),
            headers=PRIVATE_READ_HEADERS,
        ) from exc
    if isinstance(exc, ArtifactContentError):
        raise _http_error(
            exc.status_code,
            exc.code,
            str(exc),
            headers=PRIVATE_READ_HEADERS,
        ) from exc
    if isinstance(exc, GithubSourceError):
        status = 503 if exc.retryable else 400
        raise _http_error(status, exc.code, str(exc)) from exc
    if isinstance(exc, ArtifactStorageError):
        status = 413 if exc.code == "archive_too_large" else 400
        raise _http_error(status, exc.code, str(exc)) from exc
    if isinstance(exc, PrecheckError):
        raise _http_error(400, exc.code, str(exc)) from exc
    if isinstance(exc, ArtifactStateError):
        status = 403 if exc.code == "self_approval_forbidden" else 409
        raise _http_error(status, exc.code, str(exc)) from exc
    raise exc


def _ensure_submission_allowed(user: Mapping[str, Any]) -> None:
    value = str(user.get("muted_until") or "").strip()
    if not value:
        return
    try:
        muted_until = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return
    if muted_until.astimezone(UTC) > datetime.now(UTC):
        raise _http_error(403, "user_muted", "账号禁言期间不能提交插件版本")


async def _enforce_submission_rate_limit(request: Request, user: Mapping[str, Any]) -> None:
    runtime = request.app.state.artifact_runtime
    rpm = int(runtime.config.submission_rpm)
    if rpm <= 0:
        return
    now = int(datetime.now(UTC).timestamp())
    window = now // 60
    retry_after = 60 - (now % 60)
    user_id = str(user.get("id") or user.get("github_login") or "unknown")
    client_ip = str(request.client.host if request.client else "unknown")
    for scope, subject in (("user", user_id), ("ip", client_ip)):
        key = f"rate_limit:artifact_submission:{scope}:{subject}:{window}"
        count = await _increment_rate_limit(request, key, now + retry_after, now)
        if count > rpm:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "artifact_submission_rate_limited",
                    "message": f"提交过于频繁，请在 {retry_after} 秒后重试",
                },
                headers={"Retry-After": str(retry_after)},
            )


async def _increment_rate_limit(request: Request, key: str, expires_at: int, now: int) -> int:
    redis_client = getattr(getattr(request.app.state, "store", None), "redis", None)
    if redis_client is not None:
        try:
            count = int(await redis_client.incr(key))
            if count == 1:
                await redis_client.expire(key, max(1, expires_at - now + 5))
            return count
        except Exception:
            pass
    counters = getattr(request.app.state, "rate_limit_counters", None)
    if not isinstance(counters, dict):
        counters = {}
        request.app.state.rate_limit_counters = counters
    if len(counters) > 1000:
        for counter_key, entry in list(counters.items()):
            if int(entry.get("expires_at") or 0) <= now:
                counters.pop(counter_key, None)
    entry = counters.get(key)
    if not entry or int(entry.get("expires_at") or 0) <= now:
        entry = {"count": 0, "expires_at": expires_at}
        counters[key] = entry
    entry["count"] = int(entry.get("count") or 0) + 1
    return int(entry["count"])


def _http_error(
    status: int,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message},
        headers=dict(headers or {}),
    )


def _private_read_headers(response: Response) -> None:
    response.headers.update(PRIVATE_READ_HEADERS)


def _request_idempotency_key(request: Request, payload_value: str | None) -> str:
    return str(payload_value or request.headers.get("idempotency-key", "")).strip()
