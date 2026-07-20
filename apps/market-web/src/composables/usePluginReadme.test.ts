// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { usePluginStore } from "@/stores/plugins";
import type { Plugin } from "@/types";
import { usePluginReadme } from "./usePluginReadme";

const plugin: Plugin = {
  id: "astrbot_plugin_readme",
  name: "astrbot_plugin_readme",
  display_name: "README",
  version: "1.0.0",
  logo: "",
  repo: "https://github.com/alice/astrbot_plugin_readme",
  tags: [],
  category: "utilities",
  stars: 0,
  likes: 0,
  comments_count: 0,
  list_index: 0,
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("usePluginReadme", () => {
  it("prefers the server cache endpoint and derives repository context", async () => {
    setActivePinia(createPinia());
    const store = usePluginStore();
    const request = vi.spyOn(store, "loadPluginReadme").mockResolvedValue({
      content: "# Cached",
      source_url:
        "https://raw.githubusercontent.com/alice/astrbot_plugin_readme/develop/docs/README.md",
      fetched_at: "2026-07-20T00:00:00Z",
      cached: true,
    });

    const { load } = usePluginReadme(plugin);
    const result = await load();

    expect(request).toHaveBeenCalledWith(plugin.id, "", { refresh: undefined });
    expect(result.cached).toBe(true);
    expect(result.context).toEqual({
      owner: "alice",
      repo: "astrbot_plugin_readme",
      branch: "develop",
      path: "docs/README.md",
    });
  });

  it("falls back to the existing GitHub API flow when the server endpoint fails", async () => {
    setActivePinia(createPinia());
    const store = usePluginStore();
    vi.spyOn(store, "loadPluginReadme").mockRejectedValue(new Error("server unavailable"));
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          content: btoa("# Direct"),
          download_url:
            "https://raw.githubusercontent.com/alice/astrbot_plugin_readme/main/README.md",
          path: "README.md",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { load } = usePluginReadme(plugin);
    const result = await load();

    expect(result.content).toBe("# Direct");
    expect(result.cached).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.github.com/repos/alice/astrbot_plugin_readme/readme",
      expect.objectContaining({ headers: { Accept: "application/vnd.github+json" } }),
    );
  });
});
