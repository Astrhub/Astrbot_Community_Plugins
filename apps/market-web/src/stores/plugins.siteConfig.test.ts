// @vitest-environment jsdom
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { SITE_CONFIG_CACHE_KEY } from "@/utils/bootstrapCache";
import { usePluginStore } from "./plugins";

describe("loadSiteConfig setup_required integration", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("propagates setup_required from /v1/site without persisting it to localStorage cache", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          name: "AstrBot 插件市场",
          setup_required: false,
        }),
      }),
    );
    setActivePinia(createPinia());
    const store = usePluginStore();

    const loaded = await store.loadSiteConfig();

    expect(loaded.setup_required).toBe(false);
    expect(store.setupStatus.required).toBe(false);
    const cachedRaw = localStorage.getItem(SITE_CONFIG_CACHE_KEY);
    expect(cachedRaw).toBeTruthy();
    expect(cachedRaw).not.toContain("setup_required");
  });

  it("leaves setup_required undefined when /v1/site omits it so callers fall back safely", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          name: "AstrBot 插件市场",
        }),
      }),
    );
    setActivePinia(createPinia());
    const store = usePluginStore();

    const loaded = await store.loadSiteConfig();

    expect(loaded.setup_required).toBeUndefined();
  });
});
