from __future__ import annotations

import asyncio
from typing import Any

from app.artifacts.notifications import ArtifactNotificationDispatcher
from app.config import load_settings


def test_artifact_email_omits_free_text_reason_but_in_app_notification_keeps_it(
    monkeypatch,
) -> None:
    sent: list[dict[str, str]] = []
    notifications: list[dict[str, Any]] = []
    reason = "请删除 main.py 第 42 行的 shell 命令"

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
        repository=None,  # type: ignore[arg-type]
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
                "payload": {"artifact_id": "artifact-1", "reason": reason},
            }
        )
    )

    assert len(notifications) == 1
    assert reason in notifications[0]["body"]
    assert len(sent) == 1
    assert reason not in sent[0]["content"]
    assert (
        "查看详情：https://market.example.test/plugin-workbench?artifact=artifact-1"
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
        repository=None,  # type: ignore[arg-type]
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
