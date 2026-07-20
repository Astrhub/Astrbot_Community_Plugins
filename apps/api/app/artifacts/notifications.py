from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from ..auth import is_admin, is_core_admin
from ..config import Settings
from .mail import send_artifact_status_email
from .repository import ArtifactRepository

LOGGER = logging.getLogger(__name__)
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True, slots=True)
class NotificationCopy:
    title: str
    body: str
    status: str
    email_reason: str


STATUS_COPY: dict[str, NotificationCopy] = {
    "artifact_submitted": NotificationCopy(
        "[自动审查] 插件版本已提交",
        "插件包已进入隔离队列，基础校验完成后可在站内查看结果。",
        "已提交",
        "版本已进入隔离审查队列",
    ),
    "artifact_precheck_failed": NotificationCopy(
        "[自动审查] 插件未通过基础校验",
        "该版本未通过基础校验，不会进入人工审查，也不会提供插件源 CDN 下载链接；用户仍可选择 GitHub 直连。",
        "基础校验未通过",
        "版本未通过基础校验",
    ),
    "artifact_pending_review": NotificationCopy(
        "[自动审查] 插件等待人工复核",
        "基础校验和静态扫描已完成，版本正在等待管理员人工复核。",
        "等待人工复核",
        "版本需要管理员人工复核",
    ),
    "artifact_rejected": NotificationCopy(
        "[审查结果] 插件版本未通过",
        "该版本未通过审查，不会提供插件源 CDN 下载链接；用户仍可选择 GitHub 直连。",
        "审查未通过",
        "版本未通过审查",
    ),
    "artifact_changes_requested": NotificationCopy(
        "[审查结果] 插件版本需要修改",
        "该版本需要修改，不会提供插件源 CDN 下载链接；用户仍可选择 GitHub 直连。请进入站内工作台查看审查意见。",
        "需要修改",
        "版本需要按站内审查意见修改",
    ),
    "artifact_approved": NotificationCopy(
        "[审查结果] 插件版本已批准",
        "该版本已通过人工复核，正在排队发布不可变 CDN 插件包。",
        "已批准",
        "版本已通过审查并进入发布队列",
    ),
    "artifact_published": NotificationCopy(
        "[发布结果] 插件 CDN 包已发布",
        "该版本的 CDN 插件包已发布，插件源将在仓库版本一致时提供下载链接。",
        "已发布",
        "已发布过审的不可变 CDN 插件包",
    ),
    "artifact_publish_failed": NotificationCopy(
        "[发布结果] 插件 CDN 包发布失败",
        "该版本已通过审查，但 CDN 发布失败；当前稳定 CDN 包不会被覆盖。",
        "发布失败",
        "CDN 发布未完成，旧稳定包保持不变",
    ),
    "artifact_processing_failed": NotificationCopy(
        "[自动审查] 插件版本处理失败",
        "自动审查基础设施处理失败，版本未进入人工复核；当前稳定 CDN 包不会被覆盖。",
        "处理失败",
        "自动审查基础设施未完成处理",
    ),
    "artifact_malware_critical": NotificationCopy(
        "[安全审查] 插件版本检测到严重风险",
        "恶意软件扫描检测到严重风险，该候选版本不会发布 CDN 插件包。请进入站内工作台查看结果。",
        "严重风险",
        "恶意软件扫描检测到严重风险",
    ),
    "artifact_malware_degraded": NotificationCopy(
        "[自动审查] 恶意软件扫描未完成",
        "恶意软件扫描基础设施或规则状态异常，本次结果不会显示为安全通过。请进入站内工作台查看结果。",
        "扫描降级",
        "恶意软件扫描未能形成可信结论",
    ),
    "artifact_runtime_failed": NotificationCopy(
        "[自动审查] 插件运行时校验未通过",
        "隔离运行时校验失败或未能形成可信结论，该候选版本不会自动通过。请进入站内工作台查看结果。",
        "运行时校验异常",
        "隔离运行时校验未通过或已降级",
    ),
    "artifact_dependency_failed": NotificationCopy(
        "[自动审查] 插件依赖审查未通过",
        "依赖风险检查失败或未能形成可信结论，该候选版本不会自动通过。请进入站内工作台查看结果。",
        "依赖审查异常",
        "依赖风险检查未通过或已降级",
    ),
    "artifact_review_tool_degraded": NotificationCopy(
        "[自动审查] 审查工具已降级",
        "自动审查工具未能形成可信结论，结果不得视为安全通过。请进入站内工作台处理。",
        "审查工具降级",
        "自动审查工具不可用或输出无效",
    ),
    "artifact_revoked": NotificationCopy(
        "[安全处置] 插件 CDN 包已下架",
        "管理员已撤回当前 CDN 插件包。请进入站内工作台查看原因。",
        "已下架",
        "当前 CDN 插件包已被管理员撤回",
    ),
    "artifact_stable_risk_revoking": NotificationCopy(
        "[安全处置] 插件 CDN 包正在紧急撤回",
        "管理员已确认风险影响当前稳定版本，插件已从插件源隐藏，CDN 对象正在撤回。",
        "紧急撤回中",
        "已确认风险影响当前稳定版本",
    ),
    "artifact_revoke_failed": NotificationCopy(
        "[安全处置] 插件 CDN 包撤回失败",
        "插件仍保持从插件源隐藏，但 CDN 对象撤回失败。请进入站内工作台处理。",
        "撤回失败",
        "插件源已隐藏，但 CDN 对象撤回未完成",
    ),
    "review_policy_activated": NotificationCopy(
        "[审查策略] 新策略已启用",
        "核心管理员已启用新的默认审查策略。",
        "策略已启用",
        "默认审查策略已切换",
    ),
    "review_policy_retired": NotificationCopy(
        "[审查策略] 策略已停用",
        "核心管理员已停用默认审查策略。",
        "策略已停用",
        "默认审查策略已停用",
    ),
    "review_policy_rolled_back": NotificationCopy(
        "[审查策略] 策略已回滚",
        "核心管理员已回滚到先前验证过的审查策略。",
        "策略已回滚",
        "默认审查策略已回滚",
    ),
}

_UNLIST_EVENTS = frozenset(
    {"artifact_stable_risk_revoking", "artifact_revoked", "artifact_revoke_failed"}
)
_EMERGENCY_ADMIN_EVENTS = frozenset(
    {"artifact_stable_risk_revoking", "artifact_revoked", "artifact_revoke_failed"}
)
_MALWARE_ADMIN_EVENTS = frozenset({"artifact_malware_critical", "artifact_malware_degraded"})
_REVIEW_ADMIN_EVENTS = frozenset(
    {
        "artifact_runtime_failed",
        "artifact_dependency_failed",
        "artifact_review_tool_degraded",
    }
)
_POLICY_EVENTS = frozenset(
    {"review_policy_activated", "review_policy_retired", "review_policy_rolled_back"}
)
_CRITICAL_ADMIN_EVENTS = frozenset({"artifact_rejected", "artifact_pending_review"})
_DEFAULT_COPY = NotificationCopy(
    "[插件工作台] 状态更新",
    "插件版本状态已更新。",
    "状态已更新",
    "插件工作台状态已更新",
)


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
                error_type = type(exc).__name__
                LOGGER.warning("Artifact notification %s failed: %s", event["id"], error_type)
                await self.repository.fail_outbox(
                    str(event["id"]),
                    self.worker_id,
                    error_message=f"notification_delivery_failed:{error_type}"[:500],
                    retry=True,
                    retry_delay_seconds=min(300, 2 ** min(int(event.get("attempts") or 1), 8)),
                )
            else:
                await self.repository.complete_outbox(str(event["id"]), self.worker_id)
        return len(events)

    async def _deliver(self, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("event_type") or "")
        copy = STATUS_COPY.get(event_type, _DEFAULT_COPY)
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        subject = await self._notification_subject(event, payload)
        reason = _site_reason(payload)
        email_body = "\n".join(
            (
                f"插件：{subject['name']}",
                f"版本：{subject['version']}",
                f"状态：{copy.status}",
                f"原因：{copy.email_reason}",
                f"工作台：{subject['link']}",
            )
        )
        full_body = f"{copy.body}\n"
        if reason:
            full_body += f"原因：{reason}\n"
        full_body += f"查看详情：{subject['link']}"

        recipient_id = str(event.get("recipient_user_id") or "")
        if recipient_id:
            recipient = await self._call_store("get_user_by_id", recipient_id)
            if recipient:
                await self._notify_user(
                    recipient,
                    event_id=str(event["id"]),
                    event_type=event_type,
                    title=copy.title,
                    body=full_body,
                    email_body=email_body,
                    metadata=subject["metadata"],
                    notification_type=subject["notification_type"],
                    email_preference=(
                        "email_notify_unlist"
                        if event_type in _UNLIST_EVENTS
                        else "email_notify_plugin_review"
                    ),
                )

        notify_admins = (
            event_type == "artifact_pending_review"
            or event_type in _MALWARE_ADMIN_EVENTS
            or event_type in _REVIEW_ADMIN_EVENTS
            or event_type in _POLICY_EVENTS
            or (event_type in _CRITICAL_ADMIN_EVENTS and payload.get("critical") is True)
            or (event_type in _EMERGENCY_ADMIN_EVENTS and payload.get("emergency") is True)
        )
        if notify_admins:
            users = await self._call_store("list_users")
            for admin in users or []:
                allowed_role = (
                    is_core_admin(admin) if event_type in _POLICY_EVENTS else is_admin(admin)
                )
                if not allowed_role or str(admin.get("id") or "") == recipient_id:
                    continue
                admin_title = (
                    "[待审队列] 有新的插件版本待复核"
                    if event_type == "artifact_pending_review"
                    else copy.title
                )
                await self._notify_user(
                    admin,
                    event_id=f"{event['id']}:admin:{admin.get('id')}",
                    event_type=event_type,
                    title=admin_title,
                    body=full_body,
                    email_body=email_body,
                    metadata=subject["metadata"],
                    notification_type=subject["notification_type"],
                    email_preference=(
                        "email_notify_pending_review"
                        if event_type == "artifact_pending_review"
                        or event_type in _MALWARE_ADMIN_EVENTS
                        or event_type in _REVIEW_ADMIN_EVENTS
                        or event_type in _POLICY_EVENTS
                        else "email_notify_unlist"
                    ),
                )

    async def _notify_user(
        self,
        user: Mapping[str, Any],
        *,
        event_id: str,
        event_type: str,
        title: str,
        body: str,
        email_body: str,
        metadata: Mapping[str, str],
        notification_type: str,
        email_preference: str,
    ) -> None:
        station_metadata = {
            **dict(metadata),
            "event_type": event_type,
            "outbox_event_id": event_id,
        }
        await self._call_store(
            "create_notification_once",
            str(user["id"]),
            title,
            body,
            notification_type,
            station_metadata,
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
            content=email_body,
        )

    async def _notification_subject(
        self,
        event: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        event_type = str(event.get("event_type") or "")
        if event_type in _POLICY_EVENTS or event.get("aggregate_type") == "review_policy":
            policy_id = str(payload.get("policy_id") or event.get("aggregate_id") or "")
            policy = await self.repository.get_review_policy(policy_id) if policy_id else None
            version = _mail_field((policy or {}).get("version") or payload.get("version"), "未知")
            return {
                "name": "审查策略",
                "version": version,
                "link": self._policy_link(),
                "metadata": {"policy_id": policy_id},
                "notification_type": "review_policy",
            }

        artifact_id = str(payload.get("artifact_id") or event.get("aggregate_id") or "")
        artifact = await self.repository.get_artifact(artifact_id) if artifact_id else None
        name = _mail_field(
            (artifact or {}).get("plugin_name")
            or (artifact or {}).get("plugin_id")
            or payload.get("plugin_id"),
            "未知插件",
        )
        version = _mail_field((artifact or {}).get("version") or payload.get("version"), "未知")
        return {
            "name": name,
            "version": version,
            "link": self._workbench_link(artifact_id),
            "metadata": {"artifact_id": artifact_id},
            "notification_type": "plugin_artifact",
        }

    def _workbench_link(self, artifact_id: str) -> str:
        query = urlencode({"artifact": artifact_id})
        return f"{self.settings.web_url.rstrip('/')}/plugin-workbench?{query}"

    def _policy_link(self) -> str:
        query = urlencode({"view": "policy"})
        return f"{self.settings.web_url.rstrip('/')}/plugin-workbench?{query}"

    async def _call_store(self, method_name: str, *args: Any) -> Any:
        method = getattr(self.store, method_name)
        result = method(*args)
        return await result if inspect.isawaitable(result) else result


def _mail_field(value: Any, fallback: str) -> str:
    normalized = " ".join(str(value or "").split())[:120]
    return normalized or fallback


def _site_reason(payload: Mapping[str, Any]) -> str:
    value: Any = payload.get("reason") or payload.get("code")
    if not value and isinstance(payload.get("reason_codes"), (list, tuple)):
        value = ", ".join(str(item) for item in payload["reason_codes"][:10])
    return " ".join(str(value or "").split())[:500]
