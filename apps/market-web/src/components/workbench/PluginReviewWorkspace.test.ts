// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import PluginReviewWorkspace from "./PluginReviewWorkspace.vue";

describe("PluginReviewWorkspace", () => {
  it("provides queue, main, thread, and decision regions with a mobile drawer command", async () => {
    const wrapper = mount(PluginReviewWorkspace, {
      props: { activeView: "comments", drawerOpen: false },
      slots: {
        header: "<div data-region='header'>header</div>",
        sidebar: "<div data-region='sidebar'>sidebar</div>",
        default: "<div data-region='main'>main</div>",
        thread: "<div data-region='thread'>thread</div>",
        decision: "<div data-region='decision'>decision</div>",
      },
      global: { stubs: { teleport: true } },
    });
    await flushPromises();

    expect(wrapper.find('[data-region="header"]').exists()).toBe(true);
    expect(wrapper.find('[data-region="main"]').exists()).toBe(true);
    expect(wrapper.find('[data-region="thread"]').exists()).toBe(true);
    expect(wrapper.find(".review-workspace__thread").classes()).toContain(
      "review-workspace__pane--mobile-active",
    );
    const queueButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("版本队列"));
    expect(queueButton).toBeDefined();
    await queueButton?.trigger("click");
    await flushPromises();
    expect(wrapper.emitted("update:drawerOpen")).toEqual([[true]]);
  });
});
