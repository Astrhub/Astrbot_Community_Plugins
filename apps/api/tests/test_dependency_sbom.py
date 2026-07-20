from __future__ import annotations

import hashlib
import json

import pytest

from app.artifacts.runner_contract import InstalledPackage
from app.artifacts.sbom import (
    SBOM_FORMAT,
    SBOM_GENERATOR,
    SBOM_TOOL_VERSION,
    build_cyclonedx_sbom,
    validate_cyclonedx_sbom,
)


def packages() -> tuple[InstalledPackage, ...]:
    return (
        InstalledPackage(
            name="Demo_Lib",
            version="1.2.3",
            source="index",
            requires=("urllib3",),
        ),
        InstalledPackage(name="urllib3", version="2.2.2", source="index"),
    )


def test_cyclonedx_builder_is_canonical_and_contains_dependency_edges() -> None:
    first = build_cyclonedx_sbom("4.26.5", packages())
    second = build_cyclonedx_sbom("4.26.5", tuple(reversed(packages())))

    assert first == second
    payload = json.loads(first)
    assert payload["bomFormat"] == "CycloneDX"
    assert payload["specVersion"] == "1.5"
    assert payload["metadata"]["tools"]["components"][0] == {
        "name": SBOM_GENERATOR,
        "type": "application",
        "version": SBOM_TOOL_VERSION,
    }
    demo = next(item for item in payload["dependencies"] if "demo-lib" in item["ref"])
    assert demo["dependsOn"] == ["pkg:pypi/urllib3@2.2.2"]

    digest = hashlib.sha256(first).hexdigest()
    validated = validate_cyclonedx_sbom(
        first,
        astrbot_version="4.26.5",
        packages=packages(),
        expected_sha256=digest,
    )
    assert validated.document_sha256 == digest
    assert validated.package_count == 2
    assert validated.format == SBOM_FORMAT


def test_cyclonedx_validator_rejects_hash_or_package_snapshot_drift() -> None:
    content = build_cyclonedx_sbom("4.26.5", packages())
    digest = hashlib.sha256(content).hexdigest()

    with pytest.raises(ValueError, match="sha256"):
        validate_cyclonedx_sbom(
            content,
            astrbot_version="4.26.5",
            packages=packages(),
            expected_sha256="0" * 64,
        )

    changed = (*packages()[:-1], InstalledPackage(name="urllib3", version="1.26.0"))
    with pytest.raises(ValueError, match="does not match"):
        validate_cyclonedx_sbom(
            content,
            astrbot_version="4.26.5",
            packages=changed,
            expected_sha256=digest,
        )
