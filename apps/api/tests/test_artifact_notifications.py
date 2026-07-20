from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from app.artifacts.notifications import ArtifactNotificationDispatcher
from app.artifacts.repository import InMemoryArtifactRepository
from app.config import load_settings


class ArtifactRepositoryStub:
    def __init__(self) -> None:
        self.artifacts = {
            "artifact-1": {
                "id": "artifact-1",
                "plugin_id": "astrbot_plugin_demo",
                "plugin_name": "Demo Plugin",
                "version": "v1.2.3",
            }
        }
        self.policies = {
            "policy-1": {
                "id": "policy-1",
                "version": "policy-2026-07",
            }
        }

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self.artifacts.get(artifact_id)

    async def get_review_policy(self, policy_id: str) -> dict[str, Any] | None:
        return self.policies.get(policy_id)


def test_artifact_email_omits_free_text_reason_but_in_app_notification_keeps_it(
    monkeypatch,
) -> None:
    sent: list[dict[str, str]] = []
    notifications: list[dict[str, Any]] = []
    reason = (
        "请删除 main.py 第 42 行的 shell 命令；token=secret-value；"
        "object_key=quarantine/private/source.zip；diff=+ subprocess.run(...)"
    )

    class Store:
        def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
            if user_id != "owner-1":
                return None
            return {
                "id": "owner-1",
                "notification_email": "owner@example.test",
                "email_notify_plugin_review": True,
            }

        def create_notification_once(
            self,
            user_id: str,
            title: str,
            body: str,
            notification_type: str,
            metadata: dict[str, Any],
            dedupe_key: str,
        ) -> None:
            notifications.append(
                {
                    "user_id": user_id,
                    "title": title,
                    "body": body,
                    "type": notification_type,
                    "metadata": metadata,
                    "dedupe_key": dedupe_key,
                }
            )

    async def fake_send(*_: Any, **kwargs: str) -> None:
        sent.append(kwargs)

    monkeypatch.setattr("app.artifacts.notifications.send_artifact_status_email", fake_send)
    settings = load_settings(
        {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM": "market@example.test",
            "WEB_URL": "https://market.example.test",
        }
    )
    dispatcher = ArtifactNotificationDispatcher(
        repository=ArtifactRepositoryStub(),  # type: ignore[arg-type]
        store=Store(),
        settings=settings,
        worker_id="notification-test",
        lease_seconds=60,
    )

    asyncio.run(
        dispatcher._deliver(
            {
                "id": "event-1",
                "event_type": "artifact_changes_requested",
                "aggregate_id": "artifact-1",
                "recipient_user_id": "owner-1",
                "payload": {
                    "artifact_id": "artifact-1",
                    "reason": reason,
                    "requirements": "private-package @ https://user:password@example.test/pkg",
                    "comment": "reviewer pasted a credential",
                    "log": "Bearer private-runtime-token",
                },
            }
        )
    )

    assert len(notifications) == 1
    assert reason in notifications[0]["body"]
    assert len(sent) == 1
    assert reason not in sent[0]["content"]
    assert "插件：Demo Plugin" in sent[0]["content"]
    assert "版本：v1.2.3" in sent[0]["content"]
    assert "状态：需要修改" in sent[0]["content"]
    assert "原因：版本需要按站内审查意见修改" in sent[0]["content"]
    assert "main.py" not in sent[0]["content"]
    assert "secret-value" not in sent[0]["content"]
    assert "quarantine/private" not in sent[0]["content"]
    assert "private-package" not in sent[0]["content"]
    assert "private-runtime-token" not in sent[0]["content"]
    assert (
        "工作台：https://market.example.test/plugin-workbench?artifact=artifact-1"
        in sent[0]["content"]
    )


def test_malware_alert_notifies_author_and_admin_without_putting_scan_code_in_email(
    monkeypatch,
) -> None:
    sent: list[dict[str, str]] = []
    notifications: list[dict[str, Any]] = []

    class Store:
        users = {
            "owner-1": {
                "id": "owner-1",
                "role": "user",
                "notification_email": "owner@example.test",
                "email_notify_plugin_review": True,
            },
            "admin-1": {
                "id": "admin-1",
                "role": "admin",
                "notification_email": "admin@example.test",
                "email_notify_pending_review": True,
            },
        }

        def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
            return self.users.get(user_id)

        def list_users(self) -> list[dict[str, Any]]:
            return list(self.users.values())

        def create_notification_once(
            self,
            user_id: str,
            title: str,
            body: str,
            notification_type: str,
            metadata: dict[str, Any],
            dedupe_key: str,
        ) -> None:
            notifications.append(
                {
                    "user_id": user_id,
                    "title": title,
                    "body": body,
                    "type": notification_type,
                    "metadata": metadata,
                    "dedupe_key": dedupe_key,
                }
            )

    async def fake_send(*_: Any, **kwargs: str) -> None:
        sent.append(kwargs)

    monkeypatch.setattr("app.artifacts.notifications.send_artifact_status_email", fake_send)
    settings = load_settings(
        {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM": "market@example.test",
            "WEB_URL": "https://market.example.test",
        }
    )
    dispatcher = ArtifactNotificationDispatcher(
        repository=ArtifactRepositoryStub(),  # type: ignore[arg-type]
        store=Store(),
        settings=settings,
        worker_id="notification-test",
        lease_seconds=60,
    )

    asyncio.run(
        dispatcher._deliver(
            {
                "id": "event-malware-1",
                "event_type": "artifact_malware_critical",
                "aggregate_id": "artifact-1",
                "recipient_user_id": "owner-1",
                "payload": {
                    "artifact_id": "artifact-1",
                    "code": "malware_infected",
                    "critical": True,
                },
            }
        )
    )

    assert {item["user_id"] for item in notifications} == {"owner-1", "admin-1"}
    assert all("malware_infected" in item["body"] for item in notifications)
    assert len(sent) == 2
    assert all("malware_infected" not in message["content"] for message in sent)
    assert all("main.py" not in message["content"] for message in sent)


def test_policy_lifecycle_alert_only_notifies_core_admins(monkeypatch) -> None:
    notifications: list[dict[str, Any]] = []

    class Store:
        users = [
            {"id": "user-1", "role": "user"},
            {"id": "admin-1", "role": "admin"},
            {"id": "core-1", "role": "core_admin"},
        ]

        def list_users(self) -> list[dict[str, Any]]:
            return self.users

        def create_notification_once(
            self,
            user_id: str,
            title: str,
            body: str,
            notification_type: str,
            metadata: dict[str, Any],
            dedupe_key: str,
        ) -> None:
            notifications.append(
                {
                    "user_id": user_id,
                    "title": title,
                    "body": body,
                    "type": notification_type,
                    "metadata": metadata,
                    "dedupe_key": dedupe_key,
                }
            )

    settings = load_settings(
        {"EMAIL_PROVIDER": "disabled", "WEB_URL": "https://market.example.test"}
    )
    dispatcher = ArtifactNotificationDispatcher(
        repository=ArtifactRepositoryStub(),  # type: ignore[arg-type]
        store=Store(),
        settings=settings,
        worker_id="notification-test",
        lease_seconds=60,
    )

    asyncio.run(
        dispatcher._deliver(
            {
                "id": "event-policy-1",
                "event_type": "review_policy_activated",
                "aggregate_type": "review_policy",
                "aggregate_id": "policy-1",
                "recipient_user_id": None,
                "payload": {
                    "policy_id": "policy-1",
                    "version": "policy-2026-07",
                    "reason": "contains config:secret-ref and /private/rules.yar",
                },
            }
        )
    )

    assert [item["user_id"] for item in notifications] == ["core-1"]
    assert notifications[0]["metadata"] == {
        "policy_id": "policy-1",
        "event_type": "review_policy_activated",
        "outbox_event_id": "event-policy-1:admin:core-1",
    }
    assert "contains config:secret-ref" in notifications[0]["body"]


def test_review_risk_alert_recipient_matrix() -> None:
    class Store:
        users = {
            "owner-1": {"id": "owner-1", "role": "user"},
            "admin-1": {"id": "admin-1", "role": "admin"},
            "core-1": {"id": "core-1", "role": "core_admin"},
        }

        def __init__(self) -> None:
            self.notified: list[str] = []

        def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
            return self.users.get(user_id)

        def list_users(self) -> list[dict[str, Any]]:
            return list(self.users.values())

        def create_notification_once(
            self,
            user_id: str,
            *_: Any,
        ) -> None:
            self.notified.append(user_id)

    settings = load_settings(
        {"EMAIL_PROVIDER": "disabled", "WEB_URL": "https://market.example.test"}
    )

    async def recipients(event_type: str, *, author: bool, critical: bool = False) -> set[str]:
        store = Store()
        dispatcher = ArtifactNotificationDispatcher(
            repository=ArtifactRepositoryStub(),  # type: ignore[arg-type]
            store=store,
            settings=settings,
            worker_id="notification-test",
            lease_seconds=60,
        )
        await dispatcher._deliver(
            {
                "id": f"event-{event_type}-{critical}",
                "event_type": event_type,
                "aggregate_type": "artifact",
                "aggregate_id": "artifact-1",
                "recipient_user_id": "owner-1" if author else None,
                "payload": {"artifact_id": "artifact-1", "critical": critical},
            }
        )
        return set(store.notified)

    assert asyncio.run(recipients("artifact_runtime_failed", author=True)) == {
        "owner-1",
        "admin-1",
        "core-1",
    }
    assert asyncio.run(recipients("artifact_dependency_failed", author=True)) == {
        "owner-1",
        "admin-1",
        "core-1",
    }
    assert asyncio.run(recipients("artifact_review_tool_degraded", author=False)) == {
        "admin-1",
        "core-1",
    }
    assert asyncio.run(recipients("artifact_rejected", author=True, critical=True)) == {
        "owner-1",
        "admin-1",
        "core-1",
    }
    assert asyncio.run(recipients("artifact_rejected", author=True)) == {"owner-1"}
    assert asyncio.run(recipients("artifact_submitted", author=True, critical=True)) == {"owner-1"}


def test_email_retry_is_at_least_once_without_duplicate_station_notification(
    monkeypatch,
) -> None:
    repository = InMemoryArtifactRepository()
    sent_attempts = 0
    notifications: dict[str, dict[str, Any]] = {}

    class Store:
        def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
            if user_id != "owner-1":
                return None
            return {
                "id": "owner-1",
                "notification_email": "owner@example.test",
                "email_notify_plugin_review": True,
            }

        def create_notification_once(
            self,
            user_id: str,
            title: str,
            body: str,
            notification_type: str,
            metadata: dict[str, Any],
            dedupe_key: str,
        ) -> None:
            notifications.setdefault(
                dedupe_key,
                {
                    "user_id": user_id,
                    "title": title,
                    "body": body,
                    "type": notification_type,
                    "metadata": metadata,
                },
            )

    async def flaky_send(*_: Any, **__: str) -> None:
        nonlocal sent_attempts
        sent_attempts += 1
        if sent_attempts == 1:
            raise RuntimeError("smtp temporarily unavailable")

    monkeypatch.setattr("app.artifacts.notifications.send_artifact_status_email", flaky_send)
    settings = load_settings(
        {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM": "market@example.test",
            "WEB_URL": "https://market.example.test",
        }
    )
    dispatcher = ArtifactNotificationDispatcher(
        repository=repository,
        store=Store(),
        settings=settings,
        worker_id="notification-test",
        lease_seconds=60,
    )

    async def scenario() -> str:
        artifact = await repository.create_artifact(
            {
                "id": "artifact-1",
                "plugin_id": "astrbot_plugin_demo",
                "version": "v1.2.3",
                "normalized_version": "1.2.3",
                "source_type": "upload",
                "source_repo": "https://github.com/alice/astrbot_plugin_demo",
                "archive_sha256": "a" * 64,
                "size_bytes": 128,
                "quarantine_key": "quarantine/artifact-1/source.zip",
                "submitted_by": "owner-1",
            }
        )
        event = await repository.enqueue_outbox(
            {
                "event_type": "artifact_changes_requested",
                "aggregate_type": "artifact",
                "aggregate_id": artifact["id"],
                "recipient_user_id": "owner-1",
                "payload": {"artifact_id": artifact["id"], "reason": "private detail"},
                "dedupe_key": "artifact:artifact-1:changes-requested",
            }
        )
        await dispatcher.run_once()
        assert repository.outbox[event["id"]]["status"] == "failed"
        repository.outbox[event["id"]]["available_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        await dispatcher.run_once()
        return str(event["id"])

    event_id = asyncio.run(scenario())

    assert repository.outbox[event_id]["status"] == "delivered"
    assert repository.outbox[event_id]["last_error"] == (
        "notification_delivery_failed:RuntimeError"
    )
    assert "smtp temporarily unavailable" not in repository.outbox[event_id]["last_error"]
    assert sent_attempts == 2
    assert len(notifications) == 1
