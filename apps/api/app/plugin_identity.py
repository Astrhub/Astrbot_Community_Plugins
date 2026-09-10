from typing import Any
from urllib.parse import quote


class PluginIdentityConflict(ValueError):
    """An owner/name namespace or repository is already registered."""


def plugin_canonical_path(plugin: dict[str, Any]) -> str:
    username = str(plugin.get("owner_github_login") or "").strip()
    name = str(plugin.get("name") or plugin["id"]).strip()
    if username:
        return f"/plugin/{quote(username, safe='')}/{quote(name, safe='')}"
    return f"/plugin/{quote(str(plugin['id']), safe='')}"
