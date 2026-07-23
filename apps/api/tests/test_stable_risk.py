from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.artifacts.content import ArtifactContentService
from app.artifacts.diff import manifest_tree_sha256
from app.artifacts.findings import StableRiskError, StableRiskService
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.storage import LocalArtifactStorage, build_content_key
from app.config import load_settings
from app.main import create_app
from app.store import InMemoryMarketStore


def test_llm_only_and_same_path_different_sha_do_not_revoke_stable(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], str, str, int]:
        fixture = await _stable_fixture(
            tmp_path, stable_main=b"SAFE = 1\n", candidate_main=b"SAFE = 2\n"
        )
        errors: list[str] = []
        llm = await _add_finding(
            fixture,
            artifact=fixture.candidate,
            fingerprint="llm-risk",
            source="llm",
            deterministic=False,
            file_path="main.py",
            file_sha256=fixture.candidate_files["main.py"]["sha256"],
        )
        different_sha = await _add_finding(
            fixture,
            artifact=fixture.candidate,
            fingerprint="static-risk",
            source="static",
            deterministic=True,
            file_path="main.py",
            file_sha256=fixture.candidate_files["main.py"]["sha256"],
        )
        for finding, marker in ((llm, "llm"), (different_sha, "sha")):
            with pytest.raises(StableRiskError) as caught:
                await fixture.service.request_revoke(
                    candidate_artifact_id=fixture.candidate["id"],
                    finding_id=finding["id"],
                    actor=_admin(),
                    expected_version=1,
                    reason=f"Review {marker}",
                    confirm_affects_current_release=False,
                    idempotency_key=f"stable-risk-{marker}",
                )
            errors.append(caught.value.code)
        stable = await fixture.repository.get_artifact(fixture.stable["id"])
        plugin = fixture.store.get_plugin(fixture.plugin["id"])
        return (
            errors,
            str(stable["publication_status"] if stable else ""),
            str((plugin or {}).get("current_artifact_id") or ""),
            len([job for job in fixture.repository.jobs.values() if job["type"] == "revoke"]),
        )

    errors, publication, current_id, revoke_jobs = asyncio.run(scenario())
    assert errors == ["stable_release_correlation_required"] * 2
    assert publication == "published"
    assert current_id
    assert revoke_jobs == 0


def test_path_sha_correlation_unlists_before_revoke_and_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict[str, Any], dict[str, Any], int, int, str, str]:
        fixture = await _stable_fixture(
            tmp_path, stable_main=b"SHARED = 1\n", candidate_main=b"SHARED = 1\n"
        )
        finding = await _add_finding(
            fixture,
            artifact=fixture.candidate,
            fingerprint="shared-static-risk",
            source="static",
            deterministic=True,
            file_path="main.py",
            file_sha256=fixture.candidate_files["main.py"]["sha256"],
        )
        request = {
            "candidate_artifact_id": fixture.candidate["id"],
            "finding_id": finding["id"],
            "actor": _admin(),
            "expected_version": 1,
            "reason": "Confirmed shared vulnerable file",
            "confirm_affects_current_release": False,
            "idempotency_key": "stable-risk-path-sha",
        }
        first = await fixture.service.request_revoke(**request)
        repeated = await fixture.service.request_revoke(**request)
        with pytest.raises(StableRiskError) as conflict:
            await fixture.service.request_revoke(
                **{**request, "reason": "Changed reason under the same key"}
            )
        stable = await fixture.repository.get_artifact(fixture.stable["id"])
        plugin = fixture.store.get_plugin(fixture.plugin["id"])
        assert stable is not None and plugin is not None
        return (
            first,
            repeated,
            len(
                [
                    item
                    for item in fixture.repository.decisions.values()
                    if item["action"] == "revoke"
                ]
            ),
            len([item for item in fixture.repository.jobs.values() if item["type"] == "revoke"]),
            f"{stable['publication_status']}:{plugin['status']}:{stable['published_key']}",
            conflict.value.code,
        )

    first, repeated, decisions, jobs, state, conflict_code = asyncio.run(scenario())
    assert first["correlation"]["kind"] == "path_sha"
    assert repeated["stable_artifact"]["id"] == first["stable_artifact"]["id"]
    assert decisions == 1
    assert jobs == 1
    assert state.startswith("revoking:unlisted:")
    assert not state.endswith(":None")
    assert conflict_code == "idempotency_key_conflict"


@pytest.mark.parametrize("mode", ["fingerprint", "dependency", "admin_confirmation"])
def test_stable_risk_accepts_only_exact_deterministic_or_admin_evidence(
    tmp_path: Path, mode: str
) -> None:
    async def scenario() -> tuple[str, bool]:
        fixture = await _stable_fixture(
            tmp_path, stable_main=b"OLD = 1\n", candidate_main=b"NEW = 1\n"
        )
        if mode == "fingerprint":
            stable_finding = await _add_finding(
                fixture,
                artifact=fixture.stable,
                fingerprint="same-rule",
                source="static",
                deterministic=True,
                tool_name="semgrep",
                tool_version="1.2.3",
                ruleset_version="rules-v1",
            )
            finding = await _add_finding(
                fixture,
                artifact=fixture.candidate,
                fingerprint=stable_finding["fingerprint"],
                source="static",
                deterministic=True,
                tool_name="semgrep",
                tool_version="1.2.3",
                ruleset_version="rules-v1",
            )
            confirm = False
        elif mode == "dependency":
            dependency = {
                "dependency": {
                    "name": "requests",
                    "version": "2.31.0",
                    "advisory_id": "GHSA-test-1234",
                }
            }
            await _add_finding(
                fixture,
                artifact=fixture.stable,
                fingerprint="stable-dependency",
                source="dependency",
                deterministic=True,
                correlation=dependency,
            )
            finding = await _add_finding(
                fixture,
                artifact=fixture.candidate,
                fingerprint="candidate-dependency",
                source="dependency",
                deterministic=True,
                correlation=dependency,
            )
            confirm = False
        else:
            finding = await _add_finding(
                fixture,
                artifact=fixture.candidate,
                fingerprint="llm-admin-confirm",
                source="llm",
                deterministic=False,
            )
            confirm = True
        result = await fixture.service.request_revoke(
            candidate_artifact_id=fixture.candidate["id"],
            finding_id=finding["id"],
            actor=_admin(),
            expected_version=1,
            reason="Administrator confirmed impact",
            confirm_affects_current_release=confirm,
            idempotency_key=f"stable-risk-{mode}",
        )
        updated = next(
            item
            for item in await fixture.repository.list_findings(fixture.candidate["id"])
            if item["id"] == finding["id"]
        )
        return result["correlation"]["kind"], bool(updated["affects_current_release"])

    kind, affects = asyncio.run(scenario())
    assert kind == mode
    assert affects is True


def test_admin_confirmation_requires_reason_and_fingerprint_versions_must_match(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[str]:
        fixture = await _stable_fixture(
            tmp_path, stable_main=b"OLD = 1\n", candidate_main=b"NEW = 1\n"
        )
        await _add_finding(
            fixture,
            artifact=fixture.stable,
            fingerprint="versioned-rule",
            source="static",
            deterministic=True,
            tool_name="scanner",
            tool_version="1",
            ruleset_version="rules-1",
        )
        mismatch = await _add_finding(
            fixture,
            artifact=fixture.candidate,
            fingerprint="versioned-rule",
            source="static",
            deterministic=True,
            tool_name="scanner",
            tool_version="2",
            ruleset_version="rules-1",
        )
        llm = await _add_finding(
            fixture,
            artifact=fixture.candidate,
            fingerprint="admin-no-reason",
            source="llm",
            deterministic=False,
        )
        errors: list[str] = []
        for finding, confirm, reason, marker in (
            (mismatch, False, "Mismatch", "mismatch"),
            (llm, True, "", "reason"),
        ):
            with pytest.raises(StableRiskError) as caught:
                await fixture.service.request_revoke(
                    candidate_artifact_id=fixture.candidate["id"],
                    finding_id=finding["id"],
                    actor=_admin(),
                    expected_version=1,
                    reason=reason,
                    confirm_affects_current_release=confirm,
                    idempotency_key=f"stable-risk-{marker}",
                )
            errors.append(caught.value.code)
        return errors

    assert asyncio.run(scenario()) == [
        "stable_release_correlation_required",
        "reason_required",
    ]


def test_stable_risk_side_effects_roll_back_on_notification_conflict(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str, str, int, bool, int, int]:
        fixture = await _stable_fixture(
            tmp_path, stable_main=b"SHARED = 1\n", candidate_main=b"SHARED = 1\n"
        )
        finding = await _add_finding(
            fixture,
            artifact=fixture.candidate,
            fingerprint="atomic-shared-risk",
            source="static",
            deterministic=True,
            file_path="main.py",
            file_sha256=fixture.candidate_files["main.py"]["sha256"],
        )
        request_key = "atomic-conflict"
        await fixture.repository.enqueue_outbox(
            {
                "event_type": "artifact_rejected",
                "aggregate_type": "artifact",
                "aggregate_id": fixture.candidate["id"],
                "recipient_user_id": None,
                "payload": {"artifact_id": fixture.candidate["id"]},
                "dedupe_key": (
                    f"artifact:{fixture.stable['id']}:stable-risk-revoking:{request_key}"
                ),
            }
        )
        with pytest.raises(StableRiskError) as caught:
            await fixture.service.request_revoke(
                candidate_artifact_id=fixture.candidate["id"],
                finding_id=finding["id"],
                actor=_admin(),
                expected_version=1,
                reason="Confirmed shared vulnerable file",
                confirm_affects_current_release=False,
                idempotency_key=request_key,
            )
        stable = await fixture.repository.get_artifact(fixture.stable["id"])
        updated = await fixture.repository.get_review_finding(
            fixture.candidate["id"], finding["id"]
        )
        plugin = fixture.store.get_plugin(fixture.plugin["id"])
        assert stable and updated and plugin
        return (
            caught.value.code,
            str(stable["publication_status"]),
            str(plugin["status"]),
            int(updated["version"]),
            bool(updated["affects_current_release"]),
            sum(item["action"] == "revoke" for item in fixture.repository.decisions.values()),
            sum(item["type"] == "revoke" for item in fixture.repository.jobs.values()),
        )

    result = asyncio.run(scenario())
    assert result == ("idempotency_key_conflict", "published", "listed", 1, False, 0, 0)


def test_stable_risk_rejects_finding_snapshot_drift_before_transaction(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str, str, int, bool, int]:
        fixture = await _stable_fixture(
            tmp_path, stable_main=b"SHARED = 1\n", candidate_main=b"SHARED = 1\n"
        )
        finding = await _add_finding(
            fixture,
            artifact=fixture.candidate,
            fingerprint="snapshot-shared-risk",
            source="static",
            deterministic=True,
            file_path="main.py",
            file_sha256=fixture.candidate_files["main.py"]["sha256"],
        )
        original_revoke = fixture.repository.request_revoke_artifact

        async def drift_then_revoke(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            stored = next(
                item
                for findings in fixture.repository.findings.values()
                for item in findings
                if item["id"] == finding["id"]
            )
            stored["file_sha256"] = "b" * 64
            return await original_revoke(*args, **kwargs)

        fixture.repository.request_revoke_artifact = drift_then_revoke  # type: ignore[method-assign]
        with pytest.raises(StableRiskError) as caught:
            await fixture.service.request_revoke(
                candidate_artifact_id=fixture.candidate["id"],
                finding_id=finding["id"],
                actor=_admin(),
                expected_version=1,
                reason="Confirmed shared vulnerable file",
                confirm_affects_current_release=False,
                idempotency_key="snapshot-drift",
            )
        stable = await fixture.repository.get_artifact(fixture.stable["id"])
        updated = await fixture.repository.get_review_finding(
            fixture.candidate["id"], finding["id"]
        )
        plugin = fixture.store.get_plugin(fixture.plugin["id"])
        assert stable and updated and plugin
        return (
            caught.value.code,
            str(stable["publication_status"]),
            str(plugin["status"]),
            int(updated["version"]),
            bool(updated["affects_current_release"]),
            sum(item["type"] == "revoke" for item in fixture.repository.jobs.values()),
        )

    assert asyncio.run(scenario()) == (
        "finding_version_conflict",
        "published",
        "listed",
        1,
        False,
        0,
    )


def test_stable_risk_route_hides_feed_handles_failure_and_notifies_admins(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path / "storage"),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "ARTIFACT_SUBMISSION_RPM": "0",
            "DATABASE_URL": "postgresql://example.invalid/market",
            "REDIS_URL": "redis://example.invalid/0",
            "GITHUB_METADATA_SYNC_ENABLED": "false",
            "EMAIL_PROVIDER": "disabled",
        }
    )
    store = InMemoryMarketStore()
    owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    reviewer = store.upsert_github_user(
        {"id": "reviewer-1", "login": "reviewer", "name": "Reviewer"}
    )
    store.update_user_role(reviewer["id"], "admin")
    plugin = store.submit_plugin(
        owner,
        {
            "name": "astrbot_plugin_stable_route",
            "display_name": "Stable Route",
            "desc": "Stable risk route fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_stable_route",
            "tags": [],
        },
    )
    store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.0.0"})
    app = create_app(settings=settings, store=store)
    with TestClient(app) as client:
        repository = app.state.artifact_runtime.repository
        storage = app.state.artifact_runtime.storage

        async def seed() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            stable = await repository.create_artifact(
                {
                    **_artifact_payload(plugin, owner, "route-stable"),
                    "version": "v1.0.0",
                    "normalized_version": "1.0.0",
                }
            )
            stable = await _seed_manifest(
                repository, storage, stable, {"main.py": (b"SHARED = 1\n", True)}
            )
            await repository.transition_publication_status(stable["id"], "publishing")
            stable = await repository.publish_artifact(
                stable["id"],
                expected_repo_version="v1.0.0",
                published_key="100/repo/v1.0.0/plugin.zip",
                download_url="https://cdn.example.test/100/repo/v1.0.0/plugin.zip",
            )
            assert stable is not None
            candidate = await repository.create_artifact(
                {
                    **_artifact_payload(plugin, owner, "route-candidate"),
                    "base_artifact_id": stable["id"],
                }
            )
            candidate = await _seed_manifest(
                repository,
                storage,
                candidate,
                {
                    "main.py": (b"SHARED = 1\n", True),
                    "README.md": (b"changed\n", True),
                },
            )
            files = {
                item["path"]: item for item in await repository.list_artifact_files(candidate["id"])
            }
            run = await repository.create_review_run(
                {"artifact_id": candidate["id"], "type": "static", "status": "succeeded"}
            )
            finding = (
                await repository.replace_findings(
                    candidate["id"],
                    run["id"],
                    [
                        {
                            "fingerprint": "route-shared-risk",
                            "severity": "critical",
                            "message": "shared risk",
                            "source": "static",
                            "deterministic": True,
                            "file_path": "main.py",
                            "file_sha256": files["main.py"]["sha256"],
                        }
                    ],
                )
            )[0]
            return stable, candidate, finding

        stable, candidate, finding = asyncio.run(seed())
        path = f"/v1/admin/artifacts/{candidate['id']}/findings/{finding['id']}/stable-risk"
        payload = {
            "expected_version": 1,
            "reason": "Confirmed shared vulnerable file",
            "confirm_affects_current_release": False,
        }
        owner_response = client.post(
            path,
            headers={"x-dev-github-login": "alice", "idempotency-key": "owner-stable-risk"},
            json=payload,
        )
        assert owner_response.status_code == 403
        response = client.post(
            path,
            headers={"x-dev-github-login": "reviewer", "idempotency-key": "route-stable-risk"},
            json=payload,
        )
        assert response.status_code == 200
        assert response.json()["correlation"]["kind"] == "path_sha"
        assert plugin["name"] not in client.get("/plugins.json").json()
        stored_stable = asyncio.run(repository.get_artifact(stable["id"]))
        assert stored_stable and stored_stable["publication_status"] == "revoking"
        assert stored_stable["published_key"]

        revoke_job = next(job for job in repository.jobs.values() if job["type"] == "revoke")
        revoke_job["max_attempts"] = 1
        original_revoke = storage.revoke_published

        async def fail_revoke(_: str) -> None:
            raise RuntimeError("origin unavailable")

        storage.revoke_published = fail_revoke
        asyncio.run(app.state.artifact_runtime.job_runner.run_once())
        failed = asyncio.run(repository.get_artifact(stable["id"]))
        assert failed and failed["publication_status"] == "revoke_failed"
        assert plugin["name"] not in client.get("/plugins.json").json()
        storage.revoke_published = original_revoke
        owner_notifications = store.list_notifications(owner["id"])
        admin_notifications = store.list_notifications(reviewer["id"])
        assert any("撤回失败" in item["title"] for item in owner_notifications)
        assert any("撤回失败" in item["title"] for item in admin_notifications)
        assert all(
            "main.py" not in item["body"] for item in owner_notifications + admin_notifications
        )


class _StableFixture:
    def __init__(
        self,
        *,
        store: InMemoryMarketStore,
        repository: InMemoryArtifactRepository,
        service: StableRiskService,
        plugin: dict[str, Any],
        stable: dict[str, Any],
        candidate: dict[str, Any],
        stable_files: dict[str, dict[str, Any]],
        candidate_files: dict[str, dict[str, Any]],
    ) -> None:
        self.store = store
        self.repository = repository
        self.service = service
        self.plugin = plugin
        self.stable = stable
        self.candidate = candidate
        self.stable_files = stable_files
        self.candidate_files = candidate_files


async def _stable_fixture(
    tmp_path: Path,
    *,
    stable_main: bytes,
    candidate_main: bytes,
) -> _StableFixture:
    store = InMemoryMarketStore()
    owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        owner,
        {
            "name": "astrbot_plugin_stable",
            "display_name": "Stable",
            "desc": "Stable risk fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_stable",
            "tags": [],
        },
    )
    store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.0.0"})
    repository = InMemoryArtifactRepository(store)
    storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
    stable = await repository.create_artifact(
        {
            **_artifact_payload(plugin, owner, "stable"),
            "version": "v1.0.0",
            "normalized_version": "1.0.0",
        }
    )
    stable = await _seed_manifest(
        repository,
        storage,
        stable,
        {"main.py": (stable_main, True)},
    )
    await repository.transition_publication_status(stable["id"], "publishing")
    stable = await repository.publish_artifact(
        stable["id"],
        expected_repo_version="v1.0.0",
        published_key="100/repo/v1.0.0/plugin.zip",
        download_url="https://cdn.example.test/100/repo/v1.0.0/plugin.zip",
    )
    assert stable is not None
    candidate = await repository.create_artifact(
        {**_artifact_payload(plugin, owner, "candidate"), "base_artifact_id": stable["id"]}
    )
    candidate = await _seed_manifest(
        repository,
        storage,
        candidate,
        {"main.py": (candidate_main, True), "README.md": (b"changed\n", True)},
    )
    stable_files = {
        item["path"]: item for item in await repository.list_artifact_files(stable["id"])
    }
    candidate_files = {
        item["path"]: item for item in await repository.list_artifact_files(candidate["id"])
    }
    return _StableFixture(
        store=store,
        repository=repository,
        service=StableRiskService(repository, ArtifactContentService(repository, storage)),
        plugin=plugin,
        stable=stable,
        candidate=candidate,
        stable_files=stable_files,
        candidate_files=candidate_files,
    )


async def _add_finding(
    fixture: _StableFixture,
    *,
    artifact: Mapping[str, Any],
    fingerprint: str,
    source: str,
    deterministic: bool,
    file_path: str = "",
    file_sha256: str = "",
    tool_name: str = "",
    tool_version: str = "",
    ruleset_version: str = "",
    correlation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    run = await fixture.repository.create_review_run(
        {
            "artifact_id": artifact["id"],
            "type": "llm_file" if source == "llm" else "static",
            "status": "succeeded",
            "tool_name": tool_name,
            "tool_version": tool_version,
            "ruleset_version": ruleset_version,
            "idempotency_key": f"run:{artifact['id']}:{fingerprint}",
        }
    )
    return (
        await fixture.repository.replace_findings(
            artifact["id"],
            run["id"],
            [
                {
                    "fingerprint": fingerprint,
                    "rule_id": fingerprint,
                    "severity": "critical",
                    "message": "critical risk",
                    "source": source,
                    "deterministic": deterministic,
                    "file_path": file_path,
                    "file_sha256": file_sha256 or None,
                    "correlation": dict(correlation or {}),
                }
            ],
        )
    )[0]


def _artifact_payload(
    plugin: Mapping[str, Any], user: Mapping[str, Any], marker: str
) -> dict[str, Any]:
    digest = hashlib.sha256(marker.encode()).hexdigest()
    return {
        "plugin_id": plugin["id"],
        "source_type": "upload",
        "source_repo": plugin["repo"],
        "archive_sha256": digest,
        "size_bytes": 128,
        "quarantine_key": f"quarantine/{digest[:12]}.zip",
        "submitted_by": user["id"],
    }


async def _seed_manifest(
    repository: InMemoryArtifactRepository,
    storage: LocalArtifactStorage,
    artifact: Mapping[str, Any],
    files: Mapping[str, tuple[bytes, bool]],
) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    for index, (path, (content, is_text)) in enumerate(sorted(files.items())):
        file_id = f"file_{str(artifact['id']).removeprefix('artifact_')}_{index}"
        content_key = build_content_key(str(artifact["id"]), file_id) if is_text else None
        if content_key:
            await storage.put_text_content(content_key, content)
        manifests.append(
            {
                "id": file_id,
                "path": path,
                "language": "python" if path.endswith(".py") else "text",
                "mime_type": "text/x-python" if path.endswith(".py") else "text/plain",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "line_count": len(content.decode().splitlines()) if is_text else None,
                "is_text": is_text,
                "content_key": content_key,
            }
        )
    await repository.replace_artifact_files(
        str(artifact["id"]), manifests, manifest_tree_sha256(manifests)
    )
    updated = await repository.get_artifact(str(artifact["id"]))
    assert updated is not None
    return updated


def _admin() -> dict[str, Any]:
    return {"id": "admin-1", "role": "admin", "nickname": "Admin"}
