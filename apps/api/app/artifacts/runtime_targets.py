from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from .models import ArtifactErrorCode
from .policy import ReviewPolicyV1
from .runner_contract import RuntimeTarget as ContractRuntimeTarget


@dataclass(frozen=True, slots=True)
class RuntimeImage:
    astrbot_version: str
    python_version: str
    image_digest: str
    platform: str = "linux/amd64"
    astrbot_commit: str = ""

    def contract_target(self) -> ContractRuntimeTarget:
        try:
            return ContractRuntimeTarget.model_validate(
                {
                    "astrbot_version": self.astrbot_version,
                    "python_version": self.python_version,
                    "image_digest": self.image_digest,
                    "platform": self.platform,
                    "astrbot_commit": self.astrbot_commit,
                }
            )
        except ValidationError as exc:
            raise ValueError(
                "runtime image target requires exact versions and a pinned sha256 digest"
            ) from exc


@dataclass(frozen=True, slots=True)
class RuntimeTargetFinding:
    rule_id: str
    severity: str
    category: str
    message: str
    evidence_excerpt: str
    deterministic: bool = True

    def as_repository_payload(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "evidence_excerpt": self.evidence_excerpt,
            "deterministic": self.deterministic,
            "source": "runtime",
        }


@dataclass(frozen=True, slots=True)
class RuntimeTargetResolution:
    plugin_version: str
    plugin_normalized_version: str
    metadata_requirement: str
    targets: tuple[ContractRuntimeTarget, ...]
    finding: RuntimeTargetFinding | None = None
    error_code: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.error_code)


class RuntimeTargetResolver:
    def __init__(self, images: Sequence[RuntimeImage]) -> None:
        catalog: dict[tuple[str, str], ContractRuntimeTarget] = {}
        for image in images:
            target = image.contract_target()
            key = (target.astrbot_version, target.python_version)
            if key in catalog:
                raise ValueError("duplicate_runtime_image_target")
            catalog[key] = target
        self._catalog = catalog

    def resolve(
        self,
        policy: ReviewPolicyV1,
        metadata: Mapping[str, object],
        *,
        plugin_version: str,
        plugin_normalized_version: str,
    ) -> RuntimeTargetResolution:
        if not plugin_version or not plugin_normalized_version:
            raise ValueError("plugin_version_snapshot_missing")
        requirement_text = str(metadata.get("astrbot_version") or "").strip()
        try:
            requirement = _parse_metadata_requirement(requirement_text)
        except ValueError:
            return _incompatible_resolution(
                plugin_version,
                plugin_normalized_version,
                requirement_text,
                "metadata astrbot_version is not a valid bounded version specifier",
            )

        selected: list[ContractRuntimeTarget] = []
        unavailable: list[str] = []
        policy_versions: list[str] = []
        for policy_target in policy.runtime_targets:
            policy_versions.append(policy_target.astrbot)
            if requirement is not None and not requirement.contains(
                Version(policy_target.astrbot),
                prereleases=False,
            ):
                continue
            image = self._catalog.get((policy_target.astrbot, policy_target.python))
            if image is None:
                unavailable.append(
                    f"AstrBot {policy_target.astrbot} / Python {policy_target.python}"
                )
                continue
            selected.append(image)

        if selected:
            return RuntimeTargetResolution(
                plugin_version=plugin_version,
                plugin_normalized_version=plugin_normalized_version,
                metadata_requirement=requirement_text,
                targets=tuple(selected),
            )
        if unavailable:
            return RuntimeTargetResolution(
                plugin_version=plugin_version,
                plugin_normalized_version=plugin_normalized_version,
                metadata_requirement=requirement_text,
                targets=(),
                error_code=ArtifactErrorCode.RUNTIME_RUNNER_UNAVAILABLE.value,
                finding=RuntimeTargetFinding(
                    rule_id="runtime_target_image_unavailable",
                    severity="high",
                    category="runtime_configuration",
                    message="No pinned runner image is available for the selected runtime target",
                    evidence_excerpt=", ".join(unavailable)[:500],
                    deterministic=False,
                ),
            )
        constraint = requirement_text or "not declared"
        policy_evidence = ", ".join(policy_versions) or "no policy targets"
        return _incompatible_resolution(
            plugin_version,
            plugin_normalized_version,
            requirement_text,
            f"metadata requires {constraint}; policy allows {policy_evidence}",
        )


def _parse_metadata_requirement(value: str) -> SpecifierSet | None:
    if not value:
        return None
    if len(value) > 128 or value.lower() == "latest" or "===" in value:
        raise ValueError("invalid_astrbot_version_requirement")
    candidate = value
    try:
        exact = Version(value)
    except InvalidVersion:
        pass
    else:
        if exact.is_prerelease or exact.is_devrelease or exact.local is not None:
            raise ValueError("invalid_astrbot_version_requirement")
        candidate = f"=={exact}"
    try:
        requirement = SpecifierSet(candidate)
    except InvalidSpecifier as exc:
        raise ValueError("invalid_astrbot_version_requirement") from exc
    return requirement


def _incompatible_resolution(
    plugin_version: str,
    plugin_normalized_version: str,
    requirement: str,
    evidence: str,
) -> RuntimeTargetResolution:
    return RuntimeTargetResolution(
        plugin_version=plugin_version,
        plugin_normalized_version=plugin_normalized_version,
        metadata_requirement=requirement,
        targets=(),
        error_code=ArtifactErrorCode.ASTRBOT_VERSION_INCOMPATIBLE.value,
        finding=RuntimeTargetFinding(
            rule_id=ArtifactErrorCode.ASTRBOT_VERSION_INCOMPATIBLE.value,
            severity="critical",
            category="compatibility",
            message="Plugin AstrBot version requirement does not match an allowed runtime target",
            evidence_excerpt=evidence[:500],
        ),
    )
