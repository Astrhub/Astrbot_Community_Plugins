from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from ..auth import is_admin
from ..config import Settings
from .mail import send_artifact_status_email
from .repository import ArtifactRepository

LOGGER = logging.getLogger(__name__)
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

STATUS_COPY: dict[str, tuple[str, str]] = {
    "artifact_submitted": (
        "[自动审查] 插件版本已提交",
        "插件包已进入隔离队列，基础校验完成后可在站内查看结果。",
    ),
    "artifact_precheck_failed": (
        "[自动审查] 插件未通过基础校验",
        "该版本未通过基础校验，不会进入人工审查，也不会提供插件源 CDN 下载链接；用户仍可选择 GitHub 直连。",
    ),
    "artifact_pending_review": (
        "[自动审查] 插件等待人工复核",
        "基础校验和静态扫描已完成，版本正在等待管理员人工复核。",
    ),
    "artifact_rejected": (
        "[审查结果] 插件版本未通过",
        "该版本未通过审查，不会提供插件源 CDN 下载链接；用户仍可选择 GitHub 直连。",
    ),
    "artifact_changes_requested": (
        "[审查结果] 插件版本需要修改",
        "该版本需要修改，不会提供插件源 CDN 下载链接；用户仍可选择 GitHub 直连。请进入站内工作台查看审查意见。",
    ),
    "artifact_approved": (
        "[审查结果] 插件版本已批准",
        "该版本已通过人工复核，正在排队发布不可变 CDN 插件包。",
    ),
    "artifact_published": (
        "[发布结果] 插件 CDN 包已发布",
        "该版本的 CDN 插件包已发布，插件源将在仓库版本一致时提供下载链接。",
    ),
    "artifact_publish_failed": (
        "[发布结果] 插件 CDN 包发布失败",
        "该版本已通过审查，但 CDN 发布失败；当前稳定 CDN 包不会被覆盖。",
    ),
    "artifact_processing_failed": (
        "[自动审查] 插件版本处理失败",
        "自动审查基础设施处理失败，版本未进入人工复核；当前稳定 CDN 包不会被覆盖。",
    ),
    "artifact_revoked": (
        "[安全处置] 插件 CDN 包已下架",
        "管理员已撤回当前 CDN 插件包。请进入站内工作台查看原因。",
    ),
}


class ArtifactNotificationDispatcher:
    def __init__(
        self,
        *,
        repository: ArtifactRepository,
        store: Any,
        settings: Settings,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        self.repository = repository
        self.store = store
        self.settings = settings
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def rebind_store(self, store: Any) -> None:
        self.store = store

    async def run_once(self, limit: int = 10) -> int:
        events = await self.repository.claim_outbox(
            self.worker_id,
            limit,
            self.lease_seconds,
        )
        for event in events:
            try:
                await self._deliver(event)
            except Exception as exc:
                LOGGER.warning("Artifact notification %s failed: %s", event["id"], exc)
                await self.repository.fail_outbox(
                    str(event["id"]),
                    self.worker_id,
                    error_message=" ".join(str(exc).split())[:500],
                    retry=True,
                    retry_delay_seconds=min(300, 2 ** min(int(event.get("attempts") or 1), 8)),
                )
            else:
                await self.repository.complete_outbox(str(event["id"]), self.worker_id)
        return len(events)

    async def _deliver(self, event: Mapping[str, Any]) -> None:
        title, body = STATUS_COPY.get(
            str(event.get("event_type") or ""),
            ("[插件工作台] 状态更新", "插件版本状态已更新。"),
        )
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        artifact_id = str(payload.get("artifact_id") or event.get("aggregate_id") or "")
        link = self._workbench_link(artifact_id)
        reason = str(payload.get("reason") or payload.get("code") or "").strip()
        full_body = f"{body}\n"
        if reason:
            full_body += f"原因：{reason}\n"
        full_body += f"查看详情：{link}"

        recipient_id = str(event.get("recipient_user_id") or "")
        if recipient_id:
            recipient = await self._call_store("get_user_by_id", recipient_id)
            if recipient:
                await self._notify_user(
                    recipient,
                    event_id=str(event["id"]),
                    event_type=str(event["event_type"]),
                    title=title,
                    body=full_body,
                    artifact_id=artifact_id,
                    email_preference="email_notify_unlist"
                    if event.get("event_type") == "artifact_revoked"
                    else "email_notify_plugin_review",
                )

        if event.get("event_type") == "artifact_pending_review":
            users = await self._call_store("list_users")
            for admin in users or []:
                if not is_admin(admin) or str(admin.get("id") or "") == recipient_id:
                    continue
                await self._notify_user(
                    admin,
                    event_id=f"{event['id']}:admin:{admin.get('id')}",
                    event_type="artifact_pending_review",
                    title="[待审队列] 有新的插件版本待复核",
                    body=full_body,
                    artifact_id=artifact_id,
                    email_preference="email_notify_pending_review",
                )

    async def _notify_user(
        self,
        user: Mapping[str, Any],
        *,
        event_id: str,
        event_type: str,
        title: str,
        body: str,
        artifact_id: str,
        email_preference: str,
    ) -> None:
        await self._call_store(
            "create_notification_once",
            str(user["id"]),
            title,
            body,
            "plugin_artifact",
            {
                "artifact_id": artifact_id,
                "event_type": event_type,
                "outbox_event_id": event_id,
            },
            event_id,
        )
        if self.settings.email_provider == "disabled" or user.get(email_preference) is False:
            return
        receiver = str(user.get("notification_email") or user.get("github_email") or "").strip()
        if not EMAIL_PATTERN.fullmatch(receiver):
            return
        await send_artifact_status_email(
            self.settings,
            receiver=receiver,
            subject=f"{self.settings.site_name} - {title}",
            content=body,
        )

    def _workbench_link(self, artifact_id: str) -> str:
        query = urlencode({"artifact": artifact_id})
        return f"{self.settings.web_url.rstrip('/')}/plugin-workbench?{query}"

    async def _call_store(self, method_name: str, *args: Any) -> Any:
        method = getattr(self.store, method_name)
        result = method(*args)
        return await result if inspect.isawaitable(result) else result
