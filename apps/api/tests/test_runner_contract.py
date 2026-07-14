from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.artifacts.runner_contract import (
    MAX_RUNTIME_REQUEST_BYTES,
    MAX_RUNTIME_RESULT_BYTES,
    RuntimeDispatchRequest,
    RuntimeDispatchResult,
    build_runtime_dispatch_result,
    canonical_contract_json,
    runtime_result_object_key,
    runtime_result_sha256,
)


def request_payload() -> dict:
    return {
        "schema_version": "1",
        "dispatch_id": "dispatch_01",
        "artifact_id": "artifact_01",
        "artifact_sha256": "a" * 64,
        "artifact_size_bytes": 4096,
        "quarantine_key": "quarantine/artifact_01/source.zip",
        "policy_version_id": "policy_01",
        "expected_plugin": {
            "name": "astrbot_plugin_demo",
            "version": "v1.2.3",
            "source_repo": "https://github.com/alice/astrbot_plugin_demo",
            "source_commit_sha": "b" * 40,
        },
        "target": {
            "astrbot_version": "4.26.5",
            "python_version": "3.12",
            "image_digest": f"sha256:{'c' * 64}",
            "platform": "linux/amd64",
            "astrbot_commit": "adebd2958ed8",
        },
        "limits": {
            "cpu": 1,
            "memory_mb": 768,
            "pids": 128,
            "timeout_seconds": 120,
            "disk_mb": 2048,
            "tmpfs_mb": 256,
            "max_log_bytes": 1_048_576,
            "max_result_bytes": 524_288,
        },
        "install_network_profile": "pypi-only-v1",
        "smoke_network_profile": "none",
        "result_key": "runtime/results/dispatch_01",
    }


def passed_probe(duration_ms: int = 1) -> dict:
    return {"status": "passed", "duration_ms": duration_ms}


def result_payload() -> dict:
    return {
        "schema_version": "1",
        "dispatch_id": "dispatch_01",
        "artifact_sha256": "a" * 64,
        "target": {
            "astrbot_version": "4.26.5",
            "python_version": "3.12",
            "resolved_python_version": "3.12.10",
            "image_digest": f"sha256:{'c' * 64}",
            "platform": "linux/amd64",
            "astrbot_commit": "adebd2958ed8",
        },
        "install": {
            **passed_probe(1200),
            "astrbot_version": "4.26.5",
            "pip_check": passed_probe(20),
            "packages": [
                {"name": "astrbot", "version": "4.26.5", "source": "index"},
                {"name": "demo-lib", "version": "1.0.0", "source": "index"},
            ],
            "conflicts": [],
            "core_before_sha256": "d" * 64,
            "core_after_sha256": "d" * 64,
            "sbom_key": "runtime/results/dispatch_01/sbom.json",
            "sbom_sha256": "e" * 64,
        },
        "smoke": {
            "status": "passed",
            "duration_ms": 3200,
            "metadata": {
                **passed_probe(5),
                "name": "astrbot_plugin_demo",
                "version": "v1.2.3",
                "author": "Alice",
            },
            "import_probe": passed_probe(80),
            "instance": passed_probe(10),
            "initialize": passed_probe(40),
            "startup": {**passed_probe(3000), "ready_ms": 2900},
            "handlers": {**passed_probe(2), "count": 1, "names": ["hello"]},
            "hooks": {**passed_probe(2), "count": 0, "names": []},
            "llm_tools": {**passed_probe(2), "count": 0, "names": []},
            "failed_plugin": {"present": False},
            "termination": passed_probe(30),
            "violations": [],
        },
        "network_attestation": {
            "status": "passed",
            "backend": "rootless-docker-v1",
            "install_profile": "pypi-only-v1",
            "smoke_profile": "none",
            "install_egress_enforced": True,
            "private_network_blocked": True,
            "metadata_endpoint_blocked": True,
            "smoke_network_disabled": True,
            "violations": [],
        },
        "cleanup": {
            **passed_probe(100),
            "removed_containers": 2,
            "removed_volumes": 1,
            "removed_networks": 1,
            "removed_temp_roots": 1,
            "leaked_resources": [],
        },
        "logs_key": "runtime/results/dispatch_01/runtime.log",
        "logs_sha256": "f" * 64,
    }


def test_request_is_strict_bounded_and_has_stable_canonical_hash() -> None:
    payload = request_payload()
    request = RuntimeDispatchRequest.model_validate(payload)
    reordered = RuntimeDispatchRequest.model_validate(dict(reversed(list(payload.items()))))

    assert request.canonical_sha256() == reordered.canonical_sha256()
    assert len(request.canonical_sha256()) == 64
    assert len(canonical_contract_json(request).encode()) < MAX_RUNTIME_REQUEST_BYTES
    assert RuntimeDispatchRequest.model_validate_json(request.model_dump_json()) == request


def test_result_object_keys_are_unique_per_attempt_and_content() -> None:
    request = RuntimeDispatchRequest.model_validate(request_payload())

    first = runtime_result_object_key(request, 1, "a" * 64)
    retry = runtime_result_object_key(request, 2, "a" * 64)
    changed = runtime_result_object_key(request, 1, "b" * 64)

    assert len({first, retry, changed}) == 3
    assert first.startswith(f"{request.result_key}/attempt-1-")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("target", "astrbot_version"), "latest"),
        (("target", "python_version"), ">=3.12"),
        (("target", "image_digest"), "astrbot:latest"),
        (("quarantine_key",), "../source.zip"),
        (("quarantine_key",), "quarantine/artifact 01/source.zip"),
        (("result_key",), "https://storage.example.test/result.json"),
        (("expected_plugin", "source_repo"), "https://user:pass@github.com/a/b"),
        (("expected_plugin", "source_repo"), "https://github.com/alice/%2e%2e"),
    ],
)
def test_request_rejects_ambiguous_targets_and_unsafe_references(
    path: tuple[str, ...],
    value: str,
) -> None:
    payload = request_payload()
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        RuntimeDispatchRequest.model_validate(payload)


def test_request_rejects_unknown_fields_and_credential_material() -> None:
    unknown = request_payload()
    unknown["database_url"] = "postgresql://market.example/db"
    with pytest.raises(ValidationError, match="Extra inputs"):
        RuntimeDispatchRequest.model_validate(unknown)

    secret = request_payload()
    secret["expected_plugin"]["version"] = "Bearer abcdefghijklmnopqrstuvwxyz"
    with pytest.raises(ValidationError, match="credential material"):
        RuntimeDispatchRequest.model_validate(secret)


def test_result_hash_roundtrip_detects_any_mutation() -> None:
    result = build_runtime_dispatch_result(result_payload())
    serialized = result.model_dump(mode="json")

    assert result.result_sha256 == runtime_result_sha256(result)
    assert len(canonical_contract_json(result).encode()) < MAX_RUNTIME_RESULT_BYTES
    assert RuntimeDispatchResult.model_validate(serialized) == result

    serialized["cleanup"]["removed_temp_roots"] = 2
    with pytest.raises(ValidationError, match="does not match"):
        RuntimeDispatchResult.model_validate(serialized)


def test_result_enforces_lifecycle_consistency_and_paired_object_hashes() -> None:
    invalid_smoke = result_payload()
    invalid_smoke["smoke"]["initialize"] = {
        "status": "failed",
        "error_code": "initialize_error",
        "message": "initialize failed",
    }
    with pytest.raises(ValidationError, match="every lifecycle probe"):
        build_runtime_dispatch_result(invalid_smoke)

    unpaired_log = result_payload()
    unpaired_log.pop("logs_sha256")
    with pytest.raises(ValidationError, match="Log key and sha256"):
        build_runtime_dispatch_result(unpaired_log)

    unpaired_sbom = result_payload()
    unpaired_sbom["install"].pop("sbom_sha256")
    with pytest.raises(ValidationError, match="SBOM key and sha256"):
        build_runtime_dispatch_result(unpaired_sbom)


def test_result_rejects_credentials_even_inside_bounded_error_text() -> None:
    payload = result_payload()
    payload["cleanup"] = {
        "status": "failed",
        "duration_ms": 100,
        "error_code": "cleanup_failed",
        "message": "runner returned Bearer abcdefghijklmnopqrstuvwxyz",
        "leaked_resources": ["container-1"],
    }

    with pytest.raises(ValidationError, match="credential material"):
        build_runtime_dispatch_result(payload)


def test_result_total_size_limit_applies_after_field_limits() -> None:
    payload = result_payload()
    names = [f"handler_{index:04d}_{'x' * 140}" for index in range(4000)]
    payload["smoke"]["handlers"] = {
        **passed_probe(2),
        "count": len(names),
        "names": names,
    }
    payload["smoke"]["llm_tools"] = {
        **passed_probe(2),
        "count": len(names),
        "names": [name.replace("handler", "tool") for name in names],
    }

    with pytest.raises(ValidationError, match=str(MAX_RUNTIME_RESULT_BYTES)):
        build_runtime_dispatch_result(payload)


def test_contract_schema_has_no_credential_or_connection_fields() -> None:
    schema = json.dumps(
        {
            "request": RuntimeDispatchRequest.model_json_schema(),
            "result": RuntimeDispatchResult.model_json_schema(),
        },
        sort_keys=True,
    ).lower()

    for forbidden in ("api_key", "password", "database_url", "access_token", "secret_key"):
        assert forbidden not in schema
