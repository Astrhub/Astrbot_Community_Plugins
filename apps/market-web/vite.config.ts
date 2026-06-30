import { defineConfig } from "vite-plus";
import vue from "@vitejs/plugin-vue";
import { execSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import sitemap from "vite-plugin-sitemap";
import externalSitemaps from "./sitemaps.config";

type VitePlusConfig = Parameters<typeof defineConfig>[0];

const baseUrl = process.env.VITE_BASE_URL;
const communityRepoUrl = process.env.VITE_COMMUNITY_REPO_URL || readGitRemoteUrl();
const plugins = [vue()];

if (baseUrl) {
  plugins.push(
    sitemap({
      hostname: baseUrl,
      dynamicRoutes: ["/submit"],
      externalSitemaps,
      generateRobotsTxt: true,
      readable: true,
    }),
  );
}

// vite-plus 的 UserConfig 类型递归较深，tsgo 直接比较会触发 excessive stack depth；
// 此处经由 unknown 断言跳过深度比较（库类型边界，非外部输入）。
const config = {
  fmt: {},
  lint: {
    jsPlugins: [{ name: "vite-plus", specifier: "vite-plus/oxlint-plugin" }],
    rules: { "vite-plus/prefer-vite-plus-imports": "error" },
    options: { typeAware: true, typeCheck: true },
  },
  staged: {
    "**/*.{js,ts,tsx,vue,svelte,css,md}": "vp check --fix",
  },
  plugins,
  define: {
    "import.meta.env.VITE_COMMUNITY_REPO_URL": JSON.stringify(communityRepoUrl),
  },
  base: "/",
  assetsInclude: ["**/*.md"],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    chunkSizeWarningLimit: 1000,
  },
  server: {
    host: "0.0.0.0",
    port: 3000,
  },
} as unknown as VitePlusConfig;

export default defineConfig(config);

function readGitRemoteUrl(): string {
  try {
    return normalizeGitRemoteUrl(
      execSync("git config --get remote.origin.url", {
        cwd: fileURLToPath(new URL("../..", import.meta.url)),
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }),
    );
  } catch {
    return "https://github.com/Astrhub/Astrbot_Community_Plugins";
  }
}

function normalizeGitRemoteUrl(value: string): string {
  const remoteUrl = value.trim().replace(/\.git$/, "");
  const sshMatch = remoteUrl.match(/^git@github\.com:(.+)$/);
  if (sshMatch) return `https://github.com/${sshMatch[1]}`;
  return remoteUrl || "https://github.com/Astrhub/Astrbot_Community_Plugins";
}
