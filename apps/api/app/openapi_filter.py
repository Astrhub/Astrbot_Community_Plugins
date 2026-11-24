"""Role-based OpenAPI schema filtering across the 5-tier user hierarchy.

Filters the full OpenAPI schema by tag visibility rules,
so guest/user/developer/admin/core_admin see only their permitted API surface.
"""

from __future__ import annotations

from typing import Any

from .auth import is_admin, is_core_admin


ROLE_VISIBLE_TAGS: dict[str, frozenset[str]] = {
    "public": frozenset(
        {
            "plugins",
            "comments",
            "integration",
            "announcements",
            "system",
            "auth",
        }
    ),
    "user": frozenset(
        {
            "plugins",
            "comments",
            "integration",
            "announcements",
            "system",
            "auth",
            "user",
        }
    ),
    "developer": frozenset(
        {
            "plugins",
            "submissions",
            "comments",
            "integration",
            "announcements",
            "system",
            "auth",
            "user",
            "developer",
            "artifacts",
        }
    ),
    "admin": frozenset(
        {
            "plugins",
            "submissions",
            "comments",
            "integration",
            "announcements",
            "system",
            "auth",
            "user",
            "developer",
            "admin",
            "artifacts",
            "reviews",
        }
    ),
    "core_admin": frozenset(
        {
            "plugins",
            "submissions",
            "comments",
            "integration",
            "announcements",
            "system",
            "auth",
            "user",
            "developer",
            "admin",
            "core-admin",
            "artifacts",
            "reviews",
        }
    ),
}

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_COMPONENT_REF_PREFIX = "#/components/"


def _unescape_json_pointer(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _parse_component_ref(ref: str) -> tuple[str, str] | None:
    if not ref.startswith(_COMPONENT_REF_PREFIX):
        return None
    parts = ref[len(_COMPONENT_REF_PREFIX) :].split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], _unescape_json_pointer(parts[1])


def _collect_component_refs(node: Any, out: set[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            parsed = _parse_component_ref(ref)
            if parsed is not None:
                out.add(parsed)
        discriminator = node.get("discriminator")
        if isinstance(discriminator, dict):
            mapping = discriminator.get("mapping")
            if isinstance(mapping, dict):
                for mapped in mapping.values():
                    if not isinstance(mapped, str):
                        continue
                    parsed = _parse_component_ref(mapped)
                    if parsed is not None:
                        out.add(parsed)
                    elif "/" not in mapped and mapped:
                        out.add(("schemas", mapped))
        for value in node.values():
            _collect_component_refs(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_component_refs(item, out)


def _prune_components(components: Any, filtered_paths: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(components, dict):
        return {}
    pending: set[tuple[str, str]] = set()
    _collect_component_refs(filtered_paths, pending)
    reachable: set[tuple[str, str]] = set()
    while pending:
        key = pending.pop()
        if key in reachable:
            continue
        section, name = key
        section_entries = components.get(section)
        if not isinstance(section_entries, dict) or name not in section_entries:
            continue
        reachable.add(key)
        _collect_component_refs(section_entries[name], pending)

    pruned: dict[str, Any] = {}
    for section, entries in components.items():
        if section == "securitySchemes":
            if entries:
                pruned[section] = entries
            continue
        if not isinstance(entries, dict):
            continue
        kept = {name: value for name, value in entries.items() if (section, name) in reachable}
        if kept:
            pruned[section] = kept
    return pruned


def role_for_openapi(
    user: dict[str, Any] | None,
    account_tier: str | None = None,
) -> str:
    """Map a user object or derived account tier to an OpenAPI visibility role."""
    if not user:
        return "public"
    if account_tier:
        normalized = str(account_tier)
        if normalized == "guest":
            return "public"
        if normalized in ROLE_VISIBLE_TAGS:
            return normalized
    tier = str(user.get("account_tier") or "")
    if tier in ROLE_VISIBLE_TAGS:
        return tier
    if is_core_admin(user):
        return "core_admin"
    if is_admin(user):
        return "admin"
    return "user"


def filter_openapi_by_role(schema: dict[str, Any], role: str) -> dict[str, Any]:
    """Return a copy of *schema* with only paths, tags, and referenced components for *role*."""
    allowed = ROLE_VISIBLE_TAGS.get(role, ROLE_VISIBLE_TAGS["public"])

    filtered_paths: dict[str, Any] = {}
    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        path_meta: dict[str, Any] = {}
        visible_ops: dict[str, Any] = {}
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS:
                path_meta[method] = operation
                continue
            if not isinstance(operation, dict):
                visible_ops[method] = operation
                continue
            op_tags = set(operation.get("tags") or [])
            if op_tags & allowed:
                visible_ops[method] = operation
        if visible_ops:
            filtered_paths[path] = {**path_meta, **visible_ops}

    filtered_tags = [tag for tag in schema.get("tags", []) if tag.get("name") in allowed]

    result: dict[str, Any] = {
        **schema,
        "paths": filtered_paths,
        "tags": filtered_tags,
    }
    if "components" in schema:
        pruned_components = _prune_components(schema.get("components"), filtered_paths)
        if pruned_components:
            result["components"] = pruned_components
        else:
            result.pop("components", None)
    return result
