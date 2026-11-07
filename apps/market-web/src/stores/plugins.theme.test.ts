// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { usePluginStore } from "./plugins";

function mockColorSchemeMedia(initialMatches: boolean) {
  let matches = initialMatches;
  const listeners = new Set<() => void>();
  const mediaQueryList = {
    get matches() {
      return matches;
    },
    media: "(prefers-color-scheme: dark)",
    onchange: null,
    addEventListener: vi.fn((event: string, listener: () => void) => {
      if (event === "change") listeners.add(listener);
    }),
    removeEventListener: vi.fn((event: string, listener: () => void) => {
      if (event === "change") listeners.delete(listener);
    }),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };

  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation(() => mediaQueryList),
  );

  return {
    setMatches(next: boolean) {
      matches = next;
      listeners.forEach((listener) => listener());
    },
  };
}

describe("theme preload and dark mode synchronization", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.className = "";
    document.documentElement.style.colorScheme = "";
    vi.unstubAllGlobals();
  });

  it("synchronously resolves browser prefers-color-scheme on store creation before onMounted", () => {
    const media = mockColorSchemeMedia(true);
    setActivePinia(createPinia());

    const store = usePluginStore();

    expect(store.themeMode).toBe("system");
    expect(store.isDarkMode).toBe(true);
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe("dark");

    media.setMatches(false);
    expect(store.isDarkMode).toBe(false);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.style.colorScheme).toBe("light");
  });

  it("respects stored light or dark mode immediately and updates documentElement on setThemeMode", () => {
    mockColorSchemeMedia(true);
    localStorage.setItem("theme-mode", "light");
    setActivePinia(createPinia());

    const store = usePluginStore();

    expect(store.themeMode).toBe("light");
    expect(store.isDarkMode).toBe(false);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.style.colorScheme).toBe("light");

    store.setThemeMode("dark");
    expect(store.themeMode).toBe("dark");
    expect(store.isDarkMode).toBe(true);
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe("dark");
  });

  it("preloads dark class and background in index.html head before script modules run", () => {
    const indexHtml = readFileSync(resolve(__dirname, "../../index.html"), "utf8");

    expect(indexHtml).toContain('localStorage.getItem("theme-mode")');
    expect(indexHtml).toContain('window.matchMedia("(prefers-color-scheme: dark)").matches');
    expect(indexHtml).toContain('document.documentElement.classList.toggle("dark", isDark)');
    expect(indexHtml).toContain("html.dark");
    expect(indexHtml).toContain("background-color: #0f1115;");
  });

  it("defines dark mode semantic aliases and avoids modal-only --n-* text variables in README and notifications", () => {
    const themeCss = readFileSync(resolve(__dirname, "../assets/theme.css"), "utf8");
    const notificationsVue = readFileSync(resolve(__dirname, "../views/Notifications.vue"), "utf8");
    const pluginDetailsVue = readFileSync(
      resolve(__dirname, "../components/PluginDetails.vue"),
      "utf8",
    );
    const pluginCommentVue = readFileSync(
      resolve(__dirname, "../components/PluginComment.vue"),
      "utf8",
    );

    expect(themeCss).toContain("--body-color: var(--bg-base);");
    expect(themeCss).toContain("--card-color: var(--bg-card);");
    expect(themeCss).toContain("--border-color: var(--border-base);");
    expect(themeCss).toContain("--text-color-base: var(--text-primary);");
    expect(themeCss).toContain("--text-color-2: var(--text-secondary);");
    expect(themeCss).toContain("--text-color-3: var(--text-tertiary);");

    expect(notificationsVue).toContain("background: var(--bg-card, var(--card-color));");
    expect(notificationsVue).toContain("color: var(--text-secondary, var(--text-color-2));");

    expect(pluginDetailsVue).toContain("--markdown-heading-color: var(--text-primary, #f4f4f5);");
    expect(pluginDetailsVue).toContain("--markdown-text-color: var(--text-secondary, #d4d4d8);");
    expect(pluginDetailsVue).toContain("--markdown-code-text: #e2e8f0;");
    expect(pluginDetailsVue).not.toContain("color: var(--n-text-color);");
    expect(pluginDetailsVue).not.toContain("color: var(--n-text-color-2);");

    expect(pluginCommentVue).toContain("color: var(--text-primary, var(--n-text-color));");
    expect(pluginCommentVue).toContain("color: var(--text-secondary, var(--n-text-color-2));");
  });
});
