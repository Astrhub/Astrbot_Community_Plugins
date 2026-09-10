from app.artifacts.policy import parse_review_policy
from app.artifacts.runtime import _finalize_review_status
from app.config import load_settings


def test_api_uses_live_worker_health_without_copying_scanner_endpoints(tmp_path):
    config = load_settings(
        {
            "APP_ENV_FILE": str(tmp_path / "absent.env"),
            "ARTIFACT_ADVANCED_REVIEW_ENABLED": "true",
            "ARTIFACT_CLAMAV_ENABLED": "true",
        }
    ).artifacts
    policy = parse_review_policy(
        {
            "schema_version": "1",
            "required_stages": ["static", "clamav"],
            "runtime_targets": [],
            "limits": {"cpu": 1, "memory_mb": 768, "pids": 128, "timeout_seconds": 120},
            "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
            "llm": {"enabled": False},
            "malware": {"clamav": True},
            "dependency": {"enabled": False},
            "routing": {"auto_approve": False},
        }
    )

    def status(health, remote):
        return _finalize_review_status(
            config.review.public_status(),
            policy={"enabled": True, "ready": True},
            policy_model=policy,
            tool_health=health,
            config=config,
            remote_tools=remote,
        )

    assert status({"clamav": {"ready": True}}, True)["ready"] is True
    assert status({}, True)["ready"] is False
    assert status({"clamav": {"ready": False}}, True)["ready"] is False
    assert status({"clamav": {"ready": True}}, False)["ready"] is False
