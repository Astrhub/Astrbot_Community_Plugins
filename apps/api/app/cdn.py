"""Per-plugin CDN consent and durable, version-scoped automatic review requests."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from .artifacts.archive import PrecheckError, normalize_github_repo, normalize_version
from .auth import can_edit_plugin, is_admin


def cdn_enabled(plugin: Mapping[str, Any]) -> bool:
    # Old internal callers lack the field; all HTTP submissions set it explicitly.
    return plugin.get("cdn_enabled", True) is True


class CdnPublicationError(ValueError):
    code = "cdn_disabled_or_changed"

    def __init__(self) -> None:
        super().__init__(self.code)


def publication_payload(plugin: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    if cdn_generation(plugin):
        result["cdn_generation"] = cdn_generation(plugin)
    return result


def check_publication_cdn(plugin: Mapping[str, Any], expected_generation: int) -> None:
    if not cdn_enabled(plugin) or cdn_generation(plugin) != expected_generation:
        raise CdnPublicationError()


def cdn_generation(plugin: Mapping[str, Any]) -> int:
    return int(plugin.get("cdn_generation") or 0)


def version_key(version: Any) -> str:
    try:
        return normalize_version(str(version or ""))
    except PrecheckError:
        return str(version or "").strip()


async def store_call(store: Any, method: str, *args: Any) -> Any:
    value = getattr(store, method)(*args)
    return await value if inspect.isawaitable(value) else value


def cdn_error(code: str, message: str, status: int = 409) -> HTTPException:
    return HTTPException(status, {"code": code, "error": message})


def ensure_cdn_submission_allowed(user: Mapping[str, Any]) -> None:
    try:
        muted_until = datetime.fromisoformat(
            str(user.get("muted_until") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return
    if muted_until.astimezone(UTC) > datetime.now(UTC):
        raise cdn_error("user_muted", "账号禁言期间不能提交插件版本", 403)


class CdnSubscriptions:
    def __init__(self, store: Any) -> None:
        self.store = store
        if not hasattr(store, "_pool"):
            if not hasattr(store, "_cdn_subscriptions"):
                store._cdn_subscriptions = {}
                store._cdn_lock = asyncio.Lock()
            self.rows = store._cdn_subscriptions
            self.lock = store._cdn_lock

    async def get(self, plugin_id: str) -> dict[str, Any] | None:
        if hasattr(self.store, "_pool"):
            row = await self.store._pool().fetchrow(
                "SELECT * FROM plugin_cdn_subscriptions WHERE plugin_id=$1", plugin_id
            )
            return dict(row) if row else None
        return deepcopy(self.rows.get(plugin_id))

    async def configure(
        self,
        plugin_id: str,
        *,
        enabled: bool,
        actor: Mapping[str, Any],
        authorization: Mapping[str, Any] | None = None,
        queue_review: bool = True,
    ) -> dict[str, Any]:
        if enabled:
            ensure_cdn_submission_allowed(actor)
        if hasattr(self.store, "_pool"):
            async with self.store._pool().acquire() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "SELECT * FROM market_plugins WHERE id=$1 FOR UPDATE", plugin_id
                    )
                    plugin = self.store._plugin_from_record(row) if row else None
                    self._check_actor(plugin, actor)
                    old = await connection.fetchrow(
                        "SELECT * FROM plugin_cdn_subscriptions WHERE plugin_id=$1 FOR UPDATE",
                        plugin_id,
                    )
                    values = self._configuration(plugin, old, enabled, authorization, queue_review)
                    if values.get("unchanged"):
                        return plugin
                    await connection.execute(
                        """UPDATE market_plugins SET metadata=metadata || $2::jsonb, updated_at=now()
                           WHERE id=$1""",
                        plugin_id,
                        {"cdn_enabled": enabled, "cdn_generation": values["generation"]},
                    )
                    await connection.execute(
                        """INSERT INTO plugin_cdn_subscriptions
                            (plugin_id,"authorization",generation,requested_version,state)
                            VALUES ($1,$2::jsonb,$3,$4,$5)
                            ON CONFLICT (plugin_id) DO UPDATE SET "authorization"=EXCLUDED."authorization",
                            generation=EXCLUDED.generation, requested_version=EXCLUDED.requested_version,
                            state=EXCLUDED.state, lease_id='', lease_until=NULL,
                            available_at=now(), last_error_code='', updated_at=now()""",
                        plugin_id,
                        values["authorization"],
                        values["generation"],
                        values["requested_version"],
                        values["state"],
                    )
            return await store_call(self.store, "get_plugin", plugin_id)
        async with self.lock:
            plugin = await store_call(self.store, "get_plugin", plugin_id)
            self._check_actor(plugin, actor)
            values = self._configuration(
                plugin, self.rows.get(plugin_id), enabled, authorization, queue_review
            )
            if values.get("unchanged"):
                return plugin
            self.rows[plugin_id] = {
                **values,
                "plugin_id": plugin_id,
                "available_at": datetime.now(UTC),
                "lease_id": "",
                "lease_until": None,
                "last_error_code": "",
            }
            return await store_call(
                self.store,
                "update_plugin_metadata",
                plugin_id,
                {"cdn_enabled": enabled, "cdn_generation": values["generation"]},
            )

    @staticmethod
    def _check_actor(plugin: Mapping[str, Any] | None, actor: Mapping[str, Any]) -> None:
        if not plugin:
            raise cdn_error("plugin_not_found", "插件不存在", 404)
        if not can_edit_plugin(actor, plugin):
            raise cdn_error("plugin_owner_changed", "无权管理此插件的 CDN", 403)

    @staticmethod
    def _configuration(plugin, old, enabled, authorization, queue_review):
        generation = cdn_generation(plugin) + int(enabled != cdn_enabled(plugin))
        proof = dict(authorization or (old or {}).get("authorization") or {})
        proof.pop("auth_reference", None)
        if enabled:
            if not proof or normalize_github_repo(
                str(proof.get("repo") or "")
            ) != normalize_github_repo(plugin["repo"]):
                raise cdn_error(
                    "cdn_authorization_required", "开启 CDN 前需要验证 GitHub 仓库权限", 403
                )
            proof["plugin_owner_user_id"] = str(plugin["owner_user_id"])
        target = version_key(plugin.get("repo_version") or plugin.get("version"))
        scope = ("repo", "id", "owner_id", "user_id", "plugin_owner_user_id")
        same_scope = old and all(
            str(proof.get(key)) == str(old["authorization"].get(key)) for key in scope
        )
        if (
            old
            and enabled
            and cdn_enabled(plugin)
            and same_scope
            and version_key(old["requested_version"]) == target
            and old["state"] in {"queued", "running", "submitted"}
        ):
            return {"unchanged": True}
        if old and enabled and cdn_enabled(plugin) and not same_scope:
            generation += 1
        return {
            "generation": generation,
            "authorization": proof,
            "requested_version": version_key(plugin.get("repo_version") or plugin.get("version")),
            "state": ("queued" if queue_review else "idle") if enabled else "disabled",
            "artifact_id": (old or {}).get("artifact_id"),
        }

    async def queue_version(self, plugin: Mapping[str, Any]) -> None:
        if not cdn_enabled(plugin):
            return
        target = version_key(plugin.get("repo_version") or plugin.get("version"))
        if hasattr(self.store, "_pool"):
            await self.store._pool().execute(
                """UPDATE plugin_cdn_subscriptions s SET requested_version=$2, state='queued',
                    artifact_id=NULL, lease_id='', lease_until=NULL, available_at=now(),
                    last_error_code='', updated_at=now()
                    FROM market_plugins p WHERE s.plugin_id=$1 AND p.id=s.plugin_id
                    AND p.metadata->>'cdn_enabled'='true'
                    AND COALESCE((p.metadata->>'cdn_generation')::int,0)=s.generation
                    AND COALESCE(p.repo_version,'')=$3
                    AND (s.requested_version<>$2 OR s.state='idle')""",
                plugin["id"],
                target,
                plugin.get("repo_version") or "",
            )
            return
        async with self.lock:
            current = await store_call(self.store, "get_plugin", plugin["id"])
            if (
                not current
                or version_key(current.get("repo_version") or current.get("version")) != target
            ):
                return
            row = self.rows.get(plugin["id"])
            if (
                row
                and current
                and cdn_enabled(current)
                and row["generation"] == cdn_generation(current)
            ):
                if row["requested_version"] != target or row["state"] == "idle":
                    row.update(
                        requested_version=target,
                        state="queued",
                        artifact_id=None,
                        lease_id="",
                        lease_until=None,
                        available_at=datetime.now(UTC),
                        last_error_code="",
                    )

    async def claim(self, limit: int) -> list[dict[str, Any]]:
        lease_id = uuid.uuid4().hex
        if hasattr(self.store, "_pool"):
            rows = await self.store._pool().fetch(
                """WITH candidates AS (
                    SELECT s.plugin_id FROM plugin_cdn_subscriptions s JOIN market_plugins p ON p.id=s.plugin_id
                    WHERE s.state IN ('queued','error','running') AND s.available_at<=now()
                    AND (s.lease_until IS NULL OR s.lease_until<now())
                    AND p.metadata->>'cdn_enabled'='true'
                    AND COALESCE((p.metadata->>'cdn_generation')::int,0)=s.generation
                    ORDER BY s.available_at LIMIT $1 FOR UPDATE OF s SKIP LOCKED
                ) UPDATE plugin_cdn_subscriptions s SET state='running', lease_id=$2,
                  lease_until=now()+interval '5 minutes', updated_at=now()
                  FROM candidates c WHERE s.plugin_id=c.plugin_id RETURNING s.*""",
                limit,
                lease_id,
            )
            return [dict(row) for row in rows]
        async with self.lock:
            result = []
            now = datetime.now(UTC)
            for row in self.rows.values():
                if len(result) >= limit:
                    break
                if row["state"] not in {"queued", "error", "running"} or row["available_at"] > now:
                    continue
                if row.get("lease_until") and row["lease_until"] >= now:
                    continue
                plugin = await store_call(self.store, "get_plugin", row["plugin_id"])
                if (
                    not plugin
                    or not cdn_enabled(plugin)
                    or cdn_generation(plugin) != row["generation"]
                ):
                    continue
                row.update(
                    state="running", lease_id=lease_id, lease_until=now + timedelta(minutes=5)
                )
                result.append(deepcopy(row))
            return result

    async def finish(
        self,
        claim: Mapping[str, Any],
        *,
        artifact_id: str | None = None,
        error_code: str = "",
        blocked: bool = False,
    ) -> None:
        state = "blocked" if blocked else ("error" if error_code else "submitted")
        if hasattr(self.store, "_pool"):
            await self.store._pool().execute(
                """UPDATE plugin_cdn_subscriptions SET state=$3, artifact_id=$4,
                    last_error_code=$5, lease_until=NULL, lease_id='',
                    available_at=now()+interval '5 minutes', updated_at=now()
                    WHERE plugin_id=$1 AND lease_id=$2 AND state='running'""",
                claim["plugin_id"],
                claim["lease_id"],
                state,
                artifact_id,
                error_code,
            )
            return
        async with self.lock:
            row = self.rows.get(claim["plugin_id"])
            if row and row["lease_id"] == claim["lease_id"] and row["state"] == "running":
                row.update(
                    state=state,
                    artifact_id=artifact_id,
                    last_error_code=error_code,
                    lease_id="",
                    lease_until=None,
                    available_at=datetime.now(UTC) + timedelta(minutes=5),
                )


async def verify_subscription(
    store: Any,
    plugin: Mapping[str, Any],
    subscription: Mapping[str, Any],
    loader: Callable[[str], Awaitable[Mapping[str, Any]]],
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    if not cdn_enabled(plugin) or cdn_generation(plugin) != subscription["generation"]:
        raise cdn_error("cdn_disabled_or_changed", "CDN 已关闭或设置已变化")
    proof = subscription["authorization"]
    user = await store_call(store, "get_user_by_id", proof.get("user_id"))
    if not user or str(plugin.get("owner_user_id")) != proof.get("plugin_owner_user_id"):
        raise cdn_error("cdn_owner_changed", "CDN 授权的维护者已变化", 403)
    if proof.get("mode") == "site_admin":
        if not is_admin(user):
            raise cdn_error("cdn_role_changed", "CDN 授权人的管理权限已失效", 403)
    elif str(user.get("github_id") or user["id"]) != str(proof.get("github_user_id") or ""):
        raise cdn_error("cdn_identity_changed", "CDN 授权的 GitHub 身份已变化", 403)
    canonical = normalize_github_repo(plugin["repo"])
    if proof.get("repo") != canonical or not proof.get("id") or not proof.get("owner_id"):
        raise cdn_error("cdn_authorization_required", "请重新开启 CDN 以验证仓库权限", 403)
    current = await loader(canonical)
    if (
        current.get("private") is not False
        or current.get("archived") is not False
        or current.get("disabled", False) is not False
    ):
        raise cdn_error("cdn_repository_unavailable", "CDN 仅支持公开且未归档的仓库", 403)
    if (
        str(current.get("id")) != str(proof["id"])
        or str((current.get("owner") or {}).get("id")) != str(proof["owner_id"])
        or "https://github.com/" + str(current.get("full_name") or "") != canonical
    ):
        raise cdn_error("cdn_repository_changed", "仓库身份或归属已变化，需要重新授权", 403)
    return user, current


async def process_cdn_reviews(
    app: Any,
    *,
    repository_loader: Callable[[str], Awaitable[Mapping[str, Any]]],
    source_loader: Callable[[Mapping[str, Any]], Awaitable[Any]],
    limit: int = 2,
) -> int:
    runtime = app.state.artifact_runtime
    if not runtime.available or runtime.service is None:
        return 0
    subscriptions = CdnSubscriptions(app.state.store)
    claims = await subscriptions.claim(limit)

    async def process_claim(claim: Mapping[str, Any]) -> None:
        try:
            async with asyncio.timeout(180):
                plugin = await store_call(app.state.store, "get_plugin", claim["plugin_id"])
                if not plugin:
                    raise cdn_error("plugin_not_found", "插件不存在", 404)
                if (
                    version_key(plugin.get("repo_version") or plugin.get("version"))
                    != claim["requested_version"]
                ):
                    raise cdn_error("cdn_version_changed", "仓库版本已变化，等待新的审查任务")
                user, repository = await verify_subscription(
                    app.state.store, plugin, claim, repository_loader
                )
                ensure_cdn_submission_allowed(user)
                publications = await runtime.repository.list_current_publications([plugin["id"]])
                published = publications.get(plugin["id"], {})
                if (
                    published.get("publication_status") == "published"
                    and version_key(published.get("version")) == claim["requested_version"]
                    and published.get("source_repo") == plugin["repo"]
                ):
                    await subscriptions.finish(claim, artifact_id=published["id"])
                    return
                source = await source_loader(repository)
                actor = {
                    **user,
                    "_repository_authorization": {
                        **claim["authorization"],
                        "mode": "cdn_subscription",
                        "origin": "automatic_cdn_update",
                    },
                }
                artifact = await runtime.service.submit_github(
                    plugin=plugin,
                    user=actor,
                    source_ref=source.requested_ref,
                    resolved_source=source,
                )
                if artifact["review_status"] == "approved" and artifact["publication_status"] in {
                    "unpublished",
                    "publish_failed",
                }:
                    await runtime.repository.enqueue_job(
                        {
                            "artifact_id": artifact["id"],
                            "type": "publish",
                            "payload": {"expected_repo_version": plugin.get("repo_version") or ""},
                            "max_attempts": 5,
                            "idempotency_key": f"cdn-resume:{artifact['id']}:{claim['generation']}",
                        }
                    )
                await subscriptions.finish(claim, artifact_id=artifact["id"])
        except HTTPException as exc:
            code = (
                exc.detail.get("code") if isinstance(exc.detail, dict) else None
            ) or "cdn_review_unavailable"
            await subscriptions.finish(
                claim, error_code=code, blocked=exc.status_code in {400, 403, 404, 409}
            )
        except Exception as exc:
            await subscriptions.finish(
                claim, error_code=str(getattr(exc, "code", "cdn_review_unavailable"))
            )

    await asyncio.gather(*(process_claim(claim) for claim in claims))
    return len(claims)
