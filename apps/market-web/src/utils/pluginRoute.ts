export function pluginDetailPath(plugin: {
  id?: string | number;
  name?: string;
  owner_github_login?: string;
  canonical_path?: string;
}): string {
  if (plugin.canonical_path) return plugin.canonical_path;
  const username = String(plugin.owner_github_login || "").trim();
  return username
    ? `/plugin/${encodeURIComponent(username)}/${encodeURIComponent(plugin.name || String(plugin.id))}`
    : `/plugin/${encodeURIComponent(String(plugin.id))}`;
}
