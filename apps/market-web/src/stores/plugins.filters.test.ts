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
  it("does not match internal owner_user_id during public plugin search", () => {
    setActivePinia(createPinia());
    const store = usePluginStore();
    store.plugins = [
      {
        id: "p1",
        name: "astrbot_plugin_weather",
        display_name: "天气查询",
        author: "Alice",
        owner_username: "alice",
        owner_user_id: "usr_internal_secret_999",
        tags: ["工具"],
        category: "utilities",
        stars: 0,
        likes: 0,
        comments_count: 0,
        list_index: 0,
      } as Plugin,
    ];
    store.searchQuery = "usr_internal_secret_999";
    expect(store.filteredPlugins).toHaveLength(0);
    store.searchQuery = "alice";
    expect(store.filteredPlugins).toHaveLength(1);
  });

  it("reuses HTTP cache for default loadPlugins and reloads cache when force is true", async () => {
    setActivePinia(createPinia());
    const fetchMock = vi.fn().mockImplementation(async () => ({
      ok: true,
      json: async () => ({ items: [] }),
    }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const store = usePluginStore();
      await store.loadPlugins();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0]?.[1]).toBeUndefined();

      await store.loadPlugins({ force: true });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock.mock.calls[1]?.[1]).toEqual({ cache: "reload" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("does not coalesce a forced refresh with an in-flight load and ignores stale responses", async () => {
    setActivePinia(createPinia());
    let resolveInitial: ((value: unknown) => void) | undefined;
    let resolveForced: ((value: unknown) => void) | undefined;
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveInitial = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveForced = resolve;
          }),
      );
    vi.stubGlobal("fetch", fetchMock);
    try {
      const store = usePluginStore();
      const initialLoad = store.loadPlugins();
      const coalescedLoad = store.loadPlugins();
      expect(fetchMock).toHaveBeenCalledTimes(1);

      const forcedLoad = store.loadPlugins({ force: true });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock.mock.calls[1]?.[1]).toEqual({ cache: "reload" });

      resolveForced?.({
        ok: true,
        json: async () => ({
          items: [{ id: "fresh", name: "astrbot_plugin_fresh", display_name: "新插件" }],
        }),
      });
      await forcedLoad;
      expect(store.plugins.map((plugin) => plugin.id)).toEqual(["fresh"]);
      expect(store.isLoading).toBe(false);

      resolveInitial?.({
        ok: true,
        json: async () => ({
          items: [{ id: "stale", name: "astrbot_plugin_stale", display_name: "旧插件" }],
        }),
      });
      await Promise.all([initialLoad, coalescedLoad]);
      expect(store.plugins.map((plugin) => plugin.id)).toEqual(["fresh"]);
      expect(store.isLoading).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
