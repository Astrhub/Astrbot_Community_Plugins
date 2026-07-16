from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..config import ArtifactReviewSettings, ArtifactSettings, Settings
from .archive import ArchivePrechecker
from .category import OpenAICompatibleCategoryProvider
from .github_source import GithubSourceClient
from .jobs import ArtifactJobRunner, worker_id
from .notifications import ArtifactNotificationDispatcher
from .policy import ReviewPolicyV1, parse_review_policy, review_policy_sha256
from .repository import InMemoryArtifactRepository, PgArtifactRepository
from .service import ArtifactService
from .static_scan import StaticScanner
from .storage import create_artifact_storage


@dataclass(slots=True)
class ArtifactRuntime:
    """Application-owned artifact components without an in-process worker."""

    config: ArtifactSettings
    settings: Settings
    database_url: str
    store: Any
    github_api_token: str = ""
    allow_in_memory_artifacts: bool = False
    started: bool = False
    generation: int = 0
    repository: Any | None = None
    storage: Any | None = None
    service: Any | None = None
    job_runner: Any | None = None
    _component_errors: list[str] = field(default_factory=list)
    _tool_health: dict[str, dict[str, object]] = field(default_factory=dict)

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

    def set_tool_health(
        self,
        name: str,
        *,
        ready: bool,
        reason: str = "",
    ) -> None:
        if name not in {"runtime", "llm", "clamav", "yara", "dependency"}:
            raise ValueError("unsupported_review_tool")
        self._tool_health[name] = {
            "ready": bool(ready),
            "reason": str(reason or "").strip(),
        }

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
        status["review"] = await self._review_health_status()
        return status

    async def _review_health_status(self) -> dict[str, object]:
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
                tool_health=self._tool_health,
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
                tool_health=self._tool_health,
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
                tool_health=self._tool_health,
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
        return _finalize_review_status(
            review,
            policy=policy_component,
            policy_model=policy_model if not reasons else None,
            tool_health=self._tool_health,
            config=self.config,
        )


def build_artifact_runtime(
    settings: Settings,
    store: Any,
    *,
    allow_in_memory_artifacts: bool = True,
) -> ArtifactRuntime:
    """Build the shared runtime shell without starting an in-process worker."""
    return ArtifactRuntime(
        config=settings.artifacts,
        settings=settings,
        database_url=settings.database_url,
        store=store,
        github_api_token=settings.github_api_token,
        allow_in_memory_artifacts=allow_in_memory_artifacts,
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
