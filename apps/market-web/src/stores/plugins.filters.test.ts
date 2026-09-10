// @vitest-environment jsdom
import { createPinia, setActivePinia } from "pinia";
import { describe, expect, it, vi } from "vite-plus/test";
import type { Plugin } from "../types";
import { usePluginStore } from "./plugins";

describe("multi-tag filtering", () => {
  it("preserves an explicit display name even if it equals desc and leaves missing names empty", async () => {
    setActivePinia(createPinia());
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          items: [
            { id: "a", name: "astrbot_plugin_a", display_name: "真实名称", desc: "真实名称" },
            { id: "b", name: "astrbot_plugin_b", desc: "描述" },
          ],
        }),
      }),
    );
    try {
      const store = usePluginStore();
      await store.loadPlugins();
      expect(store.plugins.map((plugin) => plugin.display_name)).toEqual(["真实名称", ""]);
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it("matches any chosen tag once and restores all plugins when cleared", () => {
    setActivePinia(createPinia());
    const store = usePluginStore();
    store.plugins = [["工具"], ["娱乐"], ["工具", "娱乐"], ["其他"]].map(
      (tags, index) =>
        ({
          id: String(index),
          name: `astrbot_plugin_${index}`,
          display_name: String(index),
          tags,
          category: "other",
          stars: 0,
          likes: 0,
          comments_count: 0,
          list_index: index,
        }) as Plugin,
    );
    store.currentPage = 3;
    store.setSelectedTags(["工具", "娱乐"]);
    expect(store.currentPage).toBe(1);
    expect(store.filteredPlugins.map((plugin) => plugin.id)).toEqual(["0", "1", "2"]);
    store.setSelectedTags([]);
    expect(store.filteredPlugins).toHaveLength(4);
  });
});
