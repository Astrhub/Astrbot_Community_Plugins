export function pluginDetailPath(plugin: {
  id?: string | number;
  name?: string;
  owner_username?: string;
  owner_github_login?: string;
  canonical_path?: string;
}): string | null {
  const canonical = String(plugin.canonical_path || "").trim();
  if (/^\/plugin\/[^/]+\/[^/]+$/.test(canonical)) return canonical;
  const username = String(plugin.owner_username || plugin.owner_github_login || "").trim();
  const name = String(plugin.name || plugin.id || "").trim();
  if (!username || !name) return null;
  return `/plugin/${encodeURIComponent(username)}/${encodeURIComponent(name)}`;
}
