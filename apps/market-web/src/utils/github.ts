/**
 * GitHub raw URL acceleration for users in China.
 *
 * Detects user timezone: Asia/Shanghai → use jsdelivr CDN mirror.
 * Otherwise → use original raw.githubusercontent.com.
 */

const isChinaUser = (() => {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    return tz === "Asia/Shanghai" || tz === "Asia/Urumqi" || tz === "Asia/Chongqing";
  } catch {
    return false;
  }
})();

/**
 * Rewrite a raw.githubusercontent.com URL to a CDN mirror for China users.
 *
 * raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}
 * → cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}
 *
 * For non-raw URLs, returns the original.
 */
export function githubRawUrl(url: string): string {
  if (!url || typeof url !== "string") return url;
  if (!isChinaUser) return url;

  const match = url.match(
    /^https?:\/\/raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\/([^/]+)\/(.+)$/,
  );
  if (!match) return url;

  const [, owner, repo, branch, path] = match;
  return `https://cdn.jsdelivr.net/gh/${owner}/${repo}@${branch}/${path}`;
}

/**
 * Build a GitHub raw content URL with acceleration applied.
 */
export function buildGithubRawUrl(
  owner: string,
  repo: string,
  branch: string,
  path: string,
): string {
  const raw = `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${path}`;
  return githubRawUrl(raw);
}

export const DEFAULT_PLUGIN_LOGO_URL = "/plugin_default.png?v=20260725";

export interface PluginLogoSource {
  logo?: unknown;
  repo?: unknown;
}

export function resolvePluginLogoUrl(plugin: PluginLogoSource): string {
  const logo = typeof plugin.logo === "string" ? plugin.logo.trim() : "";
  if (!logo) return DEFAULT_PLUGIN_LOGO_URL;
  if (/^(?:https?:\/\/[^/]+)?\/plugin_default\.png(?:\?.*)?$/.test(logo)) {
    return DEFAULT_PLUGIN_LOGO_URL;
  }
  return githubRawUrl(logo);
}

export function setDefaultPluginLogo(event: Event): void {
  const image = (event.currentTarget || event.target) as HTMLImageElement | null;
  if (!image || image.getAttribute("src") === DEFAULT_PLUGIN_LOGO_URL) return;
  image.onerror = null;
  image.src = DEFAULT_PLUGIN_LOGO_URL;
}

export function parseGithubRepoUrl(value: string): { owner: string; repo: string } | null {
  if (!value) return null;

  try {
    const url = new URL(value);
    if (url.hostname.toLowerCase() !== "github.com") return null;
    const [owner, repo] = url.pathname.replace(/^\/+/, "").split("/");
    if (!owner || !repo) return null;
    return { owner, repo: repo.replace(/\.git$/, "") };
  } catch {
    return null;
  }
}
