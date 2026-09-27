from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.artifacts.notifications import ArtifactNotificationDispatcher, _email_evaluation
from app.config import load_settings
from test_artifact_notifications import ArtifactRepositoryStub


@pytest.mark.parametrize("event_type", ["artifact_approved", "artifact_published"])
def test_approval_email_includes_saved_evaluation_and_current_site_mail_settings(
    monkeypatch, event_type
):
    class Repository(ArtifactRepositoryStub):
        async def list_review_decisions(self, artifact_id):
            assert artifact_id == "artifact-1"
            return [
                {
                    "id": "decision-1",
                    "action": "manual_approve",
                    "source": "admin",
                    "to_status": "approved",
                    "reason": "通过。建议将 board.png 改成每次响应独立文件，不影响本次通过。",
                },
                {
                    "id": "later-comment",
                    "action": "comment",
                    "source": "admin",
                    "to_status": "approved",
                    "reason": "not an approval evaluation",
                },
            ]

    class Store:
        options = {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM": "market@example.test",
            "WEB_URL": "https://plugins.example.test",
            "DATABASE_URL": "must-not-override-worker",
        }
        enabled = True
        notifications = []

        async def list_options(self):
            return dict(self.options)

        def get_user_by_id(self, _):
            return {
                "id": "owner-1",
                "notification_email": "owner@example.test",
                "email_notify_plugin_review": self.enabled,
            }

        def create_notification_once(self, *args):
            self.notifications.append(args)

    sent = []

    async def fake_send(settings, **kwargs: Any):
        assert settings.email_provider == "smtp"
        assert settings.smtp_host == "smtp.example.test"
        assert settings.database_url == "postgresql://worker/base"
        sent.append(kwargs)

    monkeypatch.setattr("app.artifacts.notifications.send_artifact_status_email", fake_send)
    base = load_settings(
        {
            "APP_ENV_FILE": "/dev/null",
            "EMAIL_PROVIDER": "disabled",
            "DATABASE_URL": "postgresql://worker/base",
        }
    )
    store = Store()
    dispatcher = ArtifactNotificationDispatcher(
        repository=Repository(), store=store, settings=base, worker_id="test", lease_seconds=60
    )
    event = {
        "id": "event-1",
        "event_type": event_type,
        "aggregate_id": "artifact-1",
        "recipient_user_id": "owner-1",
        "payload": {
            "decision_id": "decision-1",
            "reason": "token=never-send",
            "comment": "not-for-mail",
        },
    }

    async def scenario():
        await dispatcher._deliver(event)
        event["payload"]["suppress_email"] = True
        await dispatcher._deliver(event)
        assert store.notifications[-1][4]["email_delivery"] == "suppressed_by_admin_request"
        event["payload"].pop("suppress_email")
        store.enabled = False
        await dispatcher._deliver(event)
        store.enabled = True
        store.options["EMAIL_PROVIDER"] = "disabled"
        await dispatcher._deliver(event)

    asyncio.run(scenario())
    assert len(sent) == 1
    content = sent[0]["content"]
    assert "审查评价（不影响本次通过）" in content and "每次响应独立文件" in content
    assert "never-send" not in content and "not an approval" not in content
    assert "https://plugins.example.test/plugin-workbench?artifact=artifact-1" in content
    assert base.email_provider == "disabled"


@pytest.mark.parametrize(
    "reason",
    [
        'token="secret-value"',
        "object_key=quarantine/private/source.zip",
        "内部路径 /etc/market/private.env",
        "password: secret",
        "'api_key': 'hidden'",
    ],
)
def test_sensitive_evaluation_is_referred_to_private_workbench(reason):
    assert _email_evaluation(reason) == "评价含敏感内容，请进入工作台查看完整意见。"


def test_evaluation_limits_and_code_redaction():
    text = _email_evaluation(
        "建议使用独立文件。\n```python\nprivate_code()\n```\n" + "详细建议" * 1000
    )
    assert len(text) <= 1000 and "private_code" not in text
    assert "建议使用独立文件" in text
