from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any

from .category import CategoryProvider, CategorySuggestionService, UnavailableCategoryProvider
from .archive import (
    ArchivePrechecker,
    PrecheckError,
    github_repo_name,
    normalize_version,
)
from .models import JobType, PublicationStatus, ReviewStatus
from .notifications import ArtifactNotificationDispatcher
from .orchestration import ReviewOrchestrator, StageToolSnapshot, review_run_type_for_job
from .policy import ReviewPolicyStage
from .repository import ArtifactRepository
from .stages import (
    CategoryStage,
    PrecheckStage,
    ReviewStage,
    StageContext,
    StageOutcome,
    StaticScanStage,
    RoutingStage,
)
from .static_scan import StaticScanner
from .storage import (
    ArtifactStorage,
    ArtifactStorageError,
    build_published_key,
)

LOGGER = logging.getLogger(__name__)


class JobExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ArtifactJobRunner:
    def __init__(
        self,
        *,
        repository: ArtifactRepository,
        storage: ArtifactStorage,
        prechecker: ArchivePrechecker,
        scanner: StaticScanner,
        worker_id: str,
        lease_seconds: int,
        poll_seconds: int,
        notification_dispatcher: ArtifactNotificationDispatcher | None = None,
        advanced_review_enabled: bool = False,
        review_stages: Mapping[str, ReviewStage] | None = None,
        review_orchestrator: ReviewOrchestrator | None = None,
        category_provider: CategoryProvider | None = None,
        category_provider_config_ref: str = "config:llm-default",
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.prechecker = prechecker
        self.scanner = scanner
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.notification_dispatcher = notification_dispatcher
        self.advanced_review_enabled = advanced_review_enabled
        self.category_provider = category_provider or UnavailableCategoryProvider()
        tool_snapshots = (
            {ReviewPolicyStage.CATEGORY: StageToolSnapshot(category_provider.version)}
            if category_provider is not None
            else {}
        )
        self.review_orchestrator = review_orchestrator or ReviewOrchestrator(
            repository,
            tool_snapshots=tool_snapshots,
        )
        self._stopping = asyncio.Event()
        default_stages: list[ReviewStage] = [
            PrecheckStage(advanced_review_enabled=advanced_review_enabled),
            StaticScanStage(advanced_review_enabled=advanced_review_enabled),
            RoutingStage(),
        ]
        default_stages.append(
            CategoryStage(
                CategorySuggestionService(self.category_provider),
                provider_config_ref=category_provider_config_ref,
            )
        )
        self._review_stages = (
            dict(review_stages)
            if review_stages is not None
            else {stage.job_type: stage for stage in default_stages}
        )
        self._stage_tools: Mapping[str, object] = {
            "prechecker": prechecker,
            "scanner": scanner,
        }
        self._handlers: dict[str, Callable[[Mapping[str, Any]], Awaitable[None]]] = {
            **{job_type: self._run_review_stage for job_type in self._review_stages},
            JobType.PUBLISH.value: self._run_publish,
            JobType.REVOKE.value: self._run_revoke,
            JobType.CLEANUP_ORPHAN.value: self._run_cleanup_orphan,
        }

    def stop(self) -> None:
        self._stopping.set()

    async def close(self) -> None:
        close = getattr(self.category_provider, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            handled = await self.run_once(limit=4)
            if handled:
                continue
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def run_once(self, *, limit: int = 1) -> int:
        jobs = await self.repository.claim_jobs(
            self.worker_id,
            limit,
            self.lease_seconds,
        )
        for job in jobs:
            await self._execute(job)
        delivered = 0
        if self.notification_dispatcher is not None:
            delivered = await self.notification_dispatcher.run_once(limit=10)
        return len(jobs) if jobs else delivered

    def rebind_store(self, store: Any) -> None:
        if self.notification_dispatcher is not None:
            self.notification_dispatcher.rebind_store(store)

    def configure_advanced_review(self, enabled: bool) -> None:
        self.advanced_review_enabled = bool(enabled)
        for stage in self._review_stages.values():
            if hasattr(stage, "advanced_review_enabled"):
                stage.advanced_review_enabled = bool(enabled)

    async def _execute(self, job: Mapping[str, Any]) -> None:
        job_id = str(job["id"])
        handler = self._handlers.get(str(job.get("type") or ""))
        if not handler:
            await self.repository.fail_job(
                job_id,
                self.worker_id,
                error_code="unsupported_job_type",
                error_message="Unsupported artifact job type",
                retry=False,
            )
            return

        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._renew_lease(job_id, heartbeat_stop))
        try:
            await handler(job)
        except JobExecutionError as exc:
            await self._fail_job(job, exc.code, exc, retry=exc.retryable)
        except ArtifactStorageError as exc:
            retryable = exc.code in {"quarantine_object_missing"}
            await self._fail_job(job, exc.code, exc, retry=retryable)
        except Exception as exc:
            LOGGER.exception("Artifact job %s failed", job_id)
            await self._fail_job(job, "artifact_job_failed", exc, retry=True)
        else:
            await self.repository.complete_job(job_id, self.worker_id)
        finally:
            heartbeat_stop.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _fail_job(
        self,
        job: Mapping[str, Any],
        error_code: str,
        error: Exception,
        *,
        retry: bool,
    ) -> None:
        attempts = int(job.get("attempts") or 1)
        will_retry = retry and attempts < int(job.get("max_attempts") or 1)
        await self._record_stage_failure(job, error_code, error, will_retry=will_retry)
        await self.repository.fail_job(
            str(job["id"]),
            self.worker_id,
            error_code=error_code,
            error_message=_safe_error(error),
            retry=retry,
            retry_delay_seconds=_retry_delay(attempts),
        )

    async def _record_stage_failure(
        self,
        job: Mapping[str, Any],
        error_code: str,
        error: Exception,
        *,
        will_retry: bool,
    ) -> None:
        artifact_id = str(job.get("artifact_id") or "")
        job_type = str(job.get("type") or "")
        if not artifact_id:
            return
        run_type = review_run_type_for_job(job)
        if run_type:
            await self.repository.fail_open_review_runs(
                artifact_id,
                run_type,
                error_code=error_code,
                summary=_safe_error(error),
            )
            if will_retry:
                return
            artifact = await self.repository.get_artifact(artifact_id)
            if artifact and artifact["review_status"] in {
                ReviewStatus.PRECHECKING.value,
                ReviewStatus.SCANNING.value,
            }:
                failed = await self.repository.transition_review_status(
                    artifact_id,
                    ReviewStatus.PROCESSING_FAILED.value,
                    rejection_code=error_code,
                )
                if failed:
                    await self._status_event(
                        failed,
                        "artifact_processing_failed",
                        f"processing-failed:{error_code}",
                        {"code": error_code},
                    )
            return
        if job_type == JobType.PUBLISH.value:
            artifact = await self.repository.get_artifact(artifact_id)
            if artifact and artifact["publication_status"] == PublicationStatus.PUBLISHING.value:
                await self._mark_publish_failed(artifact, error_code)
            return
        if job_type == JobType.REVOKE.value and not will_retry:
            artifact = await self.repository.get_artifact(artifact_id)
            if artifact and artifact["publication_status"] == PublicationStatus.REVOKING.value:
                with suppress(Exception):
                    await self.repository.transition_publication_status(
                        artifact_id, PublicationStatus.REVOKE_FAILED.value
                    )

    async def _renew_lease(self, job_id: str, stop: asyncio.Event) -> None:
        interval = max(10, self.lease_seconds // 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                if not await self.repository.renew_job_lease(
                    job_id, self.worker_id, self.lease_seconds
                ):
                    return

    async def _run_review_stage(self, job: Mapping[str, Any]) -> None:
        job_type = str(job.get("type") or "")
        stage = self._review_stages.get(job_type)
        if stage is None:
            raise JobExecutionError(
                "unsupported_review_stage",
                "Unsupported artifact review stage",
                retryable=False,
            )
        if int(job.get("attempts") or 1) > 1:
            run_type = review_run_type_for_job(job)
            if run_type:
                await self.repository.fail_open_review_runs(
                    str(job.get("artifact_id") or ""),
                    run_type,
                    error_code="stage_worker_recovered",
                    summary="Previous stage attempt lost its worker lease",
                )
        artifact = await self._artifact_for_job(job)
        policy = None
        policy_version_id = str(artifact.get("policy_version_id") or "")
        if policy_version_id:
            policy = await self.repository.get_review_policy(policy_version_id)
        context = StageContext.create(
            job=job,
            artifact=artifact,
            policy=policy,
            repository=self.repository,
            storage=self.storage,
            tools=self._stage_tools,
            logger=LOGGER,
        )
        try:
            outcome = await stage.execute(context)
            if self.advanced_review_enabled and outcome.completes_job:
                await self.review_orchestrator.reconcile(str(artifact["id"]))
        except ArtifactStorageError as exc:
            if exc.code in {"quarantine_object_missing"}:
                outcome = StageOutcome.retryable_failure(exc.code, _safe_error(exc))
            else:
                outcome = StageOutcome.terminal_failure(exc.code, _safe_error(exc))
        except Exception as exc:
            LOGGER.exception(
                "Artifact review stage %s failed for artifact %s",
                job_type,
                artifact["id"],
            )
            outcome = StageOutcome.retryable_failure("artifact_job_failed", _safe_error(exc))

        LOGGER.info(
            "Artifact review stage finished: artifact=%s stage=%s outcome=%s",
            artifact["id"],
            job_type,
            outcome.kind.value,
        )
        if outcome.completes_job:
            return
        raise JobExecutionError(
            outcome.error_code or "artifact_stage_failed",
            outcome.summary,
            retryable=outcome.retryable,
        )

    async def _run_publish(self, job: Mapping[str, Any]) -> None:
        artifact = await self._artifact_for_job(job)
        if artifact["review_status"] != ReviewStatus.APPROVED.value:
            raise JobExecutionError(
                "artifact_not_approved", "Artifact is not approved", retryable=False
            )
        current_publication = str(artifact["publication_status"])
        expected_repo_version = str((job.get("payload") or {}).get("expected_repo_version") or "")
        current_repo_version = str(artifact.get("repo_version") or "")
        if expected_repo_version != current_repo_version:
            await self._mark_publish_failed(artifact, "repo_version_changed")
            raise JobExecutionError(
                "repo_version_changed", "Repository version changed before publish", retryable=False
            )
        try:
            normalized_repo_version = normalize_version(current_repo_version)
        except PrecheckError as exc:
            await self._mark_publish_failed(artifact, exc.code)
            raise JobExecutionError(exc.code, str(exc), retryable=False) from exc
        if normalized_repo_version != str(artifact["normalized_version"]):
            await self._mark_publish_failed(artifact, "repo_version_changed")
            raise JobExecutionError(
                "repo_version_changed",
                "Artifact version does not match repository",
                retryable=False,
            )
        try:
            published_key = build_published_key(
                author_id=str(artifact.get("owner_user_id") or ""),
                repo_name=github_repo_name(str(artifact["source_repo"])),
                version=str(artifact["version"]),
                plugin_name=str(artifact.get("plugin_name") or artifact["plugin_id"]),
                suffix=str(artifact["path_suffix"]),
            )
        except (ArtifactStorageError, PrecheckError) as exc:
            code = getattr(exc, "code", "published_key_invalid")
            await self._mark_publish_failed(artifact, code)
            raise JobExecutionError(code, str(exc), retryable=False) from exc
        if current_publication == PublicationStatus.PUBLISHED.value:
            if str(artifact.get("published_key") or "") != published_key:
                raise JobExecutionError(
                    "published_key_changed",
                    "Published artifact points to a different object",
                    retryable=False,
                )
            existing = await self.storage.stat_published(published_key)
            if (
                existing is None
                or existing.sha256 != str(artifact["archive_sha256"])
                or existing.size_bytes != int(artifact["size_bytes"])
            ):
                raise JobExecutionError(
                    "published_object_inconsistent",
                    "Published object is missing or does not match the reviewed artifact",
                    retryable=False,
                )
            await self._status_event(artifact, "artifact_published", "published")
            return
        if current_publication not in {
            PublicationStatus.UNPUBLISHED.value,
            PublicationStatus.PUBLISH_FAILED.value,
            PublicationStatus.PUBLISHING.value,
        }:
            raise JobExecutionError(
                "artifact_not_publishable",
                "Artifact publication state does not allow publishing",
                retryable=False,
            )
        if current_publication != PublicationStatus.PUBLISHING.value:
            await self.repository.transition_publication_status(
                artifact["id"], PublicationStatus.PUBLISHING.value
            )
        try:
            published = await self.storage.publish_if_absent(
                str(artifact["quarantine_key"]),
                published_key,
                str(artifact["archive_sha256"]),
            )
            if published.size_bytes != int(artifact["size_bytes"]):
                raise ArtifactStorageError(
                    "published_size_mismatch", "Published object size does not match artifact"
                )
            result = await self.repository.publish_artifact(
                artifact["id"],
                expected_repo_version=current_repo_version,
                published_key=published_key,
                download_url=self.storage.public_url(published_key),
            )
            if result is None:
                raise JobExecutionError(
                    "artifact_not_found", "Artifact disappeared during publish", retryable=False
                )
        except Exception as exc:
            error_code = str(getattr(exc, "code", "publish_failed"))
            await self._mark_publish_failed(artifact, error_code)
            await self.repository.enqueue_job(
                {
                    "artifact_id": artifact["id"],
                    "type": JobType.CLEANUP_ORPHAN.value,
                    "payload": {"published_key": published_key},
                    "max_attempts": 5,
                    "idempotency_key": f"cleanup-orphan:{artifact['id']}:{published_key}",
                }
            )
            raise
        await self._status_event(result, "artifact_published", "published")

    async def _run_revoke(self, job: Mapping[str, Any]) -> None:
        artifact = await self._artifact_for_job(job)
        current_publication = str(artifact["publication_status"])
        reason = str((job.get("payload") or {}).get("reason") or "")
        if current_publication == PublicationStatus.REVOKED.value:
            await self._status_event(
                artifact,
                "artifact_revoked",
                "revoked",
                {"reason": reason},
            )
            return
        if current_publication not in {
            PublicationStatus.PUBLISHED.value,
            PublicationStatus.REVOKING.value,
            PublicationStatus.REVOKE_FAILED.value,
        }:
            raise JobExecutionError(
                "artifact_not_revocable",
                "Artifact publication state does not allow revocation",
                retryable=False,
            )
        if current_publication != PublicationStatus.REVOKING.value:
            await self.repository.transition_publication_status(
                artifact["id"], PublicationStatus.REVOKING.value
            )
        published_key = str(artifact.get("published_key") or "")
        if published_key:
            await self.storage.revoke_published(published_key)
        revoked = await self.repository.revoke_artifact(str(artifact["id"]))
        if revoked is None:
            raise JobExecutionError(
                "artifact_not_found", "Artifact disappeared during revocation", retryable=False
            )
        await self._status_event(
            revoked,
            "artifact_revoked",
            "revoked",
            {"reason": reason},
        )

    async def _run_cleanup_orphan(self, job: Mapping[str, Any]) -> None:
        key = str((job.get("payload") or {}).get("published_key") or "")
        if not key:
            raise JobExecutionError(
                "published_key_missing", "Cleanup job has no object key", retryable=False
            )
        artifact_id = str(job.get("artifact_id") or "")
        if artifact_id:
            artifact = await self.repository.get_artifact(artifact_id)
            if (
                artifact
                and artifact.get("publication_status") == PublicationStatus.PUBLISHED.value
                and artifact.get("published_key") == key
            ):
                LOGGER.info(
                    "Skip orphan cleanup for the currently published object: artifact=%s",
                    artifact_id,
                )
                return
        await self.storage.revoke_published(key)

    async def _artifact_for_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        artifact_id = str(job.get("artifact_id") or "")
        artifact = await self.repository.get_artifact(artifact_id)
        if not artifact:
            raise JobExecutionError(
                "artifact_not_found", "Artifact does not exist", retryable=False
            )
        return artifact

    async def _mark_publish_failed(self, artifact: Mapping[str, Any], error_code: str) -> None:
        with suppress(Exception):
            await self.repository.transition_publication_status(
                str(artifact["id"]), PublicationStatus.PUBLISH_FAILED.value
            )
        await self._status_event(
            artifact,
            "artifact_publish_failed",
            f"publish-failed:{error_code}",
            {"code": error_code},
        )

    async def _status_event(
        self,
        artifact: Mapping[str, Any],
        event_type: str,
        suffix: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        await self.repository.enqueue_outbox(
            {
                "event_type": event_type,
                "aggregate_type": "artifact",
                "aggregate_id": artifact["id"],
                "recipient_user_id": artifact.get("submitted_by"),
                "payload": {
                    "artifact_id": artifact["id"],
                    "plugin_id": artifact["plugin_id"],
                    **dict(extra or {}),
                },
                "dedupe_key": f"artifact:{artifact['id']}:{suffix}",
            }
        )


def _retry_delay(attempt: int) -> int:
    return min(300, max(1, 2 ** min(attempt, 8)))


def _safe_error(error: Exception) -> str:
    return " ".join(str(error or error.__class__.__name__).split())[:500]


def worker_id() -> str:
    return f"artifact-worker-{secrets.token_hex(6)}"
