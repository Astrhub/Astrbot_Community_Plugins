import { readonly, shallowRef, toValue } from "vue";
import type { MaybeRefOrGetter } from "vue";
import { usePluginStore } from "@/stores/plugins";
import type { Plugin, PluginReadmeContext, PluginReadmeDocument } from "@/types";
import { githubRawUrl, parseGithubRepoUrl } from "@/utils/github";

interface LoadPluginReadmeOptions {
  path?: string;
  refresh?: boolean;
}

const README_CANDIDATES = ["README.md", "Readme.md", "readme.md", "README.MD", "README"];

export function buildReadmeBrowserUrl(context: PluginReadmeContext): string {
  return `https://github.com/${context.owner}/${context.repo}/blob/${context.branch}/${context.path}`;
}

export function usePluginReadme(pluginInput: MaybeRefOrGetter<Plugin | null | undefined>) {
  const store = usePluginStore();
  const loading = shallowRef(false);
  const error = shallowRef<Error | null>(null);
  const document = shallowRef<PluginReadmeDocument | null>(null);

  async function load(options: LoadPluginReadmeOptions = {}): Promise<PluginReadmeDocument> {
    const plugin = toValue(pluginInput);
    if (!plugin?.id || !plugin.repo) throw new Error("插件仓库信息不完整");
    const path = String(options.path || "").trim();
    loading.value = true;
    error.value = null;
    try {
      let result: PluginReadmeDocument;
      try {
        const cached = await store.loadPluginReadme(plugin.id, path, {
          refresh: options.refresh,
        });
        result = {
          ...cached,
          context: readmeContextFromSource(cached.source_url, plugin.repo, path),
        };
      } catch {
        result = await fetchReadmeDirect(plugin.repo, path);
      }
      document.value = result;
      return result;
    } catch (reason) {
      error.value = reason instanceof Error ? reason : new Error("加载 README 失败");
      throw error.value;
    } finally {
      loading.value = false;
    }
  }

  return {
    document: readonly(document),
    error: readonly(error),
    loading: readonly(loading),
    load,
  };
}

async function fetchReadmeDirect(repoUrl: string, path: string): Promise<PluginReadmeDocument> {
  const repoInfo = parseGithubRepoUrl(repoUrl);
  if (!repoInfo) throw new Error("GitHub 仓库地址无效");
  const { owner, repo } = repoInfo;
  const apiPath = path ? `/contents/${encodeGithubPath(path)}` : "/readme";
  try {
    const response = await fetchWithTimeout(
      `https://api.github.com/repos/${owner}/${repo}${apiPath}`,
      { headers: { Accept: "application/vnd.github+json" } },
    );
    if (!response.ok) throw new Error(`GitHub API 返回 ${response.status}`);
    const data = await response.json();
    const content = decodeBase64Content(data.content || "");
    if (!content) throw new Error("README 内容为空");
    const sourceUrl = String(data.download_url || data.html_url || "");
    return {
      content,
      source_url: sourceUrl,
      fetched_at: new Date().toISOString(),
      cached: false,
      context: readmeContextFromSource(sourceUrl, repoUrl, data.path || path),
    };
  } catch {
    return fetchReadmeRawFallback(owner, repo, path);
  }
}

async function fetchReadmeRawFallback(
  owner: string,
  repo: string,
  path: string,
): Promise<PluginReadmeDocument> {
  const candidates = path ? [path] : README_CANDIDATES;
  for (const branch of ["main", "master"]) {
    for (const candidate of candidates) {
      const sourceUrl = `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${candidate}`;
      try {
        const response = await fetchWithTimeout(githubRawUrl(sourceUrl), {
          headers: { Accept: "text/plain" },
        });
        if (!response.ok) continue;
        const content = await response.text();
        if (!content) continue;
        return {
          content,
          source_url: sourceUrl,
          fetched_at: new Date().toISOString(),
          cached: false,
          context: { owner, repo, branch, path: candidate },
        };
      } catch {
        // Try the next branch/file candidate.
      }
    }
  }
  throw new Error("无法获取 README（服务端缓存与 GitHub 直连均失败）");
}

function readmeContextFromSource(
  sourceUrl: string,
  repoUrl: string,
  fallbackPath: string,
): PluginReadmeContext {
  const repoInfo = parseGithubRepoUrl(repoUrl);
  const fallback = {
    owner: repoInfo?.owner || "",
    repo: repoInfo?.repo || "",
    branch: "main",
    path: fallbackPath || "README.md",
  };
  if (!sourceUrl) return fallback;
  try {
    const url = new URL(sourceUrl);
    if (url.hostname === "raw.githubusercontent.com") {
      const [owner, repo, branch, ...parts] = url.pathname.split("/").filter(Boolean);
      return { owner, repo, branch, path: parts.join("/") || fallback.path };
    }
    if (url.hostname === "cdn.jsdelivr.net") {
      const [, owner, repoAndBranch, ...parts] = url.pathname.split("/").filter(Boolean);
      const [repo, branch = "main"] = String(repoAndBranch || "").split("@");
      return { owner, repo, branch, path: parts.join("/") || fallback.path };
    }
    const blobParts = url.pathname.split("/blob/");
    if (url.hostname === "github.com" && blobParts.length === 2) {
      const [owner, repo] = blobParts[0].split("/").filter(Boolean);
      const [branch, ...parts] = blobParts[1].split("/").filter(Boolean);
      return { owner, repo, branch, path: parts.join("/") || fallback.path };
    }
  } catch {
    return fallback;
  }
  return fallback;
}

function decodeBase64Content(value: string): string {
  const normalized = String(value || "").replace(/\s/g, "");
  if (!normalized) return "";
  try {
    return decodeURIComponent(escape(atob(normalized)));
  } catch {
    return atob(normalized);
  }
}

function encodeGithubPath(path: string): string {
  return String(path || "")
    .split("/")
    .filter(Boolean)
    .map((part) => encodeURIComponent(part))
    .join("/");
}

async function fetchWithTimeout(
  url: string,
  options: RequestInit = {},
  timeoutMs = 10_000,
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    window.clearTimeout(timeoutId);
  }
}
