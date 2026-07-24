import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import puppeteer from "puppeteer-core";

const baseUrl = String(process.env.VITE_BASE_URL || "").replace(/\/$/, "");
if (!baseUrl) {
  console.log("[prerender] VITE_BASE_URL is not set; skipping.");
  process.exit(0);
}

const response = await fetch(`${baseUrl}/v1/plugins`);
if (!response.ok) throw new Error(`[prerender] Plugin list returned HTTP ${response.status}`);
const payload = await response.json();
const pluginRoutes = (payload.items || [])
  .filter((plugin) => !plugin.status || plugin.status === "listed")
  .map((plugin) => String(plugin.id || "").trim())
  .filter(Boolean)
  .map((name) => `/plugin/${encodeURIComponent(name)}`);
const routes = ["/", "/submit", "/docs/rest", ...pluginRoutes];
const port = 4174;
const server = spawn(
  process.execPath,
  [path.resolve("node_modules/sirv-cli/bin.js"), "dist", "--single", "--port", String(port)],
  { stdio: "inherit" },
);

try {
  await waitForServer(`http://127.0.0.1:${port}/`);
  const executablePath = resolveChromiumPath();
  const browser = await puppeteer.launch({
    executablePath,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-web-security"],
    headless: true,
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 1000 });
    let rootHtml = "";
    for (const route of routes) {
      await page.goto(`http://127.0.0.1:${port}${route}`, { waitUntil: "domcontentloaded" });
      await page.waitForSelector("#app > *", { timeout: 20_000 });
      if (route === "/")
        await page.waitForSelector(".plugin-card, .empty-state", { timeout: 20_000 });
      if (route.startsWith("/plugin/"))
        await page.waitForSelector(".plugin-layout", { timeout: 20_000 });
      const html = await page.content();
      if (route === "/") {
        rootHtml = html;
        console.log("[prerender] / captured; writing after route snapshots");
        continue;
      }
      const output = `dist${route}/index.html`;
      await mkdir(path.dirname(output), { recursive: true });
      await writeFile(output, html, "utf8");
      console.log(`[prerender] ${route} -> ${output}`);
    }
    if (!rootHtml) throw new Error("[prerender] Homepage snapshot was not captured");
    await writeFile("dist/index.html", rootHtml, "utf8");
    console.log("[prerender] / -> dist/index.html");
  } finally {
    await browser.close();
  }
} finally {
  server.kill("SIGTERM");
}

async function waitForServer(url) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const ready = await fetch(url);
      if (ready.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("[prerender] sirv did not start in time");
}

function resolveChromiumPath() {
  const windowsCandidates =
    process.platform === "win32"
      ? [
          [process.env.PROGRAMFILES, "Google/Chrome/Application/chrome.exe"],
          [process.env["PROGRAMFILES(X86)"], "Microsoft/Edge/Application/msedge.exe"],
          [process.env.LOCALAPPDATA, "Microsoft/Edge/Application/msedge.exe"],
        ]
          .filter(([base]) => Boolean(base))
          .map(([base, relative]) => path.join(base, relative))
      : [];
  const candidates = [
    process.env.PUPPETEER_EXECUTABLE_PATH,
    ...windowsCandidates,
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
  ].filter(Boolean);
  const executablePath = candidates.find((candidate) => existsSync(candidate));
  if (!executablePath) {
    throw new Error("[prerender] Chromium executable was not found");
  }
  return executablePath;
}
