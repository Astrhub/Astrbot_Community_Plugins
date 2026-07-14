from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from .archive import (
    ArchiveMember,
    ArchivePrechecker,
    PrecheckError,
    github_repo_name,
    normalize_version,
    read_member,
)
from .models import JobType, PublicationStatus, ReviewStatus
from .notifications import ArtifactNotificationDispatcher
from .repository import ArtifactRepository
from .static_scan import RULESET_VERSION, StaticScanner
from .storage import (
    ArtifactStorage,
    ArtifactStorageError,
    build_content_key,
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
        self._stopping = asyncio.Event()
        self._handlers: dict[str, Callable[[Mapping[str, Any]], Awaitable[None]]] = {
            JobType.PRECHECK.value: self._run_precheck,
            JobType.STATIC_SCAN.value: self._run_static_scan,
            JobType.PUBLISH.value: self._run_publish,
            JobType.REVOKE.value: self._run_revoke,
            JobType.CLEANUP_ORPHAN.value: self._run_cleanup_orphan,
        }

    def stop(self) -> None:
        self._stopping.set()

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
        if job_type in {JobType.PRECHECK.value, JobType.STATIC_SCAN.value}:
            run_type = "precheck" if job_type == JobType.PRECHECK.value else "static"
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

    async def _run_precheck(self, job: Mapping[str, Any]) -> None:
        artifact = await self._artifact_for_job(job)
        transitioned = await self.repository.transition_review_status(
            artifact["id"], ReviewStatus.PRECHECKING.value
        )
        if not transitioned:
            raise JobExecutionError(
                "artifact_state_changed",
                "Artifact left precheck state",
                retryable=False,
            )
        artifact = transitioned
        if self.advanced_review_enabled:
            snapshot = await self.repository.snapshot_active_review_policy(str(artifact["id"]))
            if not snapshot or not snapshot.get("policy_version_id"):
                raise JobExecutionError(
                    "review_policy_unavailable",
                    "No validated active review policy is available",
                    retryable=False,
                )
            artifact = snapshot
        run = await self.repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "precheck",
                "status": "running",
                "attempt": int(job.get("attempts") or 1),
                "ruleset_version": "p1.1",
                "policy_version_id": artifact.get("policy_version_id"),
            }
        )
        with tempfile.TemporaryDirectory(prefix="artifact-precheck-") as directory:
            archive_path = Path(directory) / "source.zip"
            downloaded = await self.storage.download_quarantine(
                str(artifact["quarantine_key"]), archive_path
            )
            if downloaded.sha256 != artifact["archive_sha256"]:
                raise JobExecutionError(
                    "sha256_mismatch",
                    "Quarantine artifact digest changed",
                    retryable=False,
                )
            try:
                result = await asyncio.to_thread(
                    self.prechecker.inspect,
                    archive_path,
                    expected_repo=str(artifact["source_repo"]),
                )
            except PrecheckError as exc:
                await self.repository.complete_review_run(
                    run["id"],
                    {
                        "status": "failed",
                        "summary": str(exc),
                        "raw_result": {"code": exc.code, "path": exc.path},
                        "error_code": exc.code,
                    },
                )
                risk = (
                    "critical" if exc.code in {"path_traversal", "zip_bomb_suspected"} else "high"
                )
                rejected = await self.repository.decide_artifact(
                    artifact["id"],
                    action="auto_reject",
                    target_status=ReviewStatus.REJECTED.value,
                    reason=str(exc),
                    reviewer=None,
                    idempotency_key=f"precheck-reject:{artifact['id']}",
                    policy_version_id=artifact.get("policy_version_id"),
                    risk_level=risk,
                    rejection_code=exc.code,
                )
                if rejected:
                    await self._status_event(
                        rejected,
                        "artifact_precheck_failed",
                        "precheck-failed",
                        {"code": exc.code},
                    )
                return

            manifests: list[dict[str, Any]] = []
            for member in result.members:
                file_id = _file_id(str(artifact["id"]), member.path)
                content_key = None
                if member.is_text:
                    content_key = build_content_key(str(artifact["id"]), file_id)
                    content = await asyncio.to_thread(read_member, archive_path, member.source_name)
                    await self.storage.put_text_content(content_key, content)
                manifest = member.as_manifest(content_key=content_key)
                manifest.update(
                    {
                        "id": file_id,
                        "flags": {"source_name": member.source_name},
                    }
                )
                manifests.append(manifest)
            updated = await self.repository.update_artifact_manifest(
                artifact["id"],
                version=result.version,
                normalized_version=result.normalized_version,
                tree_sha256=result.tree_sha256,
            )
            if not updated:
                raise JobExecutionError(
                    "artifact_state_changed",
                    "Artifact left precheck state",
                    retryable=False,
                )
            await self.repository.replace_artifact_files(
                artifact["id"], manifests, result.tree_sha256
            )
            await self.repository.complete_review_run(
                run["id"],
                {
                    "status": "succeeded",
                    "summary": "基础校验通过",
                    "raw_result": {
                        "version": result.version,
                        "normalized_version": result.normalized_version,
                        "file_count": len(result.members),
                        "tree_sha256": result.tree_sha256,
                        "metadata": {
                            key: result.metadata.get(key)
                            for key in (
                                "name",
                                "display_name",
                                "desc",
                                "version",
                                "author",
                                "repo",
                                "astrbot_version",
                            )
                            if key in result.metadata
                        },
                    },
                },
            )
        await self.repository.transition_review_status(artifact["id"], ReviewStatus.SCANNING.value)
        await self.repository.enqueue_job(
            {
                "artifact_id": artifact["id"],
                "type": JobType.STATIC_SCAN.value,
                "max_attempts": 3,
                "idempotency_key": f"static:{artifact['id']}",
                "policy_version_id": artifact.get("policy_version_id"),
            }
        )

    async def _run_static_scan(self, job: Mapping[str, Any]) -> None:
        artifact = await self._artifact_for_job(job)
        if artifact["review_status"] != ReviewStatus.SCANNING.value:
            raise JobExecutionError(
                "artifact_not_scanning",
                "Artifact is not ready for static scan",
                retryable=False,
            )
        if self.advanced_review_enabled and not artifact.get("policy_version_id"):
            raise JobExecutionError(
                "review_policy_unavailable",
                "Artifact has no fixed review policy snapshot",
                retryable=False,
            )
        if job.get("policy_version_id") != artifact.get("policy_version_id"):
            raise JobExecutionError(
                "artifact_policy_snapshot_conflict",
                "Static job policy does not match the artifact snapshot",
                retryable=False,
            )
        run = await self.repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "static",
                "status": "running",
                "attempt": int(job.get("attempts") or 1),
                "ruleset_version": RULESET_VERSION,
                "policy_version_id": artifact.get("policy_version_id"),
            }
        )
        files = await self.repository.list_artifact_files(str(artifact["id"]))
        members = tuple(_member_from_manifest(item) for item in files)
        with tempfile.TemporaryDirectory(prefix="artifact-static-") as directory:
            archive_path = Path(directory) / "source.zip"
            await self.storage.download_quarantine(str(artifact["quarantine_key"]), archive_path)
            findings = await asyncio.to_thread(self.scanner.scan, str(archive_path), members)
        await self.repository.replace_findings(artifact["id"], run["id"], findings)
        risk_level = self.scanner.risk_level(findings)
        await self.repository.complete_review_run(
            run["id"],
            {
                "status": "succeeded",
                "summary": f"静态扫描完成，共 {len(findings)} 条发现",
                "raw_result": {
                    "finding_count": len(findings),
                    "risk_level": risk_level,
                    "ruleset_version": RULESET_VERSION,
                },
            },
        )
        if risk_level == "critical":
            rejected = await self.repository.decide_artifact(
                artifact["id"],
                action="auto_reject",
                target_status=ReviewStatus.REJECTED.value,
                reason="静态扫描发现 critical 风险",
                reviewer=None,
                idempotency_key=f"static-critical-reject:{artifact['id']}",
                policy_version_id=artifact.get("policy_version_id"),
                risk_level=risk_level,
                rejection_code="critical_static_finding",
            )
            if rejected:
                await self._status_event(
                    rejected,
                    "artifact_rejected",
                    "critical-rejected",
                    {"reason": "critical_static_finding"},
                )
            return
        pending = await self.repository.transition_review_status(
            artifact["id"],
            ReviewStatus.PENDING_REVIEW.value,
            risk_level=risk_level,
        )
        if pending:
            await self._status_event(
                pending,
                "artifact_pending_review",
                "pending-review",
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


def _member_from_manifest(item: Mapping[str, Any]) -> ArchiveMember:
    flags = item.get("flags") if isinstance(item.get("flags"), Mapping) else {}
    return ArchiveMember(
        path=str(item["path"]),
        source_name=str(flags.get("source_name") or item["path"]),
        language=str(item.get("language") or ""),
        mime_type=str(item.get("mime_type") or "application/octet-stream"),
        sha256=str(item["sha256"]),
        size_bytes=int(item.get("size_bytes") or 0),
        line_count=item.get("line_count"),
        is_text=bool(item.get("is_text")),
    )


def _file_id(artifact_id: str, path: str) -> str:
    digest = hashlib.sha256(f"{artifact_id}\x00{path}".encode()).hexdigest()[:32]
    return f"file_{digest}"


def _retry_delay(attempt: int) -> int:
    return min(300, max(1, 2 ** min(attempt, 8)))


def _safe_error(error: Exception) -> str:
    return " ".join(str(error or error.__class__.__name__).split())[:500]


def worker_id() -> str:
    return f"artifact-worker-{secrets.token_hex(6)}"
