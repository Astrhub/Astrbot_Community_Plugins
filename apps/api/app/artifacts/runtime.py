from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import ArtifactSettings, Settings
from .archive import ArchivePrechecker
from .github_source import GithubSourceClient
from .jobs import ArtifactJobRunner, worker_id
from .notifications import ArtifactNotificationDispatcher
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
                job_runner = ArtifactJobRunner(
                    repository=repository,
                    storage=storage,
                    prechecker=ArchivePrechecker(self.config),
                    scanner=StaticScanner(),
                    worker_id=identity,
                    lease_seconds=self.config.job_lease_seconds,
                    poll_seconds=self.config.worker_poll_seconds,
                    notification_dispatcher=notifications,
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
