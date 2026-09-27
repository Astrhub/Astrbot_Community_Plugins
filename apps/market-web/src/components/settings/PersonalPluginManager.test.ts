// @vitest-environment jsdom
import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vite-plus/test";
import PersonalPluginManager from "./PersonalPluginManager.vue";
import type { Plugin } from "@/types";

vi.mock("@/composables/useExternalOpenConfirm", () => ({
  useExternalOpenConfirm: () => ({ confirmExternalOpen: vi.fn() }),
}));

const plugin: Plugin = {
  id: "demo",
  name: "astrbot_plugin_demo",
  display_name: "Demo",
  version: "1.0.0",
  logo: "",
  tags: [],
  category: "other",
  stars: 0,
  likes: 0,
  comments_count: 0,
  list_index: 0,
  status: "listed",
  cdn_enabled: true,
};

function render(busyIds: Record<string, string> = {}) {
  return mount(PersonalPluginManager, {
    props: { plugins: [plugin], busyIds },
    global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
  });
}

describe("PersonalPluginManager CDN consent", () => {
  it("emits the chosen setting but waits for a confirmed server state", async () => {
    const wrapper = render();
    const control = wrapper.get('[role="switch"]');
    expect(control.attributes("aria-checked")).toBe("true");
    await control.trigger("click");
    expect(wrapper.emitted("toggleCdn")).toEqual([[{ plugin, enabled: false }]]);
    expect(control.attributes("aria-checked")).toBe("true");
    expect(wrapper.text()).toContain("已上架");
    await wrapper.setProps({ plugins: [{ ...plugin, cdn_enabled: false }] });
    expect(control.attributes("aria-checked")).toBe("false");
    expect(wrapper.text()).toContain("已上架");
    expect(wrapper.text()).toContain("通过前不提供该版本的 CDN 链接");
    wrapper.unmount();
  });

  it("prevents overlapping toggles while a plugin action is pending", async () => {
    const wrapper = render({ demo: "cdn" });
    await wrapper.get('[role="switch"]').trigger("click");
    expect(wrapper.emitted("toggleCdn")).toBeUndefined();
    wrapper.unmount();
  });
});
