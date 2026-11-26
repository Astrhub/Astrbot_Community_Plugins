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
  id?: unknown;
  logo?: unknown;
  repo?: unknown;
  repo_provider?: unknown;
}

const OVERSEAS_PROXY_PROVIDERS = new Set<OAuthProviderKey>(["gitlab", "gitea"]);
const OVERSEAS_AVATAR_HOSTS = new Set([
  "gitlab.com",
  "www.gitlab.com",
  "gitea.com",
  "www.gitea.com",
  "secure.gravatar.com",
  "gravatar.com",
  "www.gravatar.com",
]);

export function isOverseasProxyProvider(provider?: string | null): boolean {
  const key = String(provider || "")
    .trim()
    .toLowerCase() as OAuthProviderKey;
  return OVERSEAS_PROXY_PROVIDERS.has(key);
}

export function isOverseasAvatarUrl(value?: string | null): boolean {
  const raw = String(value || "").trim();
  if (!raw) return false;
  try {
    const url = new URL(raw);
    return url.protocol === "https:" && OVERSEAS_AVATAR_HOSTS.has(url.hostname.toLowerCase());
  } catch {
    return false;
  }
}

export function resolveAuthorAvatarUrl(
  avatarUrl: string | null | undefined,
  username: string | null | undefined,
  apiBaseUrl = "",
): string {
  const raw = String(avatarUrl || "").trim();
  if (!raw) return "";
  const cleanUser = String(username || "").trim();
  if (cleanUser && isOverseasAvatarUrl(raw)) {
    return `${apiBaseUrl}/v1/authors/${encodeURIComponent(cleanUser)}/avatar`;
  }
  return githubRawUrl(raw);
}

export function resolveMyAvatarUrl(avatarUrl: string | null | undefined, apiBaseUrl = ""): string {
  const raw = String(avatarUrl || "").trim();
  if (!raw) return "";
  if (isOverseasAvatarUrl(raw)) {
    return `${apiBaseUrl}/v1/me/avatar`;
  }
  return githubRawUrl(raw);
}

const AVATAR_VARIANT_SIZES = new Set([64, 128, 256]);
const AVATAR_VARIANT_KEYS = new Set(["width", "height", "fit", "fallback"]);

/**
 * Build the display URL for an avatar rendered at the given pixel size.
 *
 * Only ImgBed `/file/` URLs support server-side resizing, so they gain width/height/fit=cover/
 * fallback=original and a 44 px header chip downloads a 64 px variant instead of the stored
 * 512 px original. Other URLs (same-origin `/v1/storage/public/`, OAuth proxy, external) are
 * returned unchanged; the API stores uploaded avatar URLs with a `?v=` digest for cache busting.
 */
export function avatarVariantUrl(url: string | null | undefined, size: number): string {
  const raw = String(url || "").trim();
  if (!raw) return "";
  const isRelative = raw.startsWith("/");
  let parsed: URL;
  try {
    parsed = new URL(raw, "https://avatar.invalid");
  } catch {
    return raw;
  }
  if (!parsed.pathname.startsWith("/file/")) return raw;

  const normalizedSize = AVATAR_VARIANT_SIZES.has(size) ? size : 128;
  const params = new URLSearchParams(parsed.search);
  for (const key of AVATAR_VARIANT_KEYS) params.delete(key);
  params.set("width", String(normalizedSize));
  params.set("height", String(normalizedSize));
  params.set("fit", "cover");
  params.set("fallback", "original");
  const query = params.toString();
  const suffix = query ? `?${query}` : "";
  if (isRelative) return parsed.pathname + suffix;
  return `${parsed.protocol}//${parsed.host}${parsed.pathname}${suffix}`;
}

export function resolvePluginLogoUrl(plugin: PluginLogoSource, apiBaseUrl = ""): string {
  const logo = typeof plugin.logo === "string" ? plugin.logo.trim() : "";
  if (!logo) return DEFAULT_PLUGIN_LOGO_URL;
  if (/^(?:https?:\/\/[^/]+)?\/plugin_default\.png(?:\?.*)?$/.test(logo)) {
    return DEFAULT_PLUGIN_LOGO_URL;
  }
  const provider =
    (typeof plugin.repo_provider === "string" ? plugin.repo_provider.trim().toLowerCase() : "") ||
    detectGitProvider(typeof plugin.repo === "string" ? plugin.repo : "");
  const pluginId =
    typeof plugin.id === "string" || typeof plugin.id === "number" ? String(plugin.id).trim() : "";
  if (pluginId && isOverseasProxyProvider(provider)) {
    return `${apiBaseUrl}/v1/plugins/${encodeURIComponent(pluginId)}/logo`;
  }
  return githubRawUrl(logo);
}

export function setDefaultPluginLogo(event: Event): void {
  const image = (event.currentTarget || event.target) as HTMLImageElement | null;
  if (!image || image.getAttribute("src") === DEFAULT_PLUGIN_LOGO_URL) return;
  image.onerror = null;
  image.src = DEFAULT_PLUGIN_LOGO_URL;
}

import type { OAuthProviderKey } from "@/types";

export const GIT_PROVIDER_LABELS: Record<OAuthProviderKey, string> = {
  github: "GitHub",
  gitlab: "GitLab",
  gitee: "Gitee",
  atomgit: "AtomGit",
  gitea: "Gitea",
};

const ALLOWED_GIT_HOSTS: Record<string, OAuthProviderKey> = {
  "github.com": "github",
  "www.github.com": "github",
  "gitlab.com": "gitlab",
  "www.gitlab.com": "gitlab",
  "gitee.com": "gitee",
  "www.gitee.com": "gitee",
  "atomgit.com": "atomgit",
  "www.atomgit.com": "atomgit",
  "gitcode.com": "atomgit",
  "www.gitcode.com": "atomgit",
  "gitea.com": "gitea",
  "www.gitea.com": "gitea",
};

const PATH_SEGMENT_PATTERN = /^[A-Za-z0-9._-]+$/;

export function getGitProviderLabel(provider?: string | null): string {
  const key = String(provider || "")
    .trim()
    .toLowerCase() as OAuthProviderKey;
  return GIT_PROVIDER_LABELS[key] || (key ? key.toUpperCase() : "Git");
}

export function detectGitProvider(value?: string | null): OAuthProviderKey | null {
  const raw = String(value || "").trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.port ||
      url.search ||
      url.hash
    ) {
      return null;
    }
    const provider = ALLOWED_GIT_HOSTS[url.hostname.toLowerCase()];
    if (!provider) return null;
    const segments = url.pathname.split("/").filter(Boolean);
    if (
      segments.some(
        (segment) => segment === "." || segment === ".." || !PATH_SEGMENT_PATTERN.test(segment),
      )
    ) {
      return null;
    }
    if (provider === "gitlab") {
      if (segments.length < 2 || segments.length > 20) return null;
    } else if (segments.length !== 2) {
      return null;
    }
    const repo = segments[segments.length - 1].replace(/\.git$/i, "");
    if (!repo || repo === "." || repo === "..") return null;
    return provider;
  } catch {
    return null;
  }
}

export function isSupportedGitRepoUrl(value?: string | null): boolean {
  return detectGitProvider(value) !== null;
}

export function isGithubRepoUrl(value?: string | null): boolean {
  return detectGitProvider(value) === "github";
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
