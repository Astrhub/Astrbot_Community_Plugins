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
