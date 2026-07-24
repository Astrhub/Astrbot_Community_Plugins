// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { NConfigProvider } from "naive-ui";
import { describe, expect, it } from "vite-plus/test";
import SearchToolbar from "./SearchToolbar.vue";

function mountToolbar(props: Record<string, unknown> = {}) {
  return mount(SearchToolbar, {
    props: {
      categoryOptions: [
        { label: "全部分类", value: "all" },
        { label: "实用工具", value: "utilities" },
      ],
      tagOptions: [{ label: "工具", value: "工具" }],
      ...props,
    },
    global: {
      components: { NConfigProvider },
    },
  });
}

describe("SearchToolbar", () => {
  it("keeps the exact and fuzzy modes next to the search input with explicit labels", async () => {
    const wrapper = mountToolbar({ fuzzySearchEnabled: false });
    const cluster = wrapper.get(".search-cluster");
    const exactButton = cluster.get('button[aria-label="使用精确搜索"]');
    const fuzzyButton = cluster.get('button[aria-label="使用模糊搜索"]');

    expect(exactButton.attributes("aria-pressed")).toBe("true");
    expect(fuzzyButton.attributes("aria-pressed")).toBe("false");

    await fuzzyButton.trigger("click");
    expect(wrapper.emitted("update:fuzzySearchEnabled")).toEqual([[true]]);
  });

  it("reuses the direction button as the random refresh action", async () => {
    const wrapper = mountToolbar({ sortBy: "random", sortDirection: "asc" });

    await wrapper.get('button[aria-label="换一批随机推荐"]').trigger("click");
    expect(wrapper.emitted("refreshRandom")).toHaveLength(1);
    expect(wrapper.emitted("update:sortDirection")).toBeUndefined();

    await wrapper.setProps({ sortBy: "stars" });
    await wrapper.get('button[aria-label="切换为倒序"]').trigger("click");
    expect(wrapper.emitted("update:sortDirection")).toEqual([["desc"]]);
  });
});
