from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

REQUIREMENTS_PARSER_VERSION = "requirements-parser-v1"
MAX_REQUIREMENTS_BYTES = 256 * 1024
MAX_REQUIREMENT_LINES = 2000
MAX_REQUIREMENT_HASHES = 32

_HASH_OPTION = re.compile(
    r"(?<!\S)--hash(?:=|\s+)(?P<algorithm>[A-Za-z0-9]+):(?P<digest>[A-Fa-f0-9]+)(?=\s|$)"
)
_VCS_SCHEMES = ("git+", "hg+", "svn+", "bzr+")
_URL_SCHEMES = ("http://", "https://")
_LOCAL_PREFIXES = ("file:", "./", "../", "/", "~/")
_SECRET_VALUE = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@|"
    r"[?&](?:access_token|api[_-]?key|key|password|secret|signature|token)=[^&#\s]+)",
    re.IGNORECASE,
)


class RequirementSource(StrEnum):
    INDEX = "index"
    DIRECT_URL = "direct_url"
    VCS = "vcs"
    LOCAL = "local"
    EDITABLE = "editable"
    OPTION = "option"
    INVALID = "invalid"


class RequirementsParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ParsedRequirement:
    line_number: int
    name: str
    specifier: str
    marker: str
    marker_sha256: str
    extras: tuple[str, ...]
    hashes: tuple[str, ...]
    source: RequirementSource
    declaration_sha256: str

    def audit_value(self) -> dict[str, object]:
        return {
            "line_number": self.line_number,
            "name": self.name,
            "specifier": self.specifier,
            "marker": self.marker,
            "marker_sha256": self.marker_sha256,
            "extras": list(self.extras),
            "hashes": list(self.hashes),
            "source": self.source.value,
            "declaration_sha256": self.declaration_sha256,
        }


@dataclass(frozen=True, slots=True)
class RequirementDiagnostic:
    code: str
    line_number: int
    source: RequirementSource
    name: str
    message: str
    evidence: str


@dataclass(frozen=True, slots=True)
class RequirementsParseResult:
    content_sha256: str
    requirements: tuple[ParsedRequirement, ...]
    diagnostics: tuple[RequirementDiagnostic, ...]


def parse_requirements(content: bytes | str) -> RequirementsParseResult:
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if len(raw) > MAX_REQUIREMENTS_BYTES:
        raise RequirementsParseError(
            "requirements_too_large",
            "requirements.txt exceeds the parser byte limit",
        )
    if b"\x00" in raw:
        raise RequirementsParseError(
            "requirements_binary",
            "requirements.txt contains unsupported binary content",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequirementsParseError(
            "requirements_encoding_invalid",
            "requirements.txt must use UTF-8",
        ) from exc

    logical_lines = _logical_requirement_lines(text)
    if len(logical_lines) > MAX_REQUIREMENT_LINES:
        raise RequirementsParseError(
            "requirements_too_many_lines",
            "requirements.txt contains too many logical entries",
        )

    parsed: list[ParsedRequirement] = []
    diagnostics: list[RequirementDiagnostic] = []
    for line_number, line in logical_lines:
        requirement, issues = _parse_line(line_number, line)
        if requirement is not None:
            parsed.append(requirement)
        diagnostics.extend(issues)
    diagnostics.extend(_conflicting_declarations(parsed))
    diagnostics = list(
        {
            (item.code, item.line_number, item.name, item.evidence): item for item in diagnostics
        }.values()
    )
    diagnostics.sort(key=lambda item: (item.line_number, item.code, item.name, item.evidence))
    parsed.sort(key=lambda item: (item.line_number, item.name, item.declaration_sha256))
    return RequirementsParseResult(
        content_sha256=hashlib.sha256(raw).hexdigest() if raw.strip() else "",
        requirements=tuple(parsed),
        diagnostics=tuple(diagnostics),
    )


def _parse_line(
    line_number: int,
    line: str,
) -> tuple[ParsedRequirement | None, list[RequirementDiagnostic]]:
    line_sha256 = hashlib.sha256(line.encode("utf-8")).hexdigest()
    editable = False
    candidate = line.strip()
    lowered = candidate.casefold()
    for prefix in ("-e ", "--editable "):
        if lowered.startswith(prefix):
            editable = True
            candidate = candidate[len(prefix) :].strip()
            break

    hashes: list[str] = []

    def remove_hash(match: re.Match[str]) -> str:
        algorithm = match.group("algorithm").casefold()
        digest = match.group("digest").casefold()
        if algorithm == "sha256" and len(digest) == 64:
            hashes.append(f"sha256:{digest}")
        else:
            hashes.append("invalid")
        return " "

    candidate = _HASH_OPTION.sub(remove_hash, candidate)
    candidate = " ".join(candidate.split())
    diagnostics: list[RequirementDiagnostic] = []
    if len(hashes) > MAX_REQUIREMENT_HASHES or "invalid" in hashes:
        diagnostics.append(
            _diagnostic(
                "dependency_hash_invalid",
                line_number,
                RequirementSource.INVALID,
                "",
                "Requirement hashes must use bounded sha256 digests",
                line_sha256,
            )
        )
        hashes = [item for item in hashes if item != "invalid"][:MAX_REQUIREMENT_HASHES]
    if candidate.startswith("-"):
        diagnostics.append(
            _diagnostic(
                "dependency_source_option",
                line_number,
                RequirementSource.OPTION,
                "",
                "requirements.txt cannot change indexes, constraints, includes, or pip options",
                line_sha256,
            )
        )
        return None, diagnostics
    if " --" in f" {candidate}":
        diagnostics.append(
            _diagnostic(
                "dependency_option_invalid",
                line_number,
                RequirementSource.OPTION,
                "",
                "Requirement contains an unsupported pip option",
                line_sha256,
            )
        )
        return None, diagnostics

    source_hint = RequirementSource.EDITABLE if editable else _source_from_value(candidate)
    try:
        requirement = Requirement(candidate)
    except InvalidRequirement:
        source = (
            source_hint if source_hint is not RequirementSource.INDEX else RequirementSource.INVALID
        )
        diagnostics.append(
            _diagnostic(
                _source_code(source)
                if source is not RequirementSource.INVALID
                else "dependency_requirement_invalid",
                line_number,
                source,
                "",
                _source_message(source),
                line_sha256,
            )
        )
        return None, diagnostics

    name = canonicalize_name(requirement.name)
    source = RequirementSource.EDITABLE if editable else _source_from_value(requirement.url or "")
    marker_value = str(requirement.marker) if requirement.marker is not None else ""
    marker_sha256 = hashlib.sha256(marker_value.encode("utf-8")).hexdigest() if marker_value else ""
    marker = "<redacted>" if marker_value and _SECRET_VALUE.search(marker_value) else marker_value
    parsed = ParsedRequirement(
        line_number=line_number,
        name=name,
        specifier=str(requirement.specifier),
        marker=marker[:512],
        marker_sha256=marker_sha256,
        extras=tuple(sorted(canonicalize_name(extra) for extra in requirement.extras)),
        hashes=tuple(sorted(set(hashes))),
        source=source,
        declaration_sha256=line_sha256,
    )
    if source is not RequirementSource.INDEX:
        diagnostics.append(
            _diagnostic(
                _source_code(source),
                line_number,
                source,
                name,
                _source_message(source),
                line_sha256,
            )
        )
    return parsed, diagnostics


def _logical_requirement_lines(value: str) -> tuple[tuple[int, str], ...]:
    values: list[tuple[int, str]] = []
    pending = ""
    pending_line = 0
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if not pending:
            pending_line = line_number
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\") and not pending.endswith("\\\\"):
            pending = pending[:-1].rstrip()
            continue
        values.append((pending_line, pending))
        pending = ""
        pending_line = 0
    if pending:
        raise RequirementsParseError(
            "requirements_continuation_incomplete",
            "requirements.txt has an incomplete continuation",
        )
    return tuple(values)


def _strip_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character in {"'", '"'}:
            quote = "" if quote == character else (character if not quote else quote)
            continue
        if character == "#" and not quote and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _source_from_value(value: str) -> RequirementSource:
    normalized = str(value or "").strip().casefold()
    if not normalized:
        return RequirementSource.INDEX
    if normalized.startswith(_VCS_SCHEMES):
        return RequirementSource.VCS
    if normalized.startswith(_URL_SCHEMES):
        return RequirementSource.DIRECT_URL
    if normalized.startswith(_LOCAL_PREFIXES) or "\\" in normalized:
        return RequirementSource.LOCAL
    return RequirementSource.DIRECT_URL


def _source_code(source: RequirementSource) -> str:
    return {
        RequirementSource.DIRECT_URL: "dependency_direct_url",
        RequirementSource.VCS: "dependency_vcs_source",
        RequirementSource.LOCAL: "dependency_local_source",
        RequirementSource.EDITABLE: "dependency_editable_source",
        RequirementSource.OPTION: "dependency_source_option",
    }.get(source, "dependency_requirement_invalid")


def _source_message(source: RequirementSource) -> str:
    return {
        RequirementSource.DIRECT_URL: "Direct URL dependencies are not accepted by the plugin source",
        RequirementSource.VCS: "VCS dependencies are not accepted by the plugin source",
        RequirementSource.LOCAL: "Local path dependencies are not accepted by the plugin source",
        RequirementSource.EDITABLE: "Editable dependencies are not accepted by the plugin source",
        RequirementSource.OPTION: "requirements.txt cannot change installer source configuration",
    }.get(source, "Requirement declaration is invalid")


def _diagnostic(
    code: str,
    line_number: int,
    source: RequirementSource,
    name: str,
    message: str,
    line_sha256: str,
) -> RequirementDiagnostic:
    evidence = f"source={source.value}; name={name or 'unknown'}; line_sha256={line_sha256}"
    return RequirementDiagnostic(code, line_number, source, name, message, evidence)


def _conflicting_declarations(
    values: list[ParsedRequirement],
) -> list[RequirementDiagnostic]:
    groups: dict[tuple[str, tuple[str, ...], str], list[ParsedRequirement]] = {}
    for value in values:
        groups.setdefault((value.name, value.extras, value.marker_sha256), []).append(value)
    diagnostics: list[RequirementDiagnostic] = []
    for (name, _extras, _marker), declarations in groups.items():
        identities = {(item.specifier, item.source, item.hashes) for item in declarations}
        if len(identities) <= 1:
            continue
        latest = declarations[-1]
        diagnostics.append(
            RequirementDiagnostic(
                "dependency_declaration_conflict",
                latest.line_number,
                latest.source,
                name,
                "Dependency has multiple inconsistent declarations for the same marker scope",
                (
                    f"source={latest.source.value}; name={name}; "
                    f"declaration_sha256={latest.declaration_sha256}"
                ),
            )
        )
    return diagnostics


__all__ = [
    "MAX_REQUIREMENTS_BYTES",
    "ParsedRequirement",
    "RequirementDiagnostic",
    "RequirementSource",
    "RequirementsParseError",
    "RequirementsParseResult",
    "parse_requirements",
]
