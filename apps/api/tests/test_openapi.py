"""Tests for OpenAPI schema filtering, llms.txt, and native docs disabled."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import create_app
from app.store import InMemoryMarketStore


def _client_with_store() -> tuple[TestClient, InMemoryMarketStore]:
    store = InMemoryMarketStore()
    store.create_internal_admin("coreadmin", hash_password("testpass123"))
    store.create_internal_user("regularuser", hash_password("userpass123"), "user")
    store.create_internal_user("adminuser", hash_password("adminpass123"), "admin")
    app = create_app(store=store)
    return TestClient(app, follow_redirects=False), store


def _login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post(
        "/v1/auth/internal/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_openapi_public_excludes_admin_and_core():
    client, _ = _client_with_store()
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    paths = schema.get("paths", {})
    # admin/core routes must NOT appear
    assert "/v1/admin/users" not in paths
    assert "/v1/core/users" not in paths
    assert "/v1/admin/settings" not in paths
    assert "/v1/artifacts/{artifact_id}/files" not in paths
    assert "/v1/artifacts/{artifact_id}/files/{file_id}/content" not in paths
    # public routes MUST appear
    assert "/v1/plugins" in paths
    assert "/v1/site" in paths
    assert "/v1/announcements" in paths


def test_openapi_logged_in_includes_user():
    client, _ = _client_with_store()
    _login(client, "regularuser", "userpass123")
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json().get("paths", {})
    # user routes appear
    assert "/v1/me" in paths
    assert "/v1/me/plugins" in paths
    # admin still hidden
    assert "/v1/admin/users" not in paths
    assert "/v1/core/users" not in paths


def test_openapi_admin_includes_admin_routes():
    client, _ = _client_with_store()
    _login(client, "adminuser", "adminpass123")
    resp = client.get("/openapi.json")
    paths = resp.json().get("paths", {})
    assert "/v1/admin/users" in paths
    assert "/v1/admin/plugins" in paths
    # core-admin still hidden
    assert "/v1/core/users" not in paths


def test_artifact_report_openapi_is_typed_and_role_filtered():
    client, _ = _client_with_store()
    _login(client, "regularuser", "userpass123")
    user_schema = client.get("/openapi.json").json()
    assert "/v1/artifacts/{artifact_id}" in user_schema["paths"]
    assert "/v1/artifacts/{artifact_id}/files" in user_schema["paths"]
    assert "/v1/artifacts/{artifact_id}/files/{file_id}/content" in user_schema["paths"]
    assert "/v1/artifacts/{artifact_id}/diff" in user_schema["paths"]
    assert "/v1/artifacts/{artifact_id}/diff/{diff_id}" in user_schema["paths"]
    assert "/v1/artifacts/{artifact_id}/comments" in user_schema["paths"]
    assert "/v1/artifacts/{artifact_id}/comments/{thread_id}/replies" in user_schema["paths"]
    assert (
        "/v1/artifacts/{artifact_id}/comments/{thread_id}/author-addressed" in user_schema["paths"]
    )
    assert "/v1/admin/artifacts/{artifact_id}/comments" not in user_schema["paths"]
    assert "/v1/admin/artifacts/{artifact_id}/request-changes" not in user_schema["paths"]
    detail_response = user_schema["paths"]["/v1/artifacts/{artifact_id}"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert detail_response["$ref"].endswith("/ArtifactDetailResponse")

    _login(client, "adminuser", "adminpass123")
    admin_schema = client.get("/openapi.json").json()
    request_changes = admin_schema["paths"]["/v1/admin/artifacts/{artifact_id}/request-changes"][
        "post"
    ]
    response_schema = request_changes["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["$ref"].endswith("/ArtifactEnvelope")
    assert "/v1/admin/artifacts/{artifact_id}/comments" in admin_schema["paths"]
    for action in ("edit", "resolve", "reopen"):
        assert (
            f"/v1/admin/artifacts/{{artifact_id}}/comments/{{thread_id}}/{action}"
            in admin_schema["paths"]
        )

    schemas = admin_schema["components"]["schemas"]
    run_fields = set(schemas["PublicReviewRun"]["properties"])
    finding_fields = set(schemas["PublicReviewFinding"]["properties"])
    decision_fields = set(schemas["PublicReviewDecision"]["properties"])
    assert {"coverage", "tool_name", "tool_version", "model", "advisory", "label"} <= run_fields
    assert {"source", "deterministic", "advisory", "label"} <= finding_fields
    assert {"policy_version_id", "input_run_ids", "coverage_sha256"} <= decision_fields
    assert {"raw_result", "raw_result_key", "worker_id"}.isdisjoint(run_fields)
    assert "metadata" not in finding_fields
    assert "idempotency_key" not in decision_fields

    file_fields = set(schemas["PublicArtifactFile"]["properties"])
    diff_fields = set(schemas["PublicArtifactDiff"]["properties"])
    diff_stats_fields = set(schemas["PublicArtifactDiffStats"]["properties"])
    assert {"id", "path", "sha256", "is_text", "graph_status"} <= file_fields
    assert {"id", "path", "change_type", "has_hunks", "stats"} <= diff_fields
    assert {"content_key", "hunks_key", "quarantine_key"}.isdisjoint(file_fields)
    assert {"content_key", "hunks_key", "quarantine_key"}.isdisjoint(diff_fields)
    assert {"hunks_sha256", "hunks_size_bytes", "hunks_key"}.isdisjoint(diff_stats_fields)

    comment_fields = set(schemas["PublicReviewComment"]["properties"])
    comment_event_fields = set(schemas["PublicReviewCommentEvent"]["properties"])
    assert {
        "file_id",
        "file_path",
        "file_sha256",
        "side",
        "line_start",
        "line_end",
        "events",
        "version",
    } <= comment_fields
    assert {"actor_nickname", "actor_role", "expected_version"} <= comment_event_fields
    assert {"reviewer_user_id", "idempotency_key", "content_key"}.isdisjoint(comment_fields)
    assert {"actor_user_id", "idempotency_key", "metadata"}.isdisjoint(comment_event_fields)
    github_fields = set(schemas["GithubArtifactSubmission"]["properties"])
    assert "supersedes_artifact_id" in github_fields


def test_openapi_core_admin_sees_all():
    client, _ = _client_with_store()
    _login(client, "coreadmin", "testpass123")
    resp = client.get("/openapi.json")
    paths = resp.json().get("paths", {})
    assert "/v1/admin/settings" in paths
    assert "/v1/core/users" in paths
    assert "/v1/setup" in paths


def test_openapi_tags_present_on_operations():
    client, _ = _client_with_store()
    resp = client.get("/openapi.json")
    schema = resp.json()
    for _path, item in schema.get("paths", {}).items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete") and isinstance(op, dict):
                assert op.get("tags"), f"{method.upper()} {_path} has no tags"


def test_openapi_summary_present_on_operations():
    client, _ = _client_with_store()
    resp = client.get("/openapi.json")
    schema = resp.json()
    for _path, item in schema.get("paths", {}).items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete") and isinstance(op, dict):
                assert op.get("summary"), f"{method.upper()} {_path} has no summary"


def test_docs_disabled_returns_404():
    client, _ = _client_with_store()
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_llms_txt_public_excludes_personal():
    client, _ = _client_with_store()
    resp = client.get("/llms.txt")
    assert resp.status_code == 200
    content = resp.text
    assert "GET /v1/plugins" in content
    assert "/v1/me/plugins" not in content
    assert "/v1/admin/" not in content


def test_llms_txt_logged_in_includes_personal():
    client, _ = _client_with_store()
    _login(client, "regularuser", "userpass123")
    resp = client.get("/llms.txt")
    content = resp.text
    assert "GET /v1/me/plugins" in content
    assert "/v1/admin/" not in content


def test_llms_txt_admin_includes_admin_section():
    client, _ = _client_with_store()
    _login(client, "adminuser", "adminpass123")
    resp = client.get("/llms.txt")
    content = resp.text
    assert "GET /v1/admin/plugins" in content
    assert "/v1/core/" not in content
