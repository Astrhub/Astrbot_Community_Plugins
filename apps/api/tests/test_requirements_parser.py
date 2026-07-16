from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from app.artifacts.requirements_parser import (
    RequirementSource,
    RequirementsParseError,
    parse_requirements,
)


def test_parser_normalizes_marker_extras_hashes_and_continuations() -> None:
    digest = "a" * 64
    result = parse_requirements(
        f"Demo_Pkg[HTTP]>=1.2,<2; python_version >= '3.12' \\\n  --hash=sha256:{digest}\n"
    )

    assert result.content_sha256
    assert result.diagnostics == ()
    assert len(result.requirements) == 1
    requirement = result.requirements[0]
    assert requirement.name == "demo-pkg"
    assert requirement.extras == ("http",)
    assert requirement.specifier == "<2,>=1.2"
    assert requirement.marker == 'python_version >= "3.12"'
    assert requirement.hashes == (f"sha256:{digest}",)
    assert requirement.source is RequirementSource.INDEX


@pytest.mark.parametrize(
    ("declaration", "code", "source"),
    [
        (
            "demo @ https://user:token@example.test/demo.whl?access_token=secret",
            "dependency_direct_url",
            RequirementSource.DIRECT_URL,
        ),
        (
            "demo @ git+https://user:token@example.test/repo.git@main",
            "dependency_vcs_source",
            RequirementSource.VCS,
        ),
        ("../private/demo", "dependency_local_source", RequirementSource.LOCAL),
        (
            "-e git+https://example.test/demo.git#egg=demo",
            "dependency_editable_source",
            RequirementSource.EDITABLE,
        ),
        (
            "--index-url https://user:token@example.test/simple",
            "dependency_source_option",
            RequirementSource.OPTION,
        ),
        (
            "-r https://user:token@example.test/requirements.txt",
            "dependency_source_option",
            RequirementSource.OPTION,
        ),
    ],
)
def test_parser_reports_unsafe_sources_without_leaking_values(
    declaration: str,
    code: str,
    source: RequirementSource,
) -> None:
    result = parse_requirements(declaration)

    assert result.diagnostics[0].code == code
    assert result.diagnostics[0].source is source
    serialized = json.dumps(
        [asdict(diagnostic) for diagnostic in result.diagnostics],
        default=str,
    )
    for secret in ("user", "token", "secret", "example.test", "../private"):
        assert secret not in serialized


def test_parser_reports_conflicting_declarations_without_raw_lines() -> None:
    result = parse_requirements("Demo>=1\ndemo<1\n")

    conflict = next(
        item for item in result.diagnostics if item.code == "dependency_declaration_conflict"
    )
    assert conflict.name == "demo"
    assert ">=1" not in conflict.evidence
    assert "<1" not in conflict.evidence


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"demo\x00==1", "requirements_binary"),
        (b"\xff", "requirements_encoding_invalid"),
        ("demo \\", "requirements_continuation_incomplete"),
    ],
)
def test_parser_rejects_invalid_file_boundaries(content: bytes | str, code: str) -> None:
    with pytest.raises(RequirementsParseError) as captured:
        parse_requirements(content)

    assert captured.value.code == code
