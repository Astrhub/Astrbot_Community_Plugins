from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[3]


def test_plugin_review_operations_links_and_security_contract_are_current() -> None:
    operations_path = ROOT / "docs/plugin-review-operations.md"
    operations = operations_path.read_text(encoding="utf-8")
    runtime = (ROOT / "docs/runtime-runner.md").read_text(encoding="utf-8")
    security = (ROOT / "docs/security.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for target in re.findall(r"\[[^]]+\]\(([^)]+\.md)\)", operations):
        assert (operations_path.parent / target).resolve().is_file(), target

    assert "4.26.6" in operations
    assert "5d10e0d428b41308cc63215db00359c61ee17195" in operations
    assert "RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT=false" in operations
    assert "at-least-once" in operations
    assert "不能证明插件绝对安全" in operations
    assert "源码、requirements、comment、diff、evidence、日志、对象 key" in operations
    assert "plugin-review-operations.md" in readme
    assert "/run/user/10001/docker.sock" in runtime
    assert "Compose 默认使用\n  rootless" in security
    assert "policy activate/retire/rollback" in architecture


def test_documented_deployment_files_exist() -> None:
    expected = (
        "deploy/compose/api.env.example",
        "deploy/compose/artifact-worker.env.example",
        "deploy/systemd/astrbot-community-plugins-api.env.example",
        "deploy/systemd/astrbot-artifact-worker.env.example",
        "deploy/systemd/astrbot-runtime-runner.env.example",
        "deploy/systemd/astrbot-community-plugins.service",
        "deploy/systemd/astrbot-artifact-worker.service",
        "deploy/systemd/astrbot-runtime-runner.service",
    )

    assert all((ROOT / path).is_file() for path in expected)
    assert not (ROOT / "deploy/systemd/astrbot-community-plugins.env.example").exists()


def _markdown_headings(path: Path) -> set[str]:
    headings: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#+\s+(.+?)\s*$", line)
        if not match:
            continue
        title = re.sub(r"[`*_~]", "", match.group(1)).strip().lower()
        slug = re.sub(r"\s+", "-", title)
        headings.add(slug)
    return headings


def test_security_policy_and_security_docs_have_valid_links() -> None:
    policy_path = ROOT / "SECURITY.md"
    docs_security_path = ROOT / "docs/security.md"
    assert policy_path.is_file()

    policy = policy_path.read_text(encoding="utf-8")
    assert "https://github.com/Astrhub/Astrbot_Community_Plugins/security/advisories/new" in policy
    assert "mailto:plugins@eloina.cn" in policy
    assert "(docs/security.md)" in policy

    for source_path in (policy_path, docs_security_path):
        content = source_path.read_text(encoding="utf-8")
        for href in re.findall(r"\[[^]]+\]\(([^)]+)\)", content):
            if href.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_part, _, anchor = href.partition("#")
            target_file = (source_path.parent / target_part).resolve()
            assert target_file.is_file(), f"Broken link in {source_path.name}: {href}"
            if anchor:
                assert anchor.lower() in _markdown_headings(target_file), (
                    f"Broken anchor in {source_path.name}: {href}"
                )
