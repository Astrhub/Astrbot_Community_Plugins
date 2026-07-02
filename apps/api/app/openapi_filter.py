"""Role-based OpenAPI schema filtering.

Filters the full OpenAPI schema by tag visibility rules,
so different user roles see different subsets of the API.
"""

from __future__ import annotations

from typing import Any

from .auth import is_admin, is_core_admin


ROLE_VISIBLE_TAGS: dict[str, frozenset[str]] = {
    "public": frozenset(
        {
            "plugins",
            "submissions",
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
            "submissions",
            "comments",
            "integration",
            "announcements",
            "system",
            "auth",
            "user",
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
            "admin",
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
            "admin",
            "core-admin",
        }
    ),
}

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


def role_for_openapi(user: dict[str, Any] | None) -> str:
    """Map a user object to an OpenAPI visibility role."""
    if not user:
        return "public"
    if is_core_admin(user):
        return "core_admin"
    if is_admin(user):
        return "admin"
    return "user"


def filter_openapi_by_role(schema: dict[str, Any], role: str) -> dict[str, Any]:
    """Return a copy of *schema* with only paths/tags visible to *role*."""
    allowed = ROLE_VISIBLE_TAGS.get(role, ROLE_VISIBLE_TAGS["public"])

    filtered_paths: dict[str, Any] = {}
    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        ops: dict[str, Any] = {}
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS:
                # Keep non-method fields (e.g. parameters, summary at path level).
                ops[method] = operation
                continue
            if not isinstance(operation, dict):
                ops[method] = operation
                continue
            op_tags = set(operation.get("tags") or [])
            if op_tags & allowed:
                ops[method] = operation
        if ops:
            filtered_paths[path] = {**path_item, **ops}

    filtered_tags = [tag for tag in schema.get("tags", []) if tag.get("name") in allowed]

    return {
        **schema,
        "paths": filtered_paths,
        "tags": filtered_tags,
    }
