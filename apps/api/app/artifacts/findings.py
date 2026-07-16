from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..auth import is_admin
from .content import ArtifactContentError, ArtifactContentService
from .models import ArtifactErrorCode, ArtifactStateError, PublicationStatus
from .repository import ArtifactRepository

_PACKAGE_NORMALIZE = re.compile(r"[-_.]+")
_ACTIVE_FINDING_STATUSES = frozenset({"open", "accepted"})
_STABLE_REPLAY_STATUSES = frozenset(
    {
        PublicationStatus.PUBLISHED.value,
        PublicationStatus.REVOKING.value,
        PublicationStatus.REVOKE_FAILED.value,
        PublicationStatus.REVOKED.value,
    }
)


class StableRiskError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class StableRiskEvidence:
    kind: str
    deterministic: bool
    candidate_artifact_id: str
    stable_artifact_id: str
    finding_id: str
    fingerprint: str = ""
    path: str = ""
    file_sha256: str = ""
    package_name: str = ""
    package_version: str = ""
    advisory_id: str = ""
    tool_name: str = ""
    tool_version: str = ""
    ruleset_version: str = ""
    confirmed_by_nickname: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "kind": self.kind,
                "deterministic": self.deterministic,
                "candidate_artifact_id": self.candidate_artifact_id,
                "stable_artifact_id": self.stable_artifact_id,
                "finding_id": self.finding_id,
                "fingerprint": self.fingerprint,
                "path": self.path,
                "file_sha256": self.file_sha256,
                "package_name": self.package_name,
                "package_version": self.package_version,
                "advisory_id": self.advisory_id,
                "tool_name": self.tool_name,
                "tool_version": self.tool_version,
                "ruleset_version": self.ruleset_version,
                "confirmed_by_nickname": self.confirmed_by_nickname,
                "reason": self.reason,
            }.items()
            if value not in {"", None}
        }


class StableRiskService:
    def __init__(
        self,
        repository: ArtifactRepository,
        content: ArtifactContentService,
    ) -> None:
        self.repository = repository
        self.content = content

    async def request_revoke(
        self,
        *,
        candidate_artifact_id: str,
        finding_id: str,
        actor: Mapping[str, Any],
        expected_version: int,
        reason: str,
        confirm_affects_current_release: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not is_admin(actor):
            raise StableRiskError(
                ArtifactErrorCode.COMMENT_ACTION_FORBIDDEN.value,
                "Stable risk action is forbidden",
                status_code=403,
            )
        normalized_reason = " ".join(str(reason or "").split())
        if not normalized_reason:
            raise StableRiskError("reason_required", "紧急撤回必须填写原因", status_code=400)
        if len(normalized_reason) > 2000:
            raise StableRiskError("reason_invalid", "紧急撤回原因过长", status_code=400)
        key = _idempotency_key(idempotency_key)
        candidate = await self.repository.get_artifact(candidate_artifact_id)
        finding = await self.repository.get_review_finding(candidate_artifact_id, finding_id)
        if candidate is None or finding is None:
            raise StableRiskError(
                "artifact_finding_not_found",
                "候选 Artifact 或 finding 不存在",
                status_code=404,
            )
        self._validate_candidate_finding(finding)
        stable = await self._stable_artifact(candidate, finding)
        evidence = (
            self._admin_evidence(candidate, stable, finding, actor, normalized_reason)
            if confirm_affects_current_release
            else await self._deterministic_evidence(candidate, stable, finding)
        )
        if evidence is None:
            raise StableRiskError(
                ArtifactErrorCode.STABLE_RELEASE_CORRELATION_REQUIRED.value,
                "没有确定性证据证明该 finding 影响当前稳定版本",
                status_code=409,
            )
        correlation = evidence.as_dict()
        finding_metadata = {
            "stable_risk": correlation,
            "policy_version_id": candidate.get("policy_version_id"),
        }
        finding_metadata["request_fingerprint"] = _request_fingerprint(
            {
                "candidate_artifact_id": candidate_artifact_id,
                "finding_id": finding_id,
                "expected_version": expected_version,
                "actor_user_id": actor.get("id"),
                "actor_nickname": _actor_name(actor),
                "reason": normalized_reason,
                "correlation": correlation,
            }
        )
        finding_link = {
            "expected_version": expected_version,
            "candidate_artifact_id": candidate_artifact_id,
            "finding_id": finding_id,
            "status": finding["status"],
            "correlation": correlation,
            "affects_current_release": True,
            "actor_user_id": actor.get("id"),
            "actor_nickname": _actor_name(actor),
            "actor_source": "user",
            "reason": normalized_reason,
            "metadata": finding_metadata,
            "expected_finding": {
                "fingerprint": finding.get("fingerprint"),
                "run_id": finding.get("run_id"),
                "rule_id": finding.get("rule_id"),
                "source": finding.get("source"),
                "deterministic": bool(finding.get("deterministic")),
                "severity": finding.get("severity"),
                "status": finding.get("status"),
                "file_path": finding.get("file_path"),
                "file_sha256": finding.get("file_sha256"),
                "correlation": dict(finding.get("correlation") or {}),
            },
            "idempotency_key": f"stable-risk-link:{key}",
        }
        revoke_metadata = {
            "emergency": True,
            "stable_risk": correlation,
            "candidate_artifact_id": candidate_artifact_id,
            "finding_id": finding_id,
            "candidate_policy_version_id": candidate.get("policy_version_id"),
            "stable_policy_version_id": stable.get("policy_version_id"),
        }
        notification = {
            "event_type": "artifact_stable_risk_revoking",
            "aggregate_type": "artifact",
            "aggregate_id": stable["id"],
            "recipient_user_id": stable.get("submitted_by"),
            "payload": {
                "artifact_id": stable["id"],
                "plugin_id": stable["plugin_id"],
                "candidate_artifact_id": candidate_artifact_id,
                "finding_id": finding_id,
                "correlation_kind": correlation["kind"],
                "emergency": True,
                "reason": normalized_reason,
            },
            "dedupe_key": f"artifact:{stable['id']}:stable-risk-revoking:{key}",
        }
        try:
            revoking = await self.repository.request_revoke_artifact(
                str(stable["id"]),
                reason=normalized_reason,
                reviewer=actor,
                idempotency_key=f"stable-risk-revoke:{key}",
                source="admin",
                input_fingerprints=[str(finding.get("fingerprint") or "")],
                metadata=revoke_metadata,
                finding_link=finding_link,
                notification=notification,
            )
        except ArtifactStateError as exc:
            raise StableRiskError(exc.code, "Stable risk state changed", status_code=409) from exc
        except ValueError as exc:
            raise _stable_repository_error(exc) from exc
        if revoking is None:
            raise StableRiskError(
                "stable_artifact_not_found",
                "当前稳定 Artifact 不存在",
                status_code=404,
            )
        return {
            "candidate_artifact_id": candidate_artifact_id,
            "finding_id": finding_id,
            "affects_current_release": True,
            "correlation": correlation,
            "stable_artifact": revoking,
        }

    async def _stable_artifact(
        self,
        candidate: Mapping[str, Any],
        finding: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        correlation = finding.get("correlation")
        correlated_id = (
            str(correlation.get("stable_artifact_id") or "")
            if isinstance(correlation, Mapping)
            else ""
        )
        current_id = str(candidate.get("current_artifact_id") or "")
        if current_id:
            if correlated_id and correlated_id != current_id:
                raise StableRiskError(
                    ArtifactErrorCode.STABLE_RELEASE_CORRELATION_REQUIRED.value,
                    "Finding 关联的稳定版本已不是当前版本",
                    status_code=409,
                )
            stable_id = current_id
        else:
            stable_id = correlated_id if bool(finding.get("affects_current_release")) else ""
        if not stable_id or stable_id == str(candidate["id"]):
            raise StableRiskError(
                ArtifactErrorCode.STABLE_RELEASE_CORRELATION_REQUIRED.value,
                "插件没有可关联的当前稳定版本",
                status_code=409,
            )
        stable = await self.repository.get_artifact(stable_id)
        if (
            stable is None
            or str(stable.get("plugin_id") or "") != str(candidate.get("plugin_id") or "")
            or str(stable.get("publication_status") or "") not in _STABLE_REPLAY_STATUSES
        ):
            raise StableRiskError(
                ArtifactErrorCode.STABLE_RELEASE_CORRELATION_REQUIRED.value,
                "当前稳定版本不可用于风险关联",
                status_code=409,
            )
        return stable

    async def _deterministic_evidence(
        self,
        candidate: Mapping[str, Any],
        stable: Mapping[str, Any],
        finding: Mapping[str, Any],
    ) -> StableRiskEvidence | None:
        if not bool(finding.get("deterministic")) or str(finding.get("source") or "") == "llm":
            return None
        path_evidence = await self._path_sha_evidence(candidate, stable, finding)
        if path_evidence is not None:
            return path_evidence
        dependency_evidence = await self._dependency_evidence(candidate, stable, finding)
        if dependency_evidence is not None:
            return dependency_evidence
        return await self._fingerprint_evidence(candidate, stable, finding)

    async def _path_sha_evidence(
        self,
        candidate: Mapping[str, Any],
        stable: Mapping[str, Any],
        finding: Mapping[str, Any],
    ) -> StableRiskEvidence | None:
        path = str(finding.get("file_path") or "")
        file_sha256 = str(finding.get("file_sha256") or "")
        if not path or len(file_sha256) != 64:
            return None
        try:
            _, _, candidate_file = await self.content.resolve_path(candidate, path)
            _, _, stable_file = await self.content.resolve_path(stable, path)
        except ArtifactContentError:
            return None
        if candidate_file.sha256 != file_sha256 or stable_file.sha256 != file_sha256:
            return None
        return StableRiskEvidence(
            kind="path_sha",
            deterministic=True,
            candidate_artifact_id=str(candidate["id"]),
            stable_artifact_id=str(stable["id"]),
            finding_id=str(finding["id"]),
            fingerprint=str(finding.get("fingerprint") or ""),
            path=path,
            file_sha256=file_sha256,
        )

    async def _dependency_evidence(
        self,
        candidate: Mapping[str, Any],
        stable: Mapping[str, Any],
        finding: Mapping[str, Any],
    ) -> StableRiskEvidence | None:
        if str(finding.get("source") or "") != "dependency":
            return None
        dependency = _dependency_tuple(finding.get("correlation"))
        if dependency is None:
            return None
        for stable_finding in await self.repository.list_findings(str(stable["id"])):
            if (
                str(stable_finding.get("source") or "") == "dependency"
                and bool(stable_finding.get("deterministic"))
                and _active_finding(stable_finding)
                and _dependency_tuple(stable_finding.get("correlation")) == dependency
            ):
                return StableRiskEvidence(
                    kind="dependency",
                    deterministic=True,
                    candidate_artifact_id=str(candidate["id"]),
                    stable_artifact_id=str(stable["id"]),
                    finding_id=str(finding["id"]),
                    fingerprint=str(finding.get("fingerprint") or ""),
                    package_name=dependency[0],
                    package_version=dependency[1],
                    advisory_id=dependency[2],
                )
        return None

    async def _fingerprint_evidence(
        self,
        candidate: Mapping[str, Any],
        stable: Mapping[str, Any],
        finding: Mapping[str, Any],
    ) -> StableRiskEvidence | None:
        fingerprint = str(finding.get("fingerprint") or "")
        rule_id = str(finding.get("rule_id") or "")
        if not fingerprint or not rule_id:
            return None
        candidate_runs = {
            str(run["id"]): run
            for run in await self.repository.list_review_runs(str(candidate["id"]))
        }
        stable_runs = {
            str(run["id"]): run for run in await self.repository.list_review_runs(str(stable["id"]))
        }
        candidate_run = candidate_runs.get(str(finding.get("run_id") or ""))
        snapshot = _tool_snapshot(candidate_run)
        if snapshot is None:
            return None
        for stable_finding in await self.repository.list_findings(str(stable["id"])):
            stable_run = stable_runs.get(str(stable_finding.get("run_id") or ""))
            if (
                str(stable_finding.get("fingerprint") or "") == fingerprint
                and str(stable_finding.get("rule_id") or "") == rule_id
                and str(stable_finding.get("source") or "") == str(finding.get("source") or "")
                and bool(stable_finding.get("deterministic"))
                and _active_finding(stable_finding)
                and _tool_snapshot(stable_run) == snapshot
            ):
                return StableRiskEvidence(
                    kind="fingerprint",
                    deterministic=True,
                    candidate_artifact_id=str(candidate["id"]),
                    stable_artifact_id=str(stable["id"]),
                    finding_id=str(finding["id"]),
                    fingerprint=fingerprint,
                    tool_name=snapshot[0],
                    tool_version=snapshot[1],
                    ruleset_version=snapshot[2],
                )
        return None

    @staticmethod
    def _admin_evidence(
        candidate: Mapping[str, Any],
        stable: Mapping[str, Any],
        finding: Mapping[str, Any],
        actor: Mapping[str, Any],
        reason: str,
    ) -> StableRiskEvidence:
        return StableRiskEvidence(
            kind="admin_confirmation",
            deterministic=False,
            candidate_artifact_id=str(candidate["id"]),
            stable_artifact_id=str(stable["id"]),
            finding_id=str(finding["id"]),
            fingerprint=str(finding.get("fingerprint") or ""),
            confirmed_by_nickname=_actor_name(actor),
            reason=reason,
        )

    @staticmethod
    def _validate_candidate_finding(finding: Mapping[str, Any]) -> None:
        if (
            str(finding.get("severity") or "") != "critical"
            or str(finding.get("status") or "open") not in _ACTIVE_FINDING_STATUSES
        ):
            raise StableRiskError(
                ArtifactErrorCode.STABLE_RELEASE_CORRELATION_REQUIRED.value,
                "只有仍有效的 critical finding 可以关联稳定版本",
                status_code=409,
            )


def _dependency_tuple(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, Mapping) or not isinstance(value.get("dependency"), Mapping):
        return None
    dependency = value["dependency"]
    name = _PACKAGE_NORMALIZE.sub("-", str(dependency.get("name") or "").strip().lower())
    version = str(dependency.get("version") or "").strip()
    advisory_id = str(dependency.get("advisory_id") or "").strip().upper()
    if not name or not version or not advisory_id:
        return None
    return name, version, advisory_id


def _tool_snapshot(run: Mapping[str, Any] | None) -> tuple[str, str, str] | None:
    if not run:
        return None
    snapshot = (
        str(run.get("tool_name") or ""),
        str(run.get("tool_version") or ""),
        str(run.get("ruleset_version") or ""),
    )
    return snapshot if all(snapshot) else None


def _active_finding(finding: Mapping[str, Any]) -> bool:
    return str(finding.get("status") or "open") in _ACTIVE_FINDING_STATUSES


def _idempotency_key(value: str) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 200 or any(ord(char) < 33 or ord(char) == 127 for char in key):
        raise StableRiskError(
            ArtifactErrorCode.IDEMPOTENCY_KEY_REQUIRED.value,
            "Idempotency key is invalid",
            status_code=400,
        )
    return key


def _actor_name(actor: Mapping[str, Any]) -> str:
    return str(
        actor.get("nickname")
        or actor.get("name")
        or actor.get("github_name")
        or actor.get("internal_username")
        or actor.get("github_login")
        or ""
    )[:200]


def _stable_repository_error(exc: ValueError) -> StableRiskError:
    code = str(exc)
    status = 409
    return StableRiskError(code, "Stable risk state changed", status_code=status)


def _request_fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
