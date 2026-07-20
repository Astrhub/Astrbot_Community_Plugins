from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from packaging.utils import canonicalize_name

from .runner_contract import InstalledPackage

SBOM_FORMAT = "cyclonedx-json"
SBOM_GENERATOR = "astrbot-runtime-install"
SBOM_TOOL_VERSION = "cyclonedx-canonical-v1"
MAX_SBOM_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ValidatedSbom:
    document_sha256: str
    package_count: int
    generator: str = SBOM_GENERATOR
    tool_version: str = SBOM_TOOL_VERSION
    format: str = SBOM_FORMAT


def build_cyclonedx_sbom(
    astrbot_version: str,
    packages: Sequence[InstalledPackage],
) -> bytes:
    normalized = sorted(
        packages,
        key=lambda item: (canonicalize_name(item.name), item.version, item.source),
    )
    references = {
        canonicalize_name(item.name): _package_purl(item.name, item.version) for item in normalized
    }
    components = [
        {
            "bom-ref": references[canonicalize_name(package.name)],
            "type": "library",
            "name": package.name,
            "version": package.version,
            "purl": references[canonicalize_name(package.name)],
            "properties": [{"name": "astrbot:source", "value": package.source}],
        }
        for package in normalized
    ]
    dependencies = [
        {
            "ref": references[canonicalize_name(package.name)],
            "dependsOn": sorted(
                {
                    references[name]
                    for name in package.requires
                    if name in references and name != canonicalize_name(package.name)
                }
            ),
        }
        for package in normalized
    ]
    identity = json.dumps(
        {"components": components, "dependencies": dependencies},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    serial = uuid.UUID(hashlib.sha256(identity.encode()).hexdigest()[:32])
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": "AstrBot", "version": astrbot_version},
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": SBOM_GENERATOR,
                        "version": SBOM_TOOL_VERSION,
                    }
                ]
            },
        },
        "components": components,
        "dependencies": dependencies,
    }
    content = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if len(content) > MAX_SBOM_BYTES:
        raise ValueError("CycloneDX SBOM exceeds the private object limit")
    return content


def validate_cyclonedx_sbom(
    content: bytes,
    *,
    astrbot_version: str,
    packages: Sequence[InstalledPackage],
    expected_sha256: str,
) -> ValidatedSbom:
    if not content or len(content) > MAX_SBOM_BYTES:
        raise ValueError("CycloneDX SBOM size is invalid")
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha256:
        raise ValueError("CycloneDX SBOM sha256 is invalid")
    expected = build_cyclonedx_sbom(astrbot_version, packages)
    if content != expected:
        raise ValueError("CycloneDX SBOM does not match the signed package snapshot")
    return ValidatedSbom(document_sha256=digest, package_count=len(packages))


def _package_purl(name: str, version: str) -> str:
    return f"pkg:pypi/{quote(canonicalize_name(name), safe='-')}@{quote(version, safe='._+-')}"


__all__ = [
    "MAX_SBOM_BYTES",
    "SBOM_FORMAT",
    "SBOM_GENERATOR",
    "SBOM_TOOL_VERSION",
    "ValidatedSbom",
    "build_cyclonedx_sbom",
    "validate_cyclonedx_sbom",
]
