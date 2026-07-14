from __future__ import annotations

import pytest

from app.artifacts.models import ArtifactErrorCode
from app.artifacts.policy import ReviewPolicyV1
from app.artifacts.runtime_targets import RuntimeImage, RuntimeTargetResolver


def policy(*targets: tuple[str, str]) -> ReviewPolicyV1:
    return ReviewPolicyV1.model_validate(
        {
            "schema_version": "1",
            "required_stages": ["static", "runtime"],
            "runtime_targets": [
                {"astrbot": astrbot, "python": python} for astrbot, python in targets
            ],
            "limits": {
                "cpu": 1,
                "memory_mb": 768,
                "pids": 128,
                "timeout_seconds": 120,
                "disk_mb": 2048,
                "tmpfs_mb": 256,
                "max_log_bytes": 1_048_576,
            },
            "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
            "llm": {"enabled": False},
            "malware": {},
            "dependency": {"enabled": True},
            "routing": {"auto_approve": False},
        }
    )


def image(
    astrbot: str,
    python: str,
    marker: str,
    *,
    commit: str = "",
) -> RuntimeImage:
    return RuntimeImage(
        astrbot_version=astrbot,
        python_version=python,
        image_digest=f"sha256:{marker * 64}",
        astrbot_commit=commit,
    )


def resolve(
    resolver: RuntimeTargetResolver,
    value: object,
    review_policy: ReviewPolicyV1,
):
    return resolver.resolve(
        review_policy,
        {"astrbot_version": value},
        plugin_version="v9.8.7",
        plugin_normalized_version="9.8.7",
    )


def test_resolver_intersects_metadata_with_finite_policy_matrix() -> None:
    review_policy = policy(("4.25.0", "3.12"), ("4.26.5", "3.12"), ("5.0.0", "3.13"))
    resolver = RuntimeTargetResolver(
        [
            image("4.25.0", "3.12", "a"),
            image("4.26.5", "3.12", "b", commit="adebd2958ed8"),
            image("5.0.0", "3.13", "c"),
        ]
    )

    result = resolve(resolver, ">=4.26,<5", review_policy)

    assert not result.blocked
    assert result.plugin_version == "v9.8.7"
    assert result.plugin_normalized_version == "9.8.7"
    assert [target.astrbot_version for target in result.targets] == ["4.26.5"]
    assert result.targets[0].astrbot_commit == "adebd2958ed8"


@pytest.mark.parametrize("requirement", ["4.26.5", "==4.26.5", ">=4.26.5,!=5.*"])
def test_resolver_accepts_exact_and_bounded_metadata_requirements(requirement: str) -> None:
    review_policy = policy(("4.26.5", "3.12"))
    result = resolve(
        RuntimeTargetResolver([image("4.26.5", "3.12", "a")]),
        requirement,
        review_policy,
    )
    assert [target.astrbot_version for target in result.targets] == ["4.26.5"]


@pytest.mark.parametrize("requirement", ["latest", "^4.26", "===4.26.5", "not-a-version"])
def test_invalid_metadata_requirement_is_a_deterministic_blocker(requirement: str) -> None:
    result = resolve(
        RuntimeTargetResolver([image("4.26.5", "3.12", "a")]),
        requirement,
        policy(("4.26.5", "3.12")),
    )

    assert result.blocked
    assert result.error_code == ArtifactErrorCode.ASTRBOT_VERSION_INCOMPATIBLE.value
    assert result.finding and result.finding.deterministic
    assert result.finding.severity == "critical"


def test_empty_intersection_is_blocking_and_never_uses_plugin_version_as_target() -> None:
    result = resolve(
        RuntimeTargetResolver([image("4.26.5", "3.12", "a")]),
        ">=5",
        policy(("4.26.5", "3.12")),
    )

    assert result.blocked
    assert result.plugin_normalized_version == "9.8.7"
    assert result.targets == ()
    assert "9.8.7" not in result.finding.evidence_excerpt


def test_missing_pinned_image_is_fail_visible_not_a_compatibility_claim() -> None:
    result = resolve(
        RuntimeTargetResolver([]),
        ">=4",
        policy(("4.26.5", "3.12")),
    )

    assert result.blocked
    assert result.error_code == ArtifactErrorCode.RUNTIME_RUNNER_UNAVAILABLE.value
    assert result.finding and not result.finding.deterministic
    assert "AstrBot 4.26.5 / Python 3.12" in result.finding.evidence_excerpt


def test_empty_metadata_selects_all_policy_targets_with_images() -> None:
    review_policy = policy(("4.25.0", "3.12"), ("4.26.5", "3.12"))
    result = resolve(
        RuntimeTargetResolver([image("4.25.0", "3.12", "a"), image("4.26.5", "3.12", "b")]),
        "",
        review_policy,
    )
    assert [target.astrbot_version for target in result.targets] == ["4.25.0", "4.26.5"]


def test_runtime_image_catalog_rejects_duplicates_and_unpinned_images() -> None:
    duplicate = image("4.26.5", "3.12", "a")
    with pytest.raises(ValueError, match="duplicate"):
        RuntimeTargetResolver([duplicate, duplicate])
    with pytest.raises(ValueError, match="pinned sha256"):
        RuntimeTargetResolver(
            [
                RuntimeImage(
                    astrbot_version="4.26.5",
                    python_version="3.12",
                    image_digest="astrbot:latest",
                )
            ]
        )
