from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.artifacts.file_review import (
    FileCandidateSelector,
    FileFindingV1,
    FileInputBuilder,
    FileReviewEvaluation,
    FileReviewResultV1,
    FileReviewService,
    SelectionReason,
    artifact_llm_budget,
    latest_package_result,
    verified_file_findings,
)
from app.artifacts.jobs import ArtifactJobRunner
from app.artifacts.models import ReviewStatus
from app.artifacts.package_review import LlmBudgetExceeded, LlmOutputInvalid
from app.artifacts.policy import (
    LlmPolicy,
    ReviewPolicyStage,
    ReviewPolicyV1,
    review_policy_sha256,
)
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.stages import (
    LlmFileStage,
    LlmSummaryStage,
    StageContext,
    StageOutcomeKind,
)
from app.artifacts.storage import LocalArtifactStorage
from app.artifacts.structured_llm import (
    DeterministicStructuredLlmProvider,
    StructuredLlmRequest,
    StructuredLlmResponse,
    redact_llm_source,
)
from app.artifacts.summary_review import (
    ReviewSummaryResultV1,
    SummaryInputBuilder,
    SummaryReviewService,
)
from app.store import InMemoryMarketStore


def _llm_policy(**updates: Any) -> LlmPolicy:
    payload: dict[str, Any] = {
        "enabled": True,
        "provider_config_ref": "config:llm-default",
        "model": "review-model-v1",
        "prompt_version": "review-prompt-v1",
        "max_tokens": 50_000,
        "max_cost_microusd": 1_000_000,
        "input_cost_microusd_per_million_tokens": 1_000_000,
        "output_cost_microusd_per_million_tokens": 4_000_000,
        "max_files": 20,
        "max_file_bytes": 262_144,
        "required_files": ["main.py"],
        "timeout_seconds": 30,
        "max_retries": 1,
    }
    payload.update(updates)
    return LlmPolicy.model_validate(payload)


def _package_result(**updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "risk_level": "low",
        "risk_summary": "Package metadata suggests network behavior.",
        "suggested_files": ["helper.py"],
        "suggested_category": "utilities",
        "confidence": 0.8,
        "reasons": ["The package registers network-facing handlers."],
        "coverage_notes": [],
        "needs_manual_review": True,
    }
    payload.update(updates)
    return payload


def _file_result(**updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "risk_level": "medium",
        "summary": "The file performs a network request.",
        "findings": [],
        "coverage_notes": [],
        "needs_manual_review": True,
    }
    payload.update(updates)
    return payload


def _summary_result(**updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "review_priority": "high",
        "risk_level": "medium",
        "summary": "Manual review should focus on network and dynamic execution behavior.",
        "key_points": ["Review the network destination and eval usage."],
        "coverage_notes": [],
        "needs_manual_review": True,
    }
    payload.update(updates)
    return payload


class _SchemaRoutingProvider:
    name = "routing-structured-llm"
    version = "routing-structured-llm-v1"

    def __init__(
        self,
        *,
        file_result: dict[str, Any] | None = None,
        summary_result: dict[str, Any] | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        self.file_result = file_result or _file_result()
        self.summary_result = summary_result or _summary_result()
        self.usage = usage or {
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "total_tokens": 280,
        }
        self.requests: list[StructuredLlmRequest] = []

    async def complete(self, request: StructuredLlmRequest) -> StructuredLlmResponse:
        self.requests.append(request)
        if request.schema_name == "astrbot_plugin_file_review":
            result = self.file_result
        elif request.schema_name == "astrbot_plugin_review_summary":
            result = self.summary_result
        else:
            result = _package_result()
        content = json.dumps(result)
        return StructuredLlmResponse(
            content=content,
            raw_response={"content": content},
            usage=self.usage,
        )


async def _fixture(
    root: Path,
    *,
    llm_policy: LlmPolicy | None = None,
    provider_version: str = "deterministic-structured-llm-v1",
    manual_review_at: str = "low",
    static_severity: str = "medium",
    package_review_result: dict[str, Any] | None = None,
) -> tuple[
    InMemoryArtifactRepository,
    LocalArtifactStorage,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    store = InMemoryMarketStore()
    user = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.register_plugin(
        user,
        {
            "name": "astrbot_plugin_demo",
            "display_name": "Demo",
            "desc": "Demo plugin",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_demo",
            "category": "other",
            "category_explicit": False,
        },
    )
    repository = InMemoryArtifactRepository(store)
    configured_llm = llm_policy or _llm_policy()
    policy_model = ReviewPolicyV1.model_validate(
        {
            "schema_version": "1",
            "required_stages": ["static", "llm_package", "llm_file", "llm_summary"],
            "limits": {
                "cpu": 1,
                "memory_mb": 768,
                "pids": 128,
                "timeout_seconds": 120,
            },
            "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
            "llm": configured_llm.model_dump(mode="json"),
            "category": {"enabled": False},
            "dependency": {"enabled": False},
            "malware": {"clamav": False},
            "routing": {
                "auto_approve": False,
                "manual_review_at": manual_review_at,
            },
        }
    )
    policy = await repository.create_review_policy(
        {
            "version": "file-policy-v1",
            "schema_version": "1",
            "status": "active",
            "is_default": True,
            "policy": policy_model.model_dump(mode="json"),
            "policy_sha256": review_policy_sha256(policy_model),
            "validation_summary": {"valid": True},
        }
    )
    artifact = await repository.create_artifact(
        {
            "plugin_id": plugin["id"],
            "version": "v1.0.0",
            "normalized_version": "1.0.0",
            "source_type": "upload",
            "source_repo": plugin["repo"],
            "archive_sha256": "a" * 64,
            "tree_sha256": "b" * 64,
            "size_bytes": 1024,
            "quarantine_key": "artifacts/file-review/source.zip",
            "submitted_by": user["id"],
            "policy_version_id": policy["id"],
        }
    )
    await repository.transition_review_status(artifact["id"], ReviewStatus.PRECHECKING.value)
    artifact = await repository.transition_review_status(
        artifact["id"], ReviewStatus.SCANNING.value
    )
    assert artifact is not None
    storage = LocalArtifactStorage(root, "https://cdn.example.test")
    payloads = {
        "README.md": b"Plugin documentation\n",
        "assets/logo.png": b"\x89PNG\r\n\x1a\n",
        "helper.py": b"def helper():\n    return 1\n",
        "main.py": (
            b"import httpx\n"
            b"API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n"
            b"async def run(url):\n"
            b"    await client.get(url)\n"
        ),
        "risky.py": b"def risky(user_input):\n    return eval(user_input)\n",
    }
    files: list[dict[str, Any]] = []
    for index, (path, content) in enumerate(sorted(payloads.items())):
        is_text = not path.endswith(".png")
        digest = hashlib.sha256(content).hexdigest()
        key = f"artifacts/{artifact['id']}/files/file-{index}.txt" if is_text else None
        if key:
            await storage.put_text_content(key, content)
        files.append(
            {
                "id": f"file-{index}",
                "path": path,
                "language": "python" if path.endswith(".py") else "text",
                "mime_type": "text/plain" if is_text else "image/png",
                "sha256": digest,
                "size_bytes": len(content),
                "line_count": len(content.decode(errors="ignore").split("\n")) if is_text else None,
                "is_text": is_text,
                "content_key": key,
                "is_entrypoint": path == "main.py",
                "graph_status": "complete" if path.endswith(".py") else "not_analyzed",
            }
        )
    await repository.replace_artifact_files(artifact["id"], files, "b" * 64)
    by_path = {item["path"]: item for item in files}
    static_run = await repository.create_review_run(
        {
            "artifact_id": artifact["id"],
            "type": "static",
            "status": "succeeded",
            "tool_name": "static",
            "tool_version": "p1.1",
            "policy_version_id": policy["id"],
            "coverage": {"outcome": "completed", "stage_name": "static"},
        }
    )
    await repository.replace_findings(
        artifact["id"],
        static_run["id"],
        [
            {
                "fingerprint": "f" * 64,
                "rule_id": "dynamic-eval",
                "file_path": "risky.py",
                "line_start": 2,
                "line_end": 2,
                "severity": static_severity,
                "category": "code_execution",
                "message": "Dynamic eval requires review.",
                "evidence_excerpt": "return eval(user_input)",
                "deterministic": True,
                "file_id": by_path["risky.py"]["id"],
                "file_sha256": by_path["risky.py"]["sha256"],
            }
        ],
    )
    await repository.create_review_run(
        {
            "artifact_id": artifact["id"],
            "type": "llm_package",
            "status": "succeeded",
            "tool_name": "structured-llm",
            "tool_version": provider_version,
            "model": configured_llm.model,
            "prompt_version": configured_llm.prompt_version,
            "result_schema_version": "1",
            "policy_version_id": policy["id"],
            "input_sha256": "c" * 64,
            "output_sha256": "d" * 64,
            "coverage": {
                "outcome": "completed",
                "complete": True,
                "stage_name": "llm_package",
                "provider_call": True,
                "usage": {
                    "prompt_tokens": 1400,
                    "completion_tokens": 300,
                    "total_tokens": 1700,
                    "cost_microusd": 2600,
                },
            },
            "raw_result": {"normalized_result": package_review_result or _package_result()},
        }
    )
    await repository.replace_artifact_diffs(
        artifact["id"],
        None,
        current_tree_sha256="b" * 64,
        base_tree_sha256=None,
        diffs=[
            {
                "path": "helper.py",
                "change_type": "added",
                "current_file_id": by_path["helper.py"]["id"],
                "current_sha256": by_path["helper.py"]["sha256"],
                "stats": {"additions": 2, "deletions": 0},
            }
        ],
    )
    await repository.replace_dependency_edges(
        artifact["id"],
        tree_sha256="b" * 64,
        edges=[
            {
                "source_file_id": by_path["main.py"]["id"],
                "target_file_id": by_path["helper.py"]["id"],
                "target_name": "helper",
                "edge_type": "import",
                "confidence": 1,
            }
        ],
    )
    file_job = {
        "id": "job-file-1",
        "artifact_id": artifact["id"],
        "type": "llm_file",
        "attempts": 1,
        "policy_version_id": policy["id"],
        "payload": {"stage": "llm_file", "input_sha256": "e" * 64},
    }
    summary_job = {
        "id": "job-summary-1",
        "artifact_id": artifact["id"],
        "type": "llm_summary",
        "attempts": 1,
        "policy_version_id": policy["id"],
        "payload": {"stage": "llm_summary", "input_sha256": "a" * 64},
    }
    return repository, storage, artifact, policy, file_job, summary_job


def _context(
    repository: InMemoryArtifactRepository,
    storage: LocalArtifactStorage,
    artifact: dict[str, Any],
    policy: dict[str, Any],
    job: dict[str, Any],
) -> StageContext:
    return StageContext.create(
        job=job,
        artifact=artifact,
        policy=policy,
        repository=repository,
        storage=storage,
        tools={},
        logger=logging.getLogger("test-file-review"),
    )


def test_file_and_summary_results_are_strict_structured_json() -> None:
    finding = {
        "rule_id": "network-call",
        "severity": "medium",
        "category": "network",
        "line_start": 2,
        "line_end": 2,
        "message": "The handler performs a network request.",
        "suggestion": "Validate the destination and timeout.",
        "evidence_excerpt": "await client.get(url)",
        "confidence": 0.9,
    }
    assert FileFindingV1.model_validate(finding).line_start == 2
    with pytest.raises(ValidationError):
        FileReviewResultV1.model_validate(
            {
                "schema_version": "1",
                "risk_level": "medium",
                "summary": "Review required.",
                "findings": [finding],
                "coverage_notes": [],
                "needs_manual_review": True,
                "command": "approve",
            }
        )
    with pytest.raises(ValidationError):
        ReviewSummaryResultV1.model_validate(
            {
                "schema_version": "1",
                "review_priority": "high",
                "risk_level": "medium",
                "summary": "Review required.",
                "key_points": ["Network behavior"],
                "coverage_notes": [],
                "needs_manual_review": True,
                "decision": "approve",
            }
        )

    file_schema = FileReviewResultV1.model_json_schema()
    summary_schema = ReviewSummaryResultV1.model_json_schema()
    assert set(file_schema["required"]) == set(file_schema["properties"])
    assert set(summary_schema["required"]) == set(summary_schema["properties"])
    assert file_schema["additionalProperties"] is False
    assert summary_schema["additionalProperties"] is False


def test_candidate_selection_is_deterministic_and_records_ineligible_files(
    tmp_path: Path,
) -> None:
    async def scenario() -> Any:
        policy = _llm_policy(required_files=["main.py", "missing.py"])
        repository, _, artifact, _, _, _ = await _fixture(
            tmp_path,
            llm_policy=policy,
        )
        return await FileCandidateSelector(repository).build(
            artifact,
            policy,
            required_stages=(
                ReviewPolicyV1.model_validate(
                    {
                        "schema_version": "1",
                        "required_stages": ["static", "llm_package", "llm_file"],
                        "limits": {
                            "cpu": 1,
                            "memory_mb": 768,
                            "pids": 128,
                            "timeout_seconds": 120,
                        },
                        "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
                        "llm": policy.model_dump(mode="json"),
                        "category": {"enabled": False},
                        "dependency": {"enabled": False},
                        "malware": {"clamav": False},
                        "routing": {"auto_approve": False},
                    }
                ).required_stages
            ),
        )

    plan = asyncio.run(scenario())
    by_path = {item.path: item for item in plan.candidates}

    assert plan.candidates[0].path == "main.py"
    assert SelectionReason.ENTRYPOINT in by_path["main.py"].reasons
    assert SelectionReason.POLICY_REQUIRED in by_path["main.py"].reasons
    assert SelectionReason.DETERMINISTIC_FINDING in by_path["risky.py"].reasons
    assert SelectionReason.CHANGED in by_path["helper.py"].reasons
    assert SelectionReason.ENTRY_DEPENDENCY in by_path["helper.py"].reasons
    assert SelectionReason.PACKAGE_SUGGESTED in by_path["helper.py"].reasons
    assert ("missing.py", "policy_required_missing") in {
        (item.path, item.reason) for item in plan.skipped
    }
    assert ("assets/logo.png", "non_text") in {(item.path, item.reason) for item in plan.skipped}
    assert ("README.md", "not_selected") in {(item.path, item.reason) for item in plan.skipped}
    assert plan.complete is False


def test_candidate_selection_rejects_unknown_binary_and_oversize_suggestions(
    tmp_path: Path,
) -> None:
    async def scenario() -> Any:
        policy = _llm_policy(max_file_bytes=1024)
        repository, _, artifact, _, _, _ = await _fixture(
            tmp_path,
            llm_policy=policy,
            package_review_result=_package_result(
                suggested_files=["helper.py", "unknown.py", "assets/logo.png"]
            ),
        )
        files = await repository.list_artifact_files(artifact["id"])
        main = next(item for item in files if item["path"] == "main.py")
        main["size_bytes"] = policy.max_file_bytes + 1
        await repository.replace_artifact_files(artifact["id"], files, "b" * 64)
        return await FileCandidateSelector(repository).build(
            artifact,
            policy,
            required_stages=(),
        )

    plan = asyncio.run(scenario())
    skipped = {(item.path, item.reason) for item in plan.skipped}

    assert ("unknown.py", "package_suggestion_invalid") in skipped
    assert ("assets/logo.png", "non_text") in skipped
    assert ("main.py", "file_too_large") in skipped
    assert "unknown.py" not in {item.path for item in plan.candidates}
    assert "assets/logo.png" not in {item.path for item in plan.candidates}
    assert "main.py" not in {item.path for item in plan.candidates}
    assert plan.complete is False


def test_candidate_selection_includes_import_graph_incremental_scope(tmp_path: Path) -> None:
    async def scenario() -> Any:
        policy = _llm_policy()
        repository, _, artifact, _, _, _ = await _fixture(tmp_path, llm_policy=policy)
        await repository.update_artifact_review_coverage(
            artifact["id"],
            {
                "import_graph": {
                    "outcome": "completed",
                    "complete": True,
                    "review_paths": ["README.md"],
                }
            },
        )
        refreshed = await repository.get_artifact(artifact["id"])
        assert refreshed is not None
        return await FileCandidateSelector(repository).build(
            refreshed,
            policy,
            required_stages=(
                ReviewPolicyStage.STATIC,
                ReviewPolicyStage.IMPORT_GRAPH,
                ReviewPolicyStage.LLM_PACKAGE,
                ReviewPolicyStage.LLM_FILE,
            ),
        )

    plan = asyncio.run(scenario())
    by_path = {item.path: item for item in plan.candidates}

    assert SelectionReason.INCREMENTAL_IMPACT in by_path["README.md"].reasons


def test_file_input_preserves_lines_redacts_credentials_and_binds_sha(tmp_path: Path) -> None:
    async def scenario() -> Any:
        policy = _llm_policy()
        repository, storage, artifact, _, _, _ = await _fixture(tmp_path)
        plan = await FileCandidateSelector(repository).build(
            artifact,
            policy,
            required_stages=(),
        )
        candidate = next(item for item in plan.candidates if item.path == "main.py")
        content = await storage.read_text_content(
            candidate.content_key,
            candidate.size_bytes + 1,
            candidate.sha256,
        )
        return FileInputBuilder().build(
            candidate,
            content,
            package_result=latest_package_result(await repository.list_review_runs(artifact["id"])),
            remaining_tokens=30_000,
            remaining_cost_microusd=500_000,
            policy=policy,
        )

    prepared = asyncio.run(scenario())
    payload = prepared.input.canonical_json()

    assert prepared.input.path == "main.py"
    assert prepared.input.line_count == 5
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in payload
    assert "[REDACTED]" in prepared.source_view
    assert prepared.source_view.split("\n")[3] == "    await client.get(url)"
    assert prepared.input_sha256 == hashlib.sha256(payload.encode()).hexdigest()


def test_multiline_secret_redaction_preserves_following_line_numbers() -> None:
    source = (
        "before\n-----BEGIN PRIVATE KEY-----\nsecret-material\n-----END PRIVATE KEY-----\nafter\n"
    )

    redacted = redact_llm_source(source, maximum=10_000)

    assert "secret-material" not in redacted
    assert len(redacted.split("\n")) == len(source.split("\n"))
    assert redacted.split("\n")[4] == "after"


def test_file_result_evidence_is_verified_before_finding_persistence(tmp_path: Path) -> None:
    finding = {
        "rule_id": "network-call",
        "severity": "high",
        "category": "network",
        "line_start": 4,
        "line_end": 4,
        "message": "The handler performs a network request.",
        "suggestion": "Validate the destination and timeout.",
        "evidence_excerpt": "await client.get(url)",
        "confidence": 0.95,
    }

    async def scenario() -> tuple[Any, Any, bytes, list[dict[str, Any]]]:
        policy = _llm_policy()
        repository, storage, artifact, _, _, _ = await _fixture(tmp_path)
        plan = await FileCandidateSelector(repository).build(
            artifact,
            policy,
            required_stages=(),
        )
        candidate = next(item for item in plan.candidates if item.path == "main.py")
        content = await storage.read_text_content(
            candidate.content_key,
            candidate.size_bytes + 1,
            candidate.sha256,
        )
        prepared = FileInputBuilder().build(
            candidate,
            content,
            package_result=None,
            remaining_tokens=30_000,
            remaining_cost_microusd=500_000,
            policy=policy,
        )
        service = FileReviewService(
            DeterministicStructuredLlmProvider(_file_result(findings=[finding], risk_level="high")),
            retry_delay_seconds=0,
        )
        evaluation = await service.evaluate(
            prepared,
            policy=policy,
            remaining_tokens=30_000,
            remaining_cost_microusd=500_000,
        )
        existing = await repository.list_findings(artifact["id"])
        return prepared, evaluation, content, existing

    prepared, evaluation, content, existing = asyncio.run(scenario())
    normalized = verified_file_findings(evaluation, prepared, content, existing)

    assert len(normalized) == 1
    assert normalized[0]["source"] == "llm"
    assert normalized[0]["deterministic"] is False
    assert normalized[0]["file_sha256"] == prepared.input.sha256
    assert normalized[0]["evidence_excerpt"] == "await client.get(url)"
    assert normalized[0]["fingerprint"] != "f" * 64


@pytest.mark.parametrize(
    "finding",
    [
        {
            "rule_id": "bad-line",
            "severity": "medium",
            "category": "quality",
            "line_start": 99,
            "line_end": 99,
            "message": "Bad line.",
            "suggestion": "Review it.",
            "evidence_excerpt": "missing",
            "confidence": 0.8,
        },
        {
            "rule_id": "bad-evidence",
            "severity": "medium",
            "category": "quality",
            "line_start": 4,
            "line_end": 4,
            "message": "Bad evidence.",
            "suggestion": "Review it.",
            "evidence_excerpt": "os.system(command)",
            "confidence": 0.8,
        },
    ],
)
def test_invalid_line_or_evidence_never_becomes_a_finding(
    tmp_path: Path,
    finding: dict[str, Any],
) -> None:
    async def scenario() -> tuple[Any, Any, bytes]:
        policy = _llm_policy()
        repository, storage, artifact, _, _, _ = await _fixture(tmp_path)
        plan = await FileCandidateSelector(repository).build(artifact, policy, required_stages=())
        candidate = next(item for item in plan.candidates if item.path == "main.py")
        content = await storage.read_text_content(
            candidate.content_key,
            candidate.size_bytes + 1,
            candidate.sha256,
        )
        prepared = FileInputBuilder().build(
            candidate,
            content,
            package_result=None,
            remaining_tokens=30_000,
            remaining_cost_microusd=500_000,
            policy=policy,
        )
        evaluation = FileReviewEvaluation(
            result=FileReviewResultV1.model_validate(_file_result(findings=[finding])),
            raw_response={},
            usage={"total_tokens": 1, "cost_microusd": 1},
            attempts=1,
            output_sha256="a" * 64,
        )
        return prepared, evaluation, content

    prepared, evaluation, content = asyncio.run(scenario())

    with pytest.raises(LlmOutputInvalid, match="unverified_model_output"):
        verified_file_findings(evaluation, prepared, content, [])


def test_sha_drift_and_budget_exhaustion_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> Any:
        policy = _llm_policy()
        repository, storage, artifact, _, _, _ = await _fixture(tmp_path)
        plan = await FileCandidateSelector(repository).build(artifact, policy, required_stages=())
        candidate = next(item for item in plan.candidates if item.path == "main.py")
        content = await storage.read_text_content(
            candidate.content_key,
            candidate.size_bytes + 1,
            candidate.sha256,
        )
        with pytest.raises(LlmBudgetExceeded):
            FileInputBuilder().build(
                candidate,
                content,
                package_result=None,
                remaining_tokens=100,
                remaining_cost_microusd=100,
                policy=policy,
            )
        return FileInputBuilder().build(
            candidate,
            content,
            package_result=None,
            remaining_tokens=30_000,
            remaining_cost_microusd=500_000,
            policy=policy,
        )

    prepared = asyncio.run(scenario())
    evaluation = FileReviewEvaluation(
        result=FileReviewResultV1.model_validate(_file_result()),
        raw_response={},
        usage={"total_tokens": 1, "cost_microusd": 1},
        attempts=1,
        output_sha256="a" * 64,
    )
    with pytest.raises(LlmOutputInvalid, match="SHA"):
        verified_file_findings(evaluation, prepared, b"changed", [])


def test_repeated_llm_finding_cannot_lower_existing_severity(tmp_path: Path) -> None:
    base_finding = {
        "rule_id": "network-call",
        "severity": "low",
        "category": "network",
        "line_start": 4,
        "line_end": 4,
        "message": "The handler performs a network request.",
        "suggestion": "Validate the destination.",
        "evidence_excerpt": "await client.get(url)",
        "confidence": 0.7,
    }

    async def scenario() -> tuple[Any, bytes]:
        policy = _llm_policy()
        repository, storage, artifact, _, _, _ = await _fixture(tmp_path)
        plan = await FileCandidateSelector(repository).build(artifact, policy, required_stages=())
        candidate = next(item for item in plan.candidates if item.path == "main.py")
        content = await storage.read_text_content(
            candidate.content_key,
            candidate.size_bytes + 1,
            candidate.sha256,
        )
        prepared = FileInputBuilder().build(
            candidate,
            content,
            package_result=None,
            remaining_tokens=30_000,
            remaining_cost_microusd=500_000,
            policy=policy,
        )
        return prepared, content

    prepared, content = asyncio.run(scenario())
    low = FileReviewEvaluation(
        result=FileReviewResultV1.model_validate(_file_result(findings=[base_finding])),
        raw_response={},
        usage={},
        attempts=1,
        output_sha256="a" * 64,
    )
    first = verified_file_findings(low, prepared, content, [])
    previous = {**first[0], "severity": "high", "deterministic": False}
    second = verified_file_findings(low, prepared, content, [previous])

    assert second[0]["fingerprint"] == first[0]["fingerprint"]
    assert second[0]["severity"] == "high"
    with pytest.raises(LlmOutputInvalid, match="deterministic"):
        verified_file_findings(
            low,
            prepared,
            content,
            [{**previous, "deterministic": True}],
        )
    duplicate = FileReviewEvaluation(
        result=FileReviewResultV1.model_validate(
            _file_result(findings=[base_finding, base_finding])
        ),
        raw_response={},
        usage={},
        attempts=1,
        output_sha256="a" * 64,
    )
    with pytest.raises(LlmOutputInvalid, match="duplicate finding fingerprint"):
        verified_file_findings(duplicate, prepared, content, [])


def test_artifact_budget_deducts_committed_package_usage(tmp_path: Path) -> None:
    async def scenario() -> Any:
        policy = _llm_policy()
        repository, _, artifact, record, _, _ = await _fixture(tmp_path)
        budget = artifact_llm_budget(
            await repository.list_review_runs(artifact["id"]),
            policy,
            record["id"],
        )
        return budget

    budget = asyncio.run(scenario())

    assert budget.used_tokens == 1700
    assert budget.used_cost_microusd == 2600
    assert budget.remaining_tokens == 48_300


def test_file_stage_creates_one_dag_aggregate_and_isolated_child_runs(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        provider = _SchemaRoutingProvider()
        repository, storage, artifact, policy, file_job, _ = await _fixture(
            tmp_path,
            provider_version=provider.version,
        )
        stage = LlmFileStage(
            FileReviewService(provider, retry_delay_seconds=0),
            provider_config_ref="config:llm-default",
        )
        outcome = await stage.execute(_context(repository, storage, artifact, policy, file_job))
        runs = [
            run
            for run in await repository.list_review_runs(artifact["id"])
            if run["type"] == "llm_file"
        ]
        findings = await repository.list_findings(artifact["id"])
        return outcome, runs, findings

    outcome, runs, findings = asyncio.run(scenario())

    assert outcome.kind == StageOutcomeKind.COMPLETED
    aggregate = [run for run in runs if run["coverage"]["stage_name"] == "llm_file"]
    children = [run for run in runs if run["coverage"]["stage_name"].startswith("llm_file:file:")]
    assert len(aggregate) == 1
    assert aggregate[0]["status"] == "succeeded"
    assert aggregate[0]["coverage"]["reviewed_file_count"] == 3
    assert len(children) == 3
    assert all(run["status"] == "succeeded" for run in children)
    assert all(run["coverage"]["stage_name"] != "llm_file" for run in children)
    static = next(item for item in findings if item["fingerprint"] == "f" * 64)
    assert static["deterministic"] is True
    assert static["source"] == "static"


def test_unverified_file_output_degrades_stage_without_writing_llm_finding(
    tmp_path: Path,
) -> None:
    invalid = _file_result(
        findings=[
            {
                "rule_id": "invalid-evidence",
                "severity": "critical",
                "category": "security",
                "line_start": 999,
                "line_end": 999,
                "message": "Invalid evidence.",
                "suggestion": "Review manually.",
                "evidence_excerpt": "not present",
                "confidence": 1,
            }
        ]
    )

    async def scenario() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        provider = _SchemaRoutingProvider(file_result=invalid)
        policy_model = _llm_policy(max_files=1)
        repository, storage, artifact, policy, file_job, _ = await _fixture(
            tmp_path,
            llm_policy=policy_model,
            provider_version=provider.version,
        )
        outcome = await LlmFileStage(
            FileReviewService(provider, retry_delay_seconds=0),
            provider_config_ref="config:llm-default",
        ).execute(_context(repository, storage, artifact, policy, file_job))
        return (
            outcome,
            await repository.list_review_runs(artifact["id"]),
            await repository.list_findings(artifact["id"]),
        )

    outcome, runs, findings = asyncio.run(scenario())

    assert outcome.kind == StageOutcomeKind.DEGRADED
    assert outcome.error_code == "llm_output_invalid"
    child = next(
        run
        for run in runs
        if str((run.get("coverage") or {}).get("stage_name") or "").startswith("llm_file:file:")
    )
    assert child["status"] == "failed"
    assert child["coverage"]["provider_call"] is True
    assert child["coverage"]["usage"]["total_tokens"] > 0
    assert [item for item in findings if item["source"] == "llm"] == []


def test_file_budget_exhaustion_is_partial_manual_not_clean(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, dict[str, Any], int]:
        provider = _SchemaRoutingProvider()
        policy_model = _llm_policy(max_tokens=2200, max_cost_microusd=100_000)
        repository, storage, artifact, policy, file_job, _ = await _fixture(
            tmp_path,
            llm_policy=policy_model,
            provider_version=provider.version,
        )
        outcome = await LlmFileStage(
            FileReviewService(provider, retry_delay_seconds=0),
            provider_config_ref="config:llm-default",
        ).execute(_context(repository, storage, artifact, policy, file_job))
        aggregate = next(
            run
            for run in await repository.list_review_runs(artifact["id"])
            if run["type"] == "llm_file"
            and (run.get("coverage") or {}).get("stage_name") == "llm_file"
        )
        return outcome, aggregate, len(provider.requests)

    outcome, aggregate, request_count = asyncio.run(scenario())

    assert outcome.kind == StageOutcomeKind.COMPLETED
    assert request_count == 0
    assert aggregate["coverage"]["complete"] is False
    assert aggregate["coverage"]["manual_review_required"] is True
    assert aggregate["coverage"]["reviewed_file_count"] == 0
    assert aggregate["coverage"]["skipped_file_count"] >= 1


def test_actual_over_budget_provider_call_is_charged_to_artifact(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, dict[str, Any], Any]:
        provider = _SchemaRoutingProvider(
            usage={
                "prompt_tokens": 40_000,
                "completion_tokens": 10_000,
                "total_tokens": 50_000,
            }
        )
        llm_policy = _llm_policy(max_tokens=10_000)
        repository, storage, artifact, policy, file_job, _ = await _fixture(
            tmp_path,
            llm_policy=llm_policy,
            provider_version=provider.version,
        )
        outcome = await LlmFileStage(
            FileReviewService(provider, retry_delay_seconds=0),
            provider_config_ref="config:llm-default",
        ).execute(_context(repository, storage, artifact, policy, file_job))
        child = next(
            run
            for run in await repository.list_review_runs(artifact["id"])
            if str((run.get("coverage") or {}).get("stage_name") or "").startswith("llm_file:file:")
        )
        budget = artifact_llm_budget(
            await repository.list_review_runs(artifact["id"]),
            llm_policy,
            policy["id"],
        )
        return outcome, child, budget

    outcome, child, budget = asyncio.run(scenario())

    assert outcome.kind == StageOutcomeKind.DEGRADED
    assert outcome.error_code == "llm_budget_exceeded"
    assert child["coverage"]["provider_call"] is True
    assert child["coverage"]["usage"]["total_tokens"] == 50_000
    assert outcome.coverage["budget"]["used_tokens"] == 51_700
    assert budget.used_tokens == 51_700
    assert budget.remaining_tokens == 0


def test_file_stage_uses_policy_manual_review_threshold(tmp_path: Path) -> None:
    async def scenario() -> Any:
        provider = _SchemaRoutingProvider(
            file_result=_file_result(
                risk_level="low",
                needs_manual_review=False,
            )
        )
        repository, storage, artifact, policy, file_job, _ = await _fixture(
            tmp_path,
            provider_version=provider.version,
            manual_review_at="medium",
            static_severity="low",
        )
        return await LlmFileStage(
            FileReviewService(provider, retry_delay_seconds=0),
            provider_config_ref="config:llm-default",
        ).execute(_context(repository, storage, artifact, policy, file_job))

    outcome = asyncio.run(scenario())

    assert outcome.kind == StageOutcomeKind.COMPLETED
    assert outcome.coverage["risk_level"] == "low"
    assert outcome.coverage["manual_review_required"] is False


def test_summary_input_contains_only_normalized_results_and_keeps_full_risk_floor(
    tmp_path: Path,
) -> None:
    async def scenario() -> Any:
        provider = _SchemaRoutingProvider()
        repository, storage, artifact, policy, file_job, _ = await _fixture(
            tmp_path,
            provider_version=provider.version,
        )
        file_outcome = await LlmFileStage(
            FileReviewService(provider, retry_delay_seconds=0),
            provider_config_ref="config:llm-default",
        ).execute(_context(repository, storage, artifact, policy, file_job))
        assert file_outcome.kind == StageOutcomeKind.COMPLETED
        llm_policy = LlmPolicy.model_validate(policy["policy"]["llm"])
        runs = await repository.list_review_runs(artifact["id"])
        findings = await repository.list_findings(artifact["id"])
        budget = artifact_llm_budget(runs, llm_policy, policy["id"])
        return SummaryInputBuilder().build(
            runs,
            findings,
            remaining_tokens=budget.remaining_tokens,
            remaining_cost_microusd=budget.remaining_cost_microusd,
            policy=llm_policy,
        )

    prepared = asyncio.run(scenario())
    payload = prepared.input.canonical_json()

    assert prepared.risk_floor == "medium"
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in payload
    assert "await client.get(url)" not in payload
    assert '"content"' not in payload
    assert '"evidence_excerpt"' not in payload
    assert '"suggestion"' not in payload


def test_summary_service_rejects_decision_fields(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _SchemaRoutingProvider(
            summary_result={**_summary_result(), "decision": "approve"}
        )
        repository, _, artifact, policy, _, _ = await _fixture(
            tmp_path,
            provider_version=provider.version,
        )
        llm_policy = LlmPolicy.model_validate(policy["policy"]["llm"])
        runs = await repository.list_review_runs(artifact["id"])
        findings = await repository.list_findings(artifact["id"])
        prepared = SummaryInputBuilder().build(
            runs,
            findings,
            remaining_tokens=30_000,
            remaining_cost_microusd=500_000,
            policy=llm_policy,
        )
        with pytest.raises(LlmOutputInvalid) as error:
            await SummaryReviewService(provider, retry_delay_seconds=0).evaluate(
                prepared,
                remaining_tokens=30_000,
                remaining_cost_microusd=500_000,
                policy=llm_policy,
            )
        assert error.value.attempts == 1
        assert int(error.value.usage["total_tokens"]) > 0

    asyncio.run(scenario())


def test_file_and_summary_stages_recover_idempotently(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, Any, int, list[dict[str, Any]]]:
        provider = _SchemaRoutingProvider()
        repository, storage, artifact, policy, file_job, summary_job = await _fixture(
            tmp_path,
            provider_version=provider.version,
        )
        file_stage = LlmFileStage(
            FileReviewService(provider, retry_delay_seconds=0),
            provider_config_ref="config:llm-default",
        )
        summary_stage = LlmSummaryStage(
            SummaryReviewService(provider, retry_delay_seconds=0),
            provider_config_ref="config:llm-default",
        )
        first_file = await file_stage.execute(
            _context(repository, storage, artifact, policy, file_job)
        )
        request_count = len(provider.requests)
        recovered_file = await file_stage.execute(
            _context(repository, storage, artifact, policy, file_job)
        )
        assert len(provider.requests) == request_count
        first_summary = await summary_stage.execute(
            _context(repository, storage, artifact, policy, summary_job)
        )
        request_count = len(provider.requests)
        recovered_summary = await summary_stage.execute(
            _context(repository, storage, artifact, policy, summary_job)
        )
        assert first_file.kind == StageOutcomeKind.COMPLETED
        assert first_summary.kind == StageOutcomeKind.COMPLETED
        assert len(provider.requests) == request_count
        return (
            recovered_file,
            recovered_summary,
            request_count,
            await repository.list_review_runs(artifact["id"]),
        )

    recovered_file, recovered_summary, request_count, runs = asyncio.run(scenario())

    assert recovered_file.coverage["recovered"] is True
    assert recovered_summary.coverage["recovered"] is True
    assert request_count == 4
    assert (
        sum(
            run["type"] == "llm_file"
            and (run.get("coverage") or {}).get("stage_name") == "llm_file"
            for run in runs
        )
        == 1
    )
    assert sum(run["type"] == "llm_summary" for run in runs) == 1


def test_file_summary_dag_advances_to_route_without_mutating_findings(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[list[dict[str, Any]], list[Any], Any, Any]:
        provider = _SchemaRoutingProvider()
        repository, storage, artifact, _, _, _ = await _fixture(
            tmp_path,
            provider_version=provider.version,
        )
        runner = ArtifactJobRunner(
            repository=repository,
            storage=storage,
            prechecker=cast(Any, object()),
            scanner=cast(Any, object()),
            worker_id="file-summary-worker",
            lease_seconds=60,
            poll_seconds=1,
            advanced_review_enabled=True,
            llm_provider=provider,
            llm_provider_config_ref="config:llm-default",
            llm_retry_delay_seconds=0,
        )
        try:
            initial = await runner.review_orchestrator.reconcile(artifact["id"])
            assert initial.enqueued_job_ids
            assert await runner.run_once() == 1
            findings_before = await repository.list_findings(artifact["id"])
            assert await runner.run_once() == 1
            findings_after = await repository.list_findings(artifact["id"])
            jobs = await repository.list_artifact_jobs(artifact["id"])
            runs = await repository.list_review_runs(artifact["id"])
            summary = next(run for run in runs if run["type"] == "llm_summary")
            before_projection = [
                (
                    item["fingerprint"],
                    item["severity"],
                    item["source"],
                    item["deterministic"],
                )
                for item in findings_before
            ]
            after_projection = [
                (
                    item["fingerprint"],
                    item["severity"],
                    item["source"],
                    item["deterministic"],
                )
                for item in findings_after
            ]
            return jobs, list(provider.requests), summary, (before_projection, after_projection)
        finally:
            await runner.close()

    jobs, requests, summary, finding_projections = asyncio.run(scenario())

    assert [job["type"] for job in jobs] == ["llm_file", "llm_summary", "route_review"]
    assert [job["status"] for job in jobs] == ["succeeded", "succeeded", "queued"]
    assert [request.schema_name for request in requests].count("astrbot_plugin_file_review") == 3
    assert [request.schema_name for request in requests].count("astrbot_plugin_review_summary") == 1
    assert summary["status"] == "succeeded"
    assert summary["coverage"]["deterministic_risk_floor"] == "medium"
    assert finding_projections[0] == finding_projections[1]
