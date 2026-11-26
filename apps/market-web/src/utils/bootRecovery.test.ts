// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInThisContext } from "node:vm";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import {
  CHUNK_RELOAD_COOLDOWN_MS,
  CHUNK_RELOAD_STORAGE_KEY,
  handleChunkLoadFailure,
  isChunkLoadError,
} from "./bootRecovery";

const indexHtml = readFileSync(resolve(process.cwd(), "index.html"), "utf8");

function extractInlineBootScript(html: string): string {
  const match = html.match(/<script data-cfasync="false">([\s\S]*?)<\/script>/);
  if (!match?.[1]) throw new Error("Inline boot script missing from index.html");
  return match[1];
}

function runInlineBootScript(html: string): void {
  runInThisContext(extractInlineBootScript(html));
}

describe("bootRecovery", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    document.documentElement.className = "";
    document.head.innerHTML = "";
    document.body.innerHTML = "";
    delete window.__showBootError;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("identifies dynamic module and CSS preload errors without matching ordinary errors", () => {
    expect(isChunkLoadError(new TypeError("Failed to fetch dynamically imported module"))).toBe(
      true,
    );
    expect(isChunkLoadError(new Error("Importing a module script failed."))).toBe(true);
    expect(isChunkLoadError(new Error("Unable to preload CSS for /assets/foo.css"))).toBe(true);
    expect(isChunkLoadError(new Error("Network timeout"))).toBe(false);
    expect(isChunkLoadError(null)).toBe(false);
  });

  it("reloads at most once within cooldown and falls back to the boot error screen", () => {
    const reload = vi.fn();
    const showBootError = vi.fn();
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    window.__showBootError = showBootError;

    const first = handleChunkLoadFailure(new Error("Failed to fetch dynamically imported module"), {
      now: () => 10_000,
      reload,
    });
    expect(first).toBe("reloaded");
    expect(reload).toHaveBeenCalledTimes(1);
    expect(showBootError).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(CHUNK_RELOAD_STORAGE_KEY)).toBe("10000");

    const second = handleChunkLoadFailure(
      new Error("Failed to fetch dynamically imported module"),
      {
        now: () => 10_000 + CHUNK_RELOAD_COOLDOWN_MS - 1,
        reload,
      },
    );
    expect(second).toBe("error_screen");
    expect(reload).toHaveBeenCalledTimes(1);
    expect(showBootError).toHaveBeenCalledTimes(1);
    expect(consoleError).toHaveBeenCalledTimes(2);
  });

  it("renders the inline boot error screen safely with optional contact email and no h1", () => {
    document.head.innerHTML = `
      <meta name="astrhub-repo-url" content="https://github.com/Astrhub/CustomRepo/" />
      <meta name="astrhub-contact-email" content="ops@example.org" />
    `;
    document.body.innerHTML = `
      <div id="app"><main class="ssr-shell"><h1>社区插件市场</h1></main></div>
      <div id="boot-overlay" class="boot-overlay" aria-hidden="true">
        <div class="boot-loading-dots"><span></span><span></span><span></span></div>
      </div>
    `;

    // Execute the exact inline boot script from index.html in the jsdom window.
    runInlineBootScript(indexHtml);

    expect(document.documentElement.classList.contains("js")).toBe(true);
    expect(typeof window.__showBootError).toBe("function");

    window.__showBootError?.();
    const overlay = document.getElementById("boot-overlay");
    expect(overlay?.classList.contains("is-error")).toBe(true);
    expect(overlay?.getAttribute("role")).toBe("alert");
    expect(overlay?.hasAttribute("aria-hidden")).toBe(false);
    expect(overlay?.querySelector("h1")).toBeNull();
    expect(overlay?.textContent).toContain("页面暂时无法加载");
    expect(overlay?.querySelector("a")?.getAttribute("href")).toBe(
      "https://github.com/Astrhub/CustomRepo/issues",
    );
    const mailLink = overlay?.querySelector('a[href^="mailto:"]');
    expect(mailLink?.getAttribute("href")).toBe("mailto:ops@example.org");
    expect(mailLink?.textContent).toBe("ops@example.org");

    // Idempotent second call does not duplicate the card.
    window.__showBootError?.();
    expect(overlay?.querySelectorAll(".boot-error-card")).toHaveLength(1);
  });

  it("omits contact email row when neither meta nor cached site config provides a valid email", () => {
    document.head.innerHTML =
      '<meta name="astrhub-repo-url" content="https://github.com/Astrhub/Astrbot_Community_Plugins" />';
    document.body.innerHTML = `
      <div id="app"></div>
      <div id="boot-overlay" class="boot-overlay" aria-hidden="true">
        <div class="boot-loading-dots"><span></span><span></span><span></span></div>
      </div>
    `;

    runInlineBootScript(indexHtml);
    expect(typeof window.__showBootError).toBe("function");
    window.__showBootError?.();

    const overlay = document.getElementById("boot-overlay");
    expect(overlay?.querySelector(".boot-error-card")).not.toBeNull();
    expect(overlay?.querySelector('a[href^="mailto:"]')).toBeNull();
    expect(overlay?.textContent).not.toContain("联系邮箱");
  });

  it("falls back to cached site config contact email when meta tag is absent", () => {
    localStorage.setItem(
      "astrhub_site_config_v1",
      JSON.stringify({
        version: 1,
        savedAt: Date.now(),
        value: { contact_email: "cached@example.org" },
      }),
    );
    document.head.innerHTML =
      '<meta name="astrhub-repo-url" content="https://github.com/Astrhub/Astrbot_Community_Plugins" />';
    document.body.innerHTML = `
      <div id="app"></div>
      <div id="boot-overlay" class="boot-overlay" aria-hidden="true">
        <div class="boot-loading-dots"><span></span><span></span><span></span></div>
      </div>
    `;

    runInlineBootScript(indexHtml);
    window.__showBootError?.();

    const mailLink = document.querySelector('#boot-overlay a[href^="mailto:"]');
    expect(mailLink?.getAttribute("href")).toBe("mailto:cached@example.org");
    expect(mailLink?.textContent).toBe("cached@example.org");
  });
});
