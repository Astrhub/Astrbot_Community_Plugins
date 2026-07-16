from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from app.artifacts.policy import (
    POLICY_SCHEMA_VERSION,
    ReviewPolicyStage,
    ReviewPolicyV1,
    canonical_policy_json,
    review_policy_json_schema,
    review_policy_sha256,
)


def policy_payload() -> dict:
    return {
        "schema_version": "1",
        "required_stages": ["static", "runtime", "dependency"],
        "runtime_targets": [
            {"astrbot": "4.26.5", "python": "3.12"},
            {"astrbot": "4.27.0", "python": "3.12"},
        ],
        "limits": {
            "cpu": 1,
            "memory_mb": 768,
            "pids": 128,
            "timeout_seconds": 120,
        },
        "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
        "llm": {
            "enabled": True,
            "model": "configured-model",
            "max_tokens": 24000,
            "max_cost_microusd": 100000,
            "input_cost_microusd_per_million_tokens": 1000000,
            "output_cost_microusd_per_million_tokens": 4000000,
        },
        "malware": {"clamav": True, "yara_ruleset": "market-v1"},
        "dependency": {"max_severity": "high", "max_data_age_hours": 24},
        "category": {
            "enabled": True,
            "model": "configured-category-model",
            "minimum_confidence": 0.8,
        },
        "routing": {"auto_approve": False, "manual_review_at": "low"},
    }


def test_review_policy_matches_versioned_design_and_is_immutable() -> None:
    policy = ReviewPolicyV1.model_validate(policy_payload())

    assert policy.schema_version == POLICY_SCHEMA_VERSION
    assert policy.required_stages == (
        ReviewPolicyStage.STATIC,
        ReviewPolicyStage.RUNTIME,
        ReviewPolicyStage.DEPENDENCY,
    )
    assert policy.runtime_targets[0].astrbot == "4.26.5"
    assert policy.network_profiles.smoke == "none"
    assert policy.llm.provider_config_ref == "config:llm-default"
    assert policy.category.default_category.value == "other"
    assert policy.category.provider_config_ref == "config:llm-default"
    assert policy.category.prompt_version == "category-prompt-v1"
    assert policy.category.max_output_tokens == 512

    with pytest.raises(ValidationError):
        policy.routing.auto_approve = True


def test_review_policy_json_schema_is_strict_and_versioned() -> None:
    schema = review_policy_json_schema()

    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "1"
    assert "schema_version" in schema["required"]
    assert schema["$defs"]["LlmPolicy"]["additionalProperties"] is False
    assert schema["$defs"]["RuntimeTarget"]["additionalProperties"] is False
    assert "api_key" not in json.dumps(schema).lower()


def test_review_policy_requires_an_explicit_supported_schema_version() -> None:
    missing = policy_payload()
    missing.pop("schema_version")
    with pytest.raises(ValidationError, match="Field required"):
        ReviewPolicyV1.model_validate(missing)

    unsupported = policy_payload()
    unsupported["schema_version"] = "2"
    with pytest.raises(ValidationError, match="Input should be '1'"):
        ReviewPolicyV1.model_validate(unsupported)


@pytest.mark.parametrize(
    "version",
    ["latest", ">=4.26.5", "4.26.*", "main", "4.26.5+local"],
)
def test_review_policy_rejects_non_exact_astrbot_versions(version: str) -> None:
    payload = policy_payload()
    payload["runtime_targets"] = [{"astrbot": version, "python": "3.12"}]

    with pytest.raises(ValidationError):
        ReviewPolicyV1.model_validate(payload)


@pytest.mark.parametrize("version", ["latest", ">=3.12", "3.12.*", "3.12rc1"])
def test_review_policy_rejects_non_exact_python_versions(version: str) -> None:
    payload = policy_payload()
    payload["runtime_targets"] = [{"astrbot": "4.26.5", "python": version}]

    with pytest.raises(ValidationError):
        ReviewPolicyV1.model_validate(payload)


def test_review_policy_rejects_duplicate_stages_and_targets() -> None:
    duplicate_stage = policy_payload()
    duplicate_stage["required_stages"].append("static")
    with pytest.raises(ValidationError, match="required_stages cannot contain duplicates"):
        ReviewPolicyV1.model_validate(duplicate_stage)

    duplicate_target = policy_payload()
    duplicate_target["runtime_targets"].append({"astrbot": "4.26.5", "python": "3.12.0"})
    with pytest.raises(ValidationError, match="duplicate version pairs"):
        ReviewPolicyV1.model_validate(duplicate_target)


@pytest.mark.parametrize(
    ("stages", "update", "message"),
    [
        (["runtime"], {}, "static must be a required stage"),
        (["static", "runtime"], {"runtime_targets": []}, "exact runtime target"),
        (["static", "category"], {"category": {"enabled": False}}, "must be enabled"),
        (["static", "clamav"], {"malware": {"clamav": False}}, "must be enabled"),
        (["static", "yara"], {"malware": {"clamav": True}}, "needs a ruleset"),
        (
            ["static", "dependency"],
            {"dependency": {"enabled": False}},
            "must be enabled",
        ),
        (["static", "llm_file"], {}, "needs llm_package"),
        (["static", "llm_summary", "llm_package"], {}, "needs llm_package and llm_file"),
        (["static", "import_graph"], {}, "needs diff"),
    ],
)
def test_review_policy_validates_stage_dependencies(
    stages: list[str],
    update: dict,
    message: str,
) -> None:
    payload = policy_payload()
    payload["required_stages"] = stages
    payload.update(copy.deepcopy(update))

    with pytest.raises(ValidationError, match=message):
        ReviewPolicyV1.model_validate(payload)


def test_review_policy_auto_approve_requires_every_enabled_gate() -> None:
    payload = policy_payload()
    payload["routing"]["auto_approve"] = True

    with pytest.raises(ValidationError, match="enabled review gates"):
        ReviewPolicyV1.model_validate(payload)

    payload["required_stages"] = [
        "static",
        "runtime",
        "clamav",
        "yara",
        "dependency",
        "llm_package",
        "llm_file",
        "llm_summary",
    ]
    policy = ReviewPolicyV1.model_validate(payload)
    assert policy.routing.auto_approve is True


def test_review_policy_auto_approve_requires_runtime_and_complete_coverage() -> None:
    payload = policy_payload()
    payload["routing"]["auto_approve"] = True
    payload["llm"] = {"enabled": False}
    payload["malware"] = {"clamav": False}
    payload["dependency"] = {"enabled": False}
    payload["required_stages"] = ["static"]

    with pytest.raises(ValidationError, match="requires runtime"):
        ReviewPolicyV1.model_validate(payload)

    payload["required_stages"] = ["static", "runtime"]
    payload["routing"]["require_complete_coverage"] = False
    with pytest.raises(ValidationError, match="complete review coverage"):
        ReviewPolicyV1.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("llm", "provider_config_ref", "sk-secret-value"),
        ("llm", "provider_config_ref", "https://provider.invalid/key"),
        ("malware", "clamav_config_ref", "token=secret"),
        ("dependency", "advisory_config_ref", "secret"),
        ("category", "provider_config_ref", "env://CATEGORY_KEY"),
    ],
)
def test_review_policy_only_accepts_secret_config_references(
    section: str,
    field: str,
    value: str,
) -> None:
    payload = policy_payload()
    payload[section][field] = value

    with pytest.raises(ValidationError, match="config, env, or secret reference"):
        ReviewPolicyV1.model_validate(payload)


def test_review_policy_rejects_unknown_secret_fields() -> None:
    payload = policy_payload()
    payload["llm"]["api_key"] = "not-allowed"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReviewPolicyV1.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "updates", "message"),
    [
        ("limits", {"tmpfs_mb": 1024, "memory_mb": 768}, "cannot exceed"),
        ("llm", {"enabled": True, "model": "", "max_tokens": 10}, "requires a model"),
        ("llm", {"max_cost_microusd": 0}, "positive cost budget"),
        (
            "llm",
            {"input_cost_microusd_per_million_tokens": 0},
            "versioned token pricing",
        ),
        ("llm", {"enabled": False, "model": "configured", "max_tokens": 0}, "disabled LLM"),
        ("routing", {"manual_review_at": "high"}, "cannot be higher than medium"),
        ("routing", {"deterministic_reject_at": "medium"}, "cannot be lower than high"),
    ],
)
def test_review_policy_validates_cross_field_thresholds(
    section: str,
    updates: dict,
    message: str,
) -> None:
    payload = policy_payload()
    payload[section].update(updates)

    with pytest.raises(ValidationError, match=message):
        ReviewPolicyV1.model_validate(payload)


def test_review_policy_canonical_json_and_hash_ignore_set_order() -> None:
    first = policy_payload()
    second = copy.deepcopy(first)
    second["required_stages"] = list(reversed(second["required_stages"]))
    second["runtime_targets"] = list(reversed(second["runtime_targets"]))
    second["category"]["allowed_categories"] = [
        "other",
        "utilities",
        "productivity",
        "integrations",
        "entertainment",
        "ai_tools",
    ]

    canonical = canonical_policy_json(first)
    assert canonical == canonical_policy_json(second)
    assert review_policy_sha256(first) == review_policy_sha256(second)
    assert len(review_policy_sha256(first)) == 64
    assert " " not in canonical
