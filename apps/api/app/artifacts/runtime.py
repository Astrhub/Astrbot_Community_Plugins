from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import (
    ArtifactReviewSettings,
    ArtifactSettings,
    Settings,
    runtime_image_digest,
)
from ..runtime_runner.storage import LocalRuntimeResultWriter
from .advisory import (
    DependencyAdvisoryProvider,
    HttpsDependencyAdvisoryProvider,
    LocalDependencyAdvisoryProvider,
    UnavailableDependencyAdvisoryProvider,
)
from .archive import ArchivePrechecker
from .category import OpenAICompatibleCategoryProvider
from .github_source import GithubSourceClient
from .jobs import ArtifactJobRunner, worker_id
from .malware import (
    ClamAvScanner,
    ClamdInstreamScanner,
    UnavailableClamAvScanner,
    UnavailableYaraScanner,
    YaraRulesetSnapshot,
    YaraScanner,
    YaraSubprocessScanner,
)
from .models import DecisionAction, DecisionSource, JobStatus, JobType, ReviewRunType
from .notifications import ArtifactNotificationDispatcher
from .policy import ReviewPolicyV1, parse_review_policy, review_policy_sha256
from .repository import InMemoryArtifactRepository, PgArtifactRepository
from .runtime_dispatch import RuntimeDispatchController
from .service import ArtifactService
from .static_scan import StaticScanner
from .storage import create_artifact_storage
from .structured_llm import OpenAICompatibleStructuredLlmProvider


@dataclass(slots=True)
class ArtifactRuntime:
    """Application-owned artifact components without an in-process worker."""

    config: ArtifactSettings
    settings: Settings
    database_url: str
    store: Any
    github_api_token: str = ""
    allow_in_memory_artifacts: bool = False
    worker_execution_enabled: bool = False
    started: bool = False
    generation: int = 0
    repository: Any | None = None
    storage: Any | None = None
    service: Any | None = None
    job_runner: Any | None = None
    _component_errors: list[str] = field(default_factory=list)

    @property
    def configuration_errors(self) -> tuple[str, ...]:
        return (*self.config.validation_errors(self.database_url), *self._component_errors)

    @property
    def components_configured(self) -> bool:
        return all(
            component is not None
            for component in (self.repository, self.storage, self.service, self.job_runner)
        )

    @property
    def available(self) -> bool:
        return (
            self.config.enabled
            and self.started
            and self.components_configured
            and not self.configuration_errors
        )

    async def start(self, store: Any) -> None:
        self.rebind_store(store)
        if not self.components_configured:
            self._component_errors.clear()
        if self.config.enabled and not self.configuration_errors and not self.components_configured:
            if not hasattr(store, "_pool") and not self.allow_in_memory_artifacts:
                self.set_component_error("postgresql_artifact_store_required")
                self.started = True
                return
            try:
                repository = (
                    PgArtifactRepository(store)
                    if hasattr(store, "_pool")
                    else InMemoryArtifactRepository(store)
                )
                storage = create_artifact_storage(self.config)
                service = ArtifactService(
                    repository=repository,
                    storage=storage,
                    github=GithubSourceClient(self.github_api_token),
                    max_upload_bytes=self.config.max_upload_bytes,
                )
                identity = worker_id()
                notifications = ArtifactNotificationDispatcher(
                    repository=repository,
                    store=store,
                    settings=self.settings,
                    worker_id=identity,
                    lease_seconds=self.config.job_lease_seconds,
                )
                category_provider = _category_provider(self.config.review)
                llm_provider = _structured_llm_provider(self.config.review)
                clamav_scanner, yara_scanner = _malware_scanners(
                    self.config.review,
                    enabled=self.worker_execution_enabled,
                )
                runtime_result_storage = None
                runtime_controller = None
                runtime_digest = ""
                if self.worker_execution_enabled and self.config.review.runtime_enabled:
                    runtime_digest = runtime_image_digest(
                        self.config.review.runtime_container_image
                    )
                    result_root = Path(self.config.review.runtime_result_root)
                    if runtime_digest and result_root.is_absolute():
                        runtime_result_storage = LocalRuntimeResultWriter(result_root)
                        runtime_controller = RuntimeDispatchController(
                            repository,
                            runtime_result_storage,
                        )
                dependency_provider = _dependency_provider(
                    self.config.review,
                    enabled=self.worker_execution_enabled,
                )
                job_runner = ArtifactJobRunner(
                    repository=repository,
                    storage=storage,
                    prechecker=ArchivePrechecker(self.config),
                    scanner=StaticScanner(),
                    worker_id=identity,
                    lease_seconds=self.config.job_lease_seconds,
                    poll_seconds=self.config.worker_poll_seconds,
                    notification_dispatcher=notifications,
                    advanced_review_enabled=self.config.review.enabled,
                    category_provider=category_provider,
                    category_provider_config_ref=self.config.review.llm_config_ref,
                    llm_provider=llm_provider,
                    llm_provider_config_ref=self.config.review.llm_config_ref,
                    clamav_scanner=clamav_scanner,
                    yara_scanner=yara_scanner,
                    runtime_controller=runtime_controller,
                    runtime_image_digest=runtime_digest,
                    runtime_result_storage=runtime_result_storage,
                    dependency_provider=dependency_provider,
                )
                self.attach_components(
                    repository=repository,
                    storage=storage,
                    service=service,
                    job_runner=job_runner,
                )
            except Exception:
                self.set_component_error("artifact_components_initialization_failed")
        self.started = True

    async def close(self) -> None:
        self.started = False
        for component in (self.job_runner, self.service, self.storage, self.repository):
            close = getattr(component, "close", None)
            if close:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    def rebind_store(self, store: Any) -> None:
        self.store = store
        self.generation += 1
        rebind = getattr(self.repository, "rebind_store", None)
        if rebind:
            rebind(store)
        rebind_runner = getattr(self.job_runner, "rebind_store", None)
        if rebind_runner:
            rebind_runner(store)

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.config = settings.artifacts
        self.database_url = settings.database_url
        self.github_api_token = settings.github_api_token
        dispatcher = getattr(self.job_runner, "notification_dispatcher", None)
        if dispatcher is not None:
            dispatcher.settings = settings
        if self.job_runner is not None:
            self.job_runner.configure_advanced_review(self.config.review.enabled)

    def attach_components(
        self,
        *,
        repository: Any,
        storage: Any,
        service: Any,
        job_runner: Any,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.service = service
        self.job_runner = job_runner
        self._component_errors.clear()

    def set_component_error(self, code: str) -> None:
        if code not in self._component_errors:
            self._component_errors.append(code)

    def public_status(self) -> dict[str, object]:
        status = self.config.public_status(self.database_url)
        status.update(
            {
                "available": self.available,
                "started": self.started,
                "components_configured": self.components_configured,
                "worker_mode": "external",
                "worker_configured": self.job_runner is not None,
                # API 进程不把“已配置”冒充为外部 Worker 存活。
                "worker_ready": False,
                "storage_ready": self.storage is not None,
                "configuration_errors": list(self.configuration_errors),
            }
        )
        return status

    async def health_status(self) -> dict[str, object]:
        status = self.public_status()
        heartbeats, heartbeat_error = await self._review_heartbeats()
        status["worker_ready"] = any(
            item.get("worker_kind") == "artifact_worker" and item.get("live") is True
            for item in heartbeats
        )
        review = await self._review_health_status(
            heartbeats=heartbeats,
            heartbeat_error=heartbeat_error,
        )
        status["review"] = {
            "enabled": bool(review.get("enabled")),
            "configured": bool(review.get("configured")),
            "ready": bool(review.get("ready")),
            "degraded": bool(review.get("degraded")),
        }
        return status

    async def _review_health_status(
        self,
        *,
        heartbeats: list[dict[str, Any]] | None = None,
        heartbeat_error: bool = False,
    ) -> dict[str, object]:
        review = self.config.review.public_status()
        if not self.config.review.enabled:
            return review
        if self.repository is None:
            return _finalize_review_status(
                review,
                policy=_health_component(
                    enabled=True,
                    configured=False,
                    ready=False,
                    reasons=["policy_repository_unavailable"],
                ),
                policy_model=None,
                tool_health={},
                config=self.config,
            )
        try:
            active = await self.repository.get_active_review_policy()
        except Exception:
            return _finalize_review_status(
                review,
                policy=_health_component(
                    enabled=True,
                    configured=False,
                    ready=False,
                    reasons=["active_policy_lookup_failed"],
                ),
                policy_model=None,
                tool_health={},
                config=self.config,
            )
        if not active:
            return _finalize_review_status(
                review,
                policy=_health_component(
                    enabled=True,
                    configured=False,
                    ready=False,
                    reasons=["active_policy_missing"],
                ),
                policy_model=None,
                tool_health={},
                config=self.config,
            )

        policy_model: ReviewPolicyV1 | None = None
        reasons: list[str] = []
        try:
            policy_model = parse_review_policy(active.get("policy") or {})
        except ValidationError:
            reasons.append("active_policy_schema_invalid")
        if policy_model:
            summary = dict(active.get("validation_summary") or {})
            if summary.get("valid") is not True:
                reasons.append("active_policy_not_validated")
            if str(summary.get("policy_sha256") or "") != str(active.get("policy_sha256") or ""):
                reasons.append("active_policy_validation_stale")
            if review_policy_sha256(policy_model) != str(active.get("policy_sha256") or ""):
                reasons.append("active_policy_hash_mismatch")
        policy_component = _health_component(
            enabled=True,
            configured=not reasons,
            ready=not reasons,
            reasons=reasons,
        )
        resolved_heartbeats = heartbeats
        if resolved_heartbeats is None:
            resolved_heartbeats, heartbeat_error = await self._review_heartbeats()
        latest_runs = await self._latest_tool_runs()
        tool_health = _resolved_tool_health(
            policy_model,
            resolved_heartbeats,
            latest_runs,
            heartbeat_error=heartbeat_error,
            require_freshness=True,
        )
        return _finalize_review_status(
            review,
            policy=policy_component,
            policy_model=policy_model if not reasons else None,
            tool_health=tool_health,
            config=self.config,
        )

    async def review_policy_readiness_issues(
        self,
        policy: ReviewPolicyV1,
    ) -> list[dict[str, str]]:
        heartbeats, heartbeat_error = await self._review_heartbeats()
        tool_health = _resolved_tool_health(
            policy,
            heartbeats,
            await self._latest_tool_runs(),
            heartbeat_error=heartbeat_error,
            require_freshness=False,
        )
        status = _finalize_review_status(
            self.config.review.public_status(),
            policy=_health_component(
                enabled=True,
                configured=self.config.review.enabled,
                ready=self.config.review.enabled,
                reasons=[] if self.config.review.enabled else ["advanced_review_disabled"],
            ),
            policy_model=policy,
            tool_health=tool_health,
            config=self.config,
        )
        issues: list[dict[str, str]] = []
        for name, component in dict(status.get("components") or {}).items():
            if name == "policy" or not isinstance(component, Mapping):
                continue
            if not component.get("enabled") or component.get("ready"):
                continue
            reasons = component.get("reasons") if isinstance(component.get("reasons"), list) else []
            code = str(reasons[0] if reasons else f"{name}_not_ready")[:96]
            issues.append(
                {
                    "path": f"tools.{name}",
                    "code": code,
                    "message": "Required review tool is not ready for policy activation",
                }
            )
        if not self.config.review.enabled:
            issues.append(
                {
                    "path": "policy",
                    "code": "advanced_review_disabled",
                    "message": "Advanced review must be enabled before activating a policy",
                }
            )
        return issues[:100]

    async def review_operations_status(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        heartbeats, heartbeat_error = await self._review_heartbeats()
        review = await self._review_health_status(
            heartbeats=heartbeats,
            heartbeat_error=heartbeat_error,
        )
        latest_runs = await self._latest_tool_runs()
        active = None
        if self.repository is not None:
            try:
                active = await self.repository.get_active_review_policy()
            except Exception:
                active = None
        policy_model = None
        if active:
            try:
                policy_model = parse_review_policy(active.get("policy") or {})
            except ValidationError:
                policy_model = None
        tool_health = _resolved_tool_health(
            policy_model,
            heartbeats,
            latest_runs,
            heartbeat_error=heartbeat_error,
            require_freshness=True,
        )
        snapshot = _empty_observability_snapshot(now - timedelta(hours=24))
        metrics_available = False
        if self.repository is not None:
            try:
                snapshot = await self.repository.get_review_observability_snapshot(
                    now - timedelta(hours=24)
                )
                metrics_available = True
            except Exception:
                pass
        return {
            "health": {
                "review": review,
                "workers": _worker_health_projection(heartbeats, heartbeat_error=heartbeat_error),
                "tools": _tool_health_projection(review, tool_health, active),
            },
            "metrics": _metrics_projection(
                snapshot,
                collected_at=now,
                available=metrics_available,
            ),
        }

    async def _review_heartbeats(self) -> tuple[list[dict[str, Any]], bool]:
        if self.repository is None:
            return [], True
        list_heartbeats = getattr(self.repository, "list_review_worker_heartbeats", None)
        if list_heartbeats is None:
            return [], True
        try:
            return list(await list_heartbeats(100)), False
        except Exception:
            return [], True

    async def _latest_tool_runs(self) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        list_runs = getattr(self.repository, "list_latest_review_tool_runs", None)
        if list_runs is None:
            return []
        try:
            return list(await list_runs())
        except Exception:
            return []


def build_artifact_runtime(
    settings: Settings,
    store: Any,
    *,
    allow_in_memory_artifacts: bool = True,
    worker_execution_enabled: bool = False,
) -> ArtifactRuntime:
    """Build the shared runtime shell without starting an in-process worker."""
    return ArtifactRuntime(
        config=settings.artifacts,
        settings=settings,
        database_url=settings.database_url,
        store=store,
        github_api_token=settings.github_api_token,
        allow_in_memory_artifacts=allow_in_memory_artifacts,
        worker_execution_enabled=worker_execution_enabled,
    )


def _category_provider(
    review: ArtifactReviewSettings,
) -> OpenAICompatibleCategoryProvider | None:
    if not review.llm_enabled or review.llm_provider not in {"openai", "openai-compatible"}:
        return None
    if not all((review.llm_endpoint_url, review.llm_api_key, review.llm_model)):
        return None
    try:
        return OpenAICompatibleCategoryProvider(
            endpoint_url=review.llm_endpoint_url,
            api_key=review.llm_api_key,
            configured_model=review.llm_model,
        )
    except ValueError:
        return None


def _structured_llm_provider(
    review: ArtifactReviewSettings,
) -> OpenAICompatibleStructuredLlmProvider | None:
    if not review.llm_enabled or review.llm_provider not in {"openai", "openai-compatible"}:
        return None
    if not all((review.llm_endpoint_url, review.llm_api_key, review.llm_model)):
        return None
    try:
        return OpenAICompatibleStructuredLlmProvider(
            endpoint_url=review.llm_endpoint_url,
            api_key=review.llm_api_key,
            configured_model=review.llm_model,
        )
    except ValueError:
        return None


def _malware_scanners(
    review: ArtifactReviewSettings,
    *,
    enabled: bool,
) -> tuple[ClamAvScanner | None, YaraScanner | None]:
    if not enabled:
        return None, None

    clamav: ClamAvScanner | None = None
    if review.clamav_enabled:
        try:
            clamav = ClamdInstreamScanner(
                host=review.clamav_host,
                port=review.clamav_port,
                config_ref=review.clamav_config_ref,
            )
        except ValueError:
            clamav = UnavailableClamAvScanner("clamav_configuration_invalid")

    yara: YaraScanner | None = None
    if review.yara_enabled:
        try:
            snapshot = YaraRulesetSnapshot.load(
                version=review.yara_ruleset_version,
                path=Path(review.yara_ruleset_path),
                source=review.yara_ruleset_source,
                activated_at=review.yara_ruleset_activated_at,
            )
            yara = YaraSubprocessScanner(snapshot)
        except (OSError, ValueError):
            yara = UnavailableYaraScanner("yara_ruleset_unavailable")
    return clamav, yara


def _dependency_provider(
    review: ArtifactReviewSettings,
    *,
    enabled: bool,
) -> DependencyAdvisoryProvider | None:
    if not enabled or not review.dependency_enabled:
        return None
    if review.dependency_advisory_path:
        try:
            return LocalDependencyAdvisoryProvider.from_file(
                review.dependency_advisory_path,
                config_ref=review.dependency_config_ref,
            )
        except (OSError, ValueError):
            return UnavailableDependencyAdvisoryProvider(
                "dependency_advisory_snapshot_unavailable",
                config_ref=review.dependency_config_ref,
            )
    if review.dependency_advisory_url:
        try:
            return HttpsDependencyAdvisoryProvider(
                review.dependency_advisory_url,
                api_token=review.dependency_api_token,
                config_ref=review.dependency_config_ref,
            )
        except ValueError:
            return UnavailableDependencyAdvisoryProvider(
                "dependency_advisory_configuration_invalid",
                config_ref=review.dependency_config_ref,
            )
    return UnavailableDependencyAdvisoryProvider(
        "dependency_advisory_source_missing",
        config_ref=review.dependency_config_ref,
    )


def _finalize_review_status(
    review: dict[str, object],
    *,
    policy: dict[str, object],
    policy_model: ReviewPolicyV1 | None,
    tool_health: dict[str, dict[str, object]],
    config: ArtifactSettings,
) -> dict[str, object]:
    components = dict(review.get("components") or {})
    components["policy"] = policy
    if policy_model:
        policy_tools = {
            "runtime": bool(policy_model.runtime_targets),
            "llm": policy_model.llm.enabled or policy_model.category.enabled,
            "clamav": policy_model.malware.clamav,
            "yara": bool(policy_model.malware.yara_ruleset),
            "dependency": policy_model.dependency.enabled,
        }
        configured = config.review.component_configuration()
        reference_errors = {
            "llm": [
                code
                for enabled, reference, code in (
                    (
                        policy_model.llm.enabled,
                        policy_model.llm.provider_config_ref,
                        "llm_config_ref_mismatch",
                    ),
                    (
                        policy_model.category.enabled,
                        policy_model.category.provider_config_ref,
                        "category_config_ref_mismatch",
                    ),
                )
                if enabled and reference != config.review.llm_config_ref
            ],
            "clamav": (
                []
                if policy_model.malware.clamav_config_ref == config.review.clamav_config_ref
                else ["clamav_config_ref_mismatch"]
            ),
            "yara": (
                []
                if not policy_model.malware.yara_ruleset
                or policy_model.malware.yara_ruleset == config.review.yara_ruleset_version
                else ["yara_ruleset_version_mismatch"]
            ),
            "dependency": (
                []
                if policy_model.dependency.advisory_config_ref
                == config.review.dependency_config_ref
                else ["dependency_config_ref_mismatch"]
            ),
            "runtime": [],
        }
        for name, enabled in policy_tools.items():
            components[name] = _tool_component(
                name,
                enabled=enabled,
                configuration=dict(configured[name]),
                reference_errors=reference_errors[name],
                health=tool_health.get(name),
            )

    enabled_components = [
        component
        for component in components.values()
        if isinstance(component, dict) and component.get("enabled")
    ]
    configured_all = bool(policy.get("ready")) and all(
        bool(component.get("configured")) for component in enabled_components
    )
    ready_all = bool(policy.get("ready")) and all(
        bool(component.get("ready")) for component in enabled_components
    )
    review.update(
        {
            "configured": configured_all,
            "ready": ready_all,
            "degraded": not ready_all,
            "components": components,
            "policy_auto_approve_enabled": bool(policy_model and policy_model.routing.auto_approve),
            "auto_approve_effective": bool(
                policy_model
                and policy_model.routing.auto_approve
                and config.review.auto_approve_enabled
                and ready_all
            ),
        }
    )
    return review


def _tool_component(
    name: str,
    *,
    enabled: bool,
    configuration: dict[str, object],
    reference_errors: list[str],
    health: dict[str, object] | None,
) -> dict[str, object]:
    if not enabled:
        return _health_component(
            enabled=False,
            configured=False,
            ready=False,
            reasons=[],
        )
    reasons = list(configuration.get("reasons") or [])
    if not configuration.get("enabled"):
        reasons = [f"{name}_disabled"]
    reasons.extend(reference_errors)
    configured = bool(configuration.get("configured")) and not reference_errors
    ready = configured and bool((health or {}).get("ready"))
    if configured and health is None:
        reasons = ["health_unknown"]
    elif configured and not ready:
        reason = str((health or {}).get("reason") or "tool_degraded")
        reasons = [reason]
    elif ready:
        reasons = []
    return _health_component(
        enabled=True,
        configured=configured,
        ready=ready,
        reasons=reasons,
    )


def _health_component(
    *,
    enabled: bool,
    configured: bool,
    ready: bool,
    reasons: list[str],
) -> dict[str, object]:
    status = "disabled"
    if enabled:
        status = "ready" if ready else "degraded"
    return {
        "enabled": enabled,
        "configured": configured,
        "ready": ready,
        "degraded": enabled and not ready,
        "status": status,
        "reasons": reasons,
    }


_TOOL_HEARTBEAT_SOURCE = {
    "runtime": ("runtime_runner", "runtime"),
    "llm": ("artifact_worker", "llm"),
    "clamav": ("artifact_worker", "clamav"),
    "yara": ("artifact_worker", "yara"),
    "dependency": ("artifact_worker", "dependency"),
}
_RUN_TYPES = frozenset(item.value for item in ReviewRunType)
_JOB_TYPES = frozenset(item.value for item in JobType)
_JOB_STATUSES = frozenset(item.value for item in JobStatus)
_DECISION_ACTIONS = frozenset(item.value for item in DecisionAction)
_DECISION_SOURCES = frozenset(item.value for item in DecisionSource)


def _resolved_tool_health(
    policy: ReviewPolicyV1 | None,
    heartbeats: list[dict[str, Any]],
    latest_runs: list[dict[str, Any]],
    *,
    heartbeat_error: bool,
    require_freshness: bool,
) -> dict[str, dict[str, object]]:
    result = {
        name: _heartbeat_component_status(
            name,
            heartbeats,
            heartbeat_error=heartbeat_error,
        )
        for name in _TOOL_HEARTBEAT_SOURCE
    }
    if policy is None:
        return result

    enabled = {
        "runtime": bool(policy.runtime_targets),
        "llm": policy.llm.enabled or policy.category.enabled,
        "clamav": policy.malware.clamav,
        "yara": bool(policy.malware.yara_ruleset),
        "dependency": policy.dependency.enabled,
    }
    for name, is_enabled in enabled.items():
        if not is_enabled:
            continue
        latest = _latest_run_for_tool(name, latest_runs)
        if latest and not result[name].get("version"):
            result[name]["version"] = str(
                latest.get("tool_version") or latest.get("ruleset_version") or ""
            )
        data_updated_at = _tool_data_updated_at(name, result[name], latest)
        result[name]["data_updated_at"] = data_updated_at
        freshness = _freshness_status(name, data_updated_at, policy)
        result[name]["freshness"] = freshness
        if require_freshness and freshness in {"unknown", "stale"}:
            result[name]["ready"] = False
            result[name]["reason"] = f"{name}_data_{freshness}"
    return result


def _heartbeat_component_status(
    name: str,
    heartbeats: list[dict[str, Any]],
    *,
    heartbeat_error: bool,
) -> dict[str, object]:
    if heartbeat_error:
        return {
            "ready": False,
            "reason": "health_lookup_failed",
            "version": "",
            "data_updated_at": "",
            "freshness": "unknown",
            "observed_at": "",
        }
    worker_kind, component_name = _TOOL_HEARTBEAT_SOURCE[name]
    rows = [item for item in heartbeats if item.get("worker_kind") == worker_kind]
    live_rows = [item for item in rows if item.get("live") is True]
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in live_rows:
        components = row.get("components")
        component = components.get(component_name) if isinstance(components, Mapping) else None
        if isinstance(component, Mapping):
            candidates.append((row, dict(component)))
    selected = next((item for item in candidates if item[1].get("ready") is True), None)
    if selected is None and candidates:
        selected = candidates[0]
    if selected is None:
        return {
            "ready": False,
            "reason": "health_unknown",
            "detail_reason": f"{name}_heartbeat_stale" if rows else f"{name}_heartbeat_missing",
            "version": "",
            "data_updated_at": "",
            "freshness": "unknown",
            "observed_at": "",
        }
    row, component = selected
    ready = component.get("ready") is True
    return {
        "ready": ready,
        "reason": str(component.get("reason") or ("" if ready else f"{name}_degraded")),
        "version": str(component.get("version") or ""),
        "data_updated_at": str(component.get("data_updated_at") or ""),
        "freshness": "unknown",
        "observed_at": str(row.get("observed_at") or ""),
    }


def _latest_run_for_tool(
    name: str,
    latest_runs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    run_types = {
        "runtime": {"runtime"},
        "llm": {"llm_package", "llm_file", "llm_summary"},
        "clamav": {"clamav"},
        "yara": {"yara"},
        "dependency": {"dependency"},
    }[name]
    matches = [item for item in latest_runs if item.get("type") in run_types]
    matches.sort(
        key=lambda item: (
            _parse_timestamp(item.get("completed_at") or item.get("created_at"))
            or datetime.fromtimestamp(0, UTC)
        ),
        reverse=True,
    )
    return matches[0] if matches else None


def _tool_data_updated_at(
    name: str,
    health: Mapping[str, object],
    latest: Mapping[str, Any] | None,
) -> str:
    candidates = [str(health.get("data_updated_at") or "")]
    coverage = latest.get("coverage") if isinstance(latest, Mapping) else None
    if isinstance(coverage, Mapping):
        field = {
            "clamav": "database_time",
            "yara": "ruleset_activated_at",
            "dependency": "database_generated_at",
        }.get(name)
        if field:
            candidates.append(str(coverage.get(field) or ""))
    parsed = [value for value in (_parse_timestamp(item) for item in candidates) if value]
    if not parsed:
        return ""
    return max(parsed).isoformat().replace("+00:00", "Z")


def _freshness_status(name: str, value: str, policy: ReviewPolicyV1) -> str:
    if name not in {"clamav", "dependency"}:
        return "not_applicable"
    observed = _parse_timestamp(value)
    if observed is None:
        return "unknown"
    now = datetime.now(UTC)
    max_age = (
        policy.malware.max_database_age_hours
        if name == "clamav"
        else policy.dependency.max_data_age_hours
    )
    if observed > now + timedelta(minutes=5):
        return "stale"
    return "current" if now - observed <= timedelta(hours=max_age) else "stale"


def _worker_health_projection(
    heartbeats: list[dict[str, Any]],
    *,
    heartbeat_error: bool,
) -> list[dict[str, Any]]:
    result = []
    for worker_kind in ("artifact_worker", "runtime_runner"):
        rows = [item for item in heartbeats if item.get("worker_kind") == worker_kind]
        live = [item for item in rows if item.get("live") is True]
        observed = [
            value for value in (_parse_timestamp(item.get("observed_at")) for item in rows) if value
        ]
        ready = bool(live) and not heartbeat_error
        result.append(
            {
                "kind": worker_kind,
                "status": "ready" if ready else "degraded",
                "ready": ready,
                "degraded": not ready,
                "live_instances": len(live),
                "stale_instances": len(rows) - len(live),
                "capacity": sum(int(item.get("capacity") or 0) for item in live),
                "active_count": sum(int(item.get("active_count") or 0) for item in live),
                "last_observed_at": (
                    max(observed).isoformat().replace("+00:00", "Z") if observed else None
                ),
                "reasons": (
                    ["health_lookup_failed"]
                    if heartbeat_error
                    else (
                        []
                        if ready
                        else [
                            f"{worker_kind}_heartbeat_stale"
                            if rows
                            else f"{worker_kind}_heartbeat_missing"
                        ]
                    )
                ),
            }
        )
    return result


def _tool_health_projection(
    review: Mapping[str, Any],
    tool_health: Mapping[str, Mapping[str, object]],
    active_policy: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    components = review.get("components") if isinstance(review.get("components"), Mapping) else {}
    result: list[dict[str, Any]] = []
    for name in ("policy", "runtime", "llm", "clamav", "yara", "dependency"):
        component = components.get(name) if isinstance(components, Mapping) else None
        component = (
            dict(component)
            if isinstance(component, Mapping)
            else _health_component(
                enabled=False,
                configured=False,
                ready=False,
                reasons=[],
            )
        )
        health = dict(tool_health.get(name) or {})
        result.append(
            {
                "name": name,
                "enabled": bool(component.get("enabled")),
                "configured": bool(component.get("configured")),
                "ready": bool(component.get("ready")),
                "degraded": bool(component.get("degraded")),
                "status": str(component.get("status") or "disabled"),
                "reasons": [str(item)[:96] for item in component.get("reasons") or []],
                "version": (
                    str(active_policy.get("version") or "")
                    if name == "policy" and active_policy
                    else str(health.get("version") or "")
                ),
                "data_updated_at": str(health.get("data_updated_at") or "") or None,
                "freshness": (
                    "current"
                    if name == "policy" and component.get("ready")
                    else (
                        "not_applicable"
                        if not component.get("enabled")
                        else str(health.get("freshness") or "unknown")
                    )
                ),
                "observed_at": str(health.get("observed_at") or "") or None,
            }
        )
    return result


def _empty_observability_snapshot(window_started_at: datetime) -> dict[str, Any]:
    return {
        "window_started_at": window_started_at,
        "queue": [],
        "stages": [],
        "manual_wait": {
            "waiting_count": 0,
            "average_wait_seconds": 0,
            "max_wait_seconds": 0,
        },
        "routing": [],
        "revoke": [],
    }


def _metrics_projection(
    snapshot: Mapping[str, Any],
    *,
    collected_at: datetime,
    available: bool,
) -> dict[str, Any]:
    queue = [
        {
            "job_type": str(item.get("type")),
            "status": str(item.get("status")),
            "count": max(0, int(item.get("count") or 0)),
        }
        for item in snapshot.get("queue") or []
        if isinstance(item, Mapping)
        and item.get("type") in _JOB_TYPES
        and item.get("status") in _JOB_STATUSES
    ]
    stages = [
        {
            "run_type": str(item.get("type")),
            "sample_count": max(0, int(item.get("sample_count") or 0)),
            "failure_count": max(0, int(item.get("failure_count") or 0)),
            "timeout_count": max(0, int(item.get("timeout_count") or 0)),
            "average_duration_ms": max(0.0, float(item.get("average_duration_ms") or 0)),
            "p95_duration_ms": max(0.0, float(item.get("p95_duration_ms") or 0)),
        }
        for item in snapshot.get("stages") or []
        if isinstance(item, Mapping) and item.get("type") in _RUN_TYPES
    ]
    routing = [
        {
            "action": str(item.get("action")),
            "source": str(item.get("source")),
            "count": max(0, int(item.get("count") or 0)),
        }
        for item in snapshot.get("routing") or []
        if isinstance(item, Mapping)
        and item.get("action") in _DECISION_ACTIONS
        and item.get("source") in _DECISION_SOURCES
    ]
    revoke = [
        {
            "status": str(item.get("status")),
            "count": max(0, int(item.get("count") or 0)),
        }
        for item in snapshot.get("revoke") or []
        if isinstance(item, Mapping) and item.get("status") in _JOB_STATUSES
    ]
    manual = snapshot.get("manual_wait")
    manual = manual if isinstance(manual, Mapping) else {}
    started_at = _parse_timestamp(snapshot.get("window_started_at")) or (
        collected_at - timedelta(hours=24)
    )
    return {
        "available": available,
        "window_started_at": started_at.isoformat().replace("+00:00", "Z"),
        "collected_at": collected_at.isoformat().replace("+00:00", "Z"),
        "queue": queue,
        "stages": stages,
        "manual_wait": {
            "waiting_count": max(0, int(manual.get("waiting_count") or 0)),
            "average_wait_seconds": max(0.0, float(manual.get("average_wait_seconds") or 0)),
            "max_wait_seconds": max(0.0, float(manual.get("max_wait_seconds") or 0)),
        },
        "routing": routing,
        "revoke": revoke,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)
