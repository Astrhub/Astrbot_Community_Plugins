from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.artifacts.routes import _policy_envelope
from app.auth import hash_password
from app.config import load_settings
from app.main import create_app
from app.store import InMemoryMarketStore


def _policy_payload(*, runtime: bool = False) -> dict:
    return {
        "schema_version": "1",
        "required_stages": ["static", *(["runtime"] if runtime else [])],
        "runtime_targets": ([{"astrbot": "4.26.5", "python": "3.12"}] if runtime else []),
        "limits": {
            "cpu": 1,
            "memory_mb": 768,
            "pids": 128,
            "timeout_seconds": 120,
        },
        "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
        "llm": {"enabled": False},
        "malware": {"clamav": False},
        "dependency": {"enabled": False},
        "routing": {"auto_approve": False, "manual_review_at": "low"},
    }


def _client() -> TestClient:
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "ARTIFACT_LOCAL_ROOT": "/tmp/review-policy-routes",
            "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
            "ARTIFACT_RUNTIME_REVIEW_ENABLED": "true",
            "ARTIFACT_RUNTIME_CONTAINER_IMAGE": f"astrbot-runtime@sha256:{'1' * 64}",
            "DATABASE_URL": "postgresql://example.invalid/market",
            "REDIS_URL": "redis://example.invalid/0",
            "GITHUB_METADATA_SYNC_ENABLED": "false",
        }
    )
    store = InMemoryMarketStore()
    store.create_internal_admin("coreadmin", hash_password("core-pass-123"))
    store.create_internal_user("reviewer", hash_password("admin-pass-123"), "admin")
    store.create_internal_user("author", hash_password("user-pass-123"), "user")
    return TestClient(create_app(settings=settings, store=store), follow_redirects=False)


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/v1/auth/internal/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text


def _create_static_policy(client: TestClient) -> dict:
    response = client.post(
        "/v1/core-admin/review-policies",
        json={
            "version": "policy-static-v1",
            "policy": _policy_payload(),
            "reason": "Initial static policy",
            "idempotency_key": "policy-static-create",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["policy"]


def test_policy_routes_enforce_roles_idempotency_and_active_snapshot() -> None:
    with _client() as client:
        assert client.get("/v1/admin/review-policies/active").status_code == 401
        _login(client, "author", "user-pass-123")
        assert client.get("/v1/admin/review-policies/active").status_code == 403
        assert client.get("/v1/core-admin/review-policies").status_code == 403

        _login(client, "reviewer", "admin-pass-123")
        empty = client.get("/v1/admin/review-policies/active")
        assert empty.status_code == 200
        assert empty.json() == {"policy": None}
        assert empty.headers["cache-control"] == "no-store, private"
        assert (
            client.post(
                "/v1/core-admin/review-policies",
                json={
                    "version": "admin-denied-v1",
                    "policy": _policy_payload(),
                    "idempotency_key": "admin-denied-create",
                },
            ).status_code
            == 403
        )

        _login(client, "coreadmin", "core-pass-123")
        draft = _create_static_policy(client)
        repeated = client.post(
            "/v1/core-admin/review-policies",
            json={
                "version": "policy-static-v1",
                "policy": _policy_payload(),
                "reason": "Initial static policy",
                "idempotency_key": "policy-static-create",
            },
        )
        assert repeated.status_code == 201
        assert repeated.json()["policy"]["id"] == draft["id"]

        conflict = client.post(
            "/v1/core-admin/review-policies",
            json={
                "version": "policy-static-v1",
                "policy": {**_policy_payload(), "network_profiles": {"install": "mirror-v2"}},
                "idempotency_key": "policy-static-conflict",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "review_policy_version_conflict"

        validated = client.post(
            f"/v1/core-admin/review-policies/{draft['id']}/validate",
            json={"idempotency_key": "policy-static-validate"},
        )
        assert validated.status_code == 200
        assert validated.json()["policy"]["validation_summary"] == {
            "valid": True,
            "schema_version": "1",
            "policy_sha256": draft["policy_sha256"],
            "readiness_checked": True,
            "issues": [],
        }
        assert validated.json()["diff"]["redacted"] is True

        missing_key = client.post(
            f"/v1/core-admin/review-policies/{draft['id']}/activate",
            json={"reason": "Activate policy"},
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["code"] == "idempotency_key_required"
        activated = client.post(
            f"/v1/core-admin/review-policies/{draft['id']}/activate",
            json={
                "reason": "Activate static policy",
                "idempotency_key": "policy-static-activate",
            },
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["policy"]["status"] == "active"
        assert "created_by_user_id" not in activated.json()["policy"]

        listed = client.get("/v1/core-admin/review-policies?limit=10&offset=0")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [draft["id"]]

        _login(client, "reviewer", "admin-pass-123")
        active = client.get("/v1/admin/review-policies/active")
        assert active.status_code == 200
        assert active.json()["policy"]["id"] == draft["id"]
        assert active.json()["policy"]["status"] == "active"
        assert client.get("/v1/core-admin/review-policies").status_code == 403


def test_policy_activation_fails_visible_without_required_runtime_runner() -> None:
    with _client() as client:
        _login(client, "coreadmin", "core-pass-123")
        static = _create_static_policy(client)
        active = client.post(
            f"/v1/core-admin/review-policies/{static['id']}/activate",
            json={"reason": "Activate base", "idempotency_key": "activate-base"},
        )
        assert active.status_code == 200
        runtime_draft = client.post(
            "/v1/core-admin/review-policies",
            json={
                "version": "policy-runtime-v1",
                "policy": _policy_payload(runtime=True),
                "idempotency_key": "policy-runtime-create",
            },
        ).json()["policy"]

        validation = client.post(
            f"/v1/core-admin/review-policies/{runtime_draft['id']}/validate",
            json={"idempotency_key": "policy-runtime-validate"},
        )
        assert validation.status_code == 200
        summary = validation.json()["policy"]["validation_summary"]
        assert summary["valid"] is False
        assert {item["path"] for item in summary["issues"]} == {"tools.runtime"}

        rejected = client.post(
            f"/v1/core-admin/review-policies/{runtime_draft['id']}/activate",
            json={
                "reason": "Must not replace active policy",
                "idempotency_key": "policy-runtime-activate",
            },
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "review_policy_invalid"
        current = client.get("/v1/admin/review-policies/active").json()["policy"]
        assert current["id"] == static["id"]


def test_core_health_and_metrics_are_typed_bounded_and_redacted() -> None:
    with _client() as client:
        _login(client, "coreadmin", "core-pass-123")
        draft = _create_static_policy(client)
        client.post(
            f"/v1/core-admin/review-policies/{draft['id']}/activate",
            json={"reason": "Health fixture", "idempotency_key": "health-activate"},
        )
        response = client.get("/v1/core-admin/review-tools/health")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store, private"
    payload = response.json()
    assert payload["health"]["review"]["components"]["policy"]["ready"] is True
    assert {item["kind"] for item in payload["health"]["workers"]} == {
        "artifact_worker",
        "runtime_runner",
    }
    assert {item["name"] for item in payload["health"]["tools"]} == {
        "policy",
        "runtime",
        "llm",
        "clamav",
        "yara",
        "dependency",
    }
    assert payload["metrics"]["available"] is True
    disabled_tools = [item for item in payload["health"]["tools"] if not item["enabled"]]
    assert disabled_tools
    assert {item["freshness"] for item in disabled_tools} == {"not_applicable"}
    assert set(payload["metrics"]) == {
        "available",
        "window_started_at",
        "collected_at",
        "queue",
        "stages",
        "manual_wait",
        "routing",
        "revoke",
    }
    rendered = str(payload).lower()
    for forbidden in ("endpoint", "token", "bucket", "object_key", "worker_id", "/tmp/"):
        assert forbidden not in rendered


def test_policy_envelope_diff_lookup_is_best_effort_after_mutation() -> None:
    class FailingRepository:
        async def get_review_policy(self, policy_id: str) -> dict:
            raise RuntimeError(f"unavailable:{policy_id}")

    class Service:
        repository = FailingRepository()

    timestamp = "2026-07-17T00:00:00Z"
    policy = {
        "id": "policy-draft",
        "version": "policy-v2",
        "schema_version": "1",
        "status": "draft",
        "policy": _policy_payload(),
        "policy_sha256": "a" * 64,
        "base_policy_id": "policy-active",
        "created_by_nickname": "coreadmin",
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    result = asyncio.run(_policy_envelope(Service(), policy))

    assert result["policy"]["id"] == "policy-draft"
    assert result["diff"]["before_sha256"] == ""
    assert result["diff"]["path_count"] > 0
