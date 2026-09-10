// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { createTestingPinia } from "@pinia/testing";
import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import AnnouncementButton from "./AnnouncementButton.vue";
import { usePluginStore } from "../stores/plugins";

vi.mock("naive-ui", async (importOriginal) => {
  const original = await importOriginal<typeof import("naive-ui")>();
  const { defineComponent, h } = await import("vue");
  return {
    ...original,
    NModal: defineComponent({
      props: { show: Boolean },
      setup:
        (props, { slots }) =>
        () =>
          props.show
            ? h("section", { role: "dialog" }, [slots.default?.(), slots.footer?.()])
            : null,
    }),
  };
});

const announcement = { id: 1, title: "站点更新", body: "欢迎使用插件市场" };

function setup() {
  const pinia = createTestingPinia({ createSpy: vi.fn });
  const store = usePluginStore(pinia);
  vi.mocked(store.loadAnnouncements).mockResolvedValue([]);
  const wrapper = mount(AnnouncementButton, {
    global: {
      plugins: [pinia],
    },
  });
  return { wrapper, store };
}

describe("AnnouncementButton", () => {
  beforeEach(() => {
    localStorage.clear();
    delete window.__ASTRHUB_PRERENDER__;
  });

  it("does not capture a visitor announcement dialog during prerendering", async () => {
    window.__ASTRHUB_PRERENDER__ = true;
    const { wrapper, store } = setup();
    store.announcements = [announcement];
    await flushPromises();
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    expect(wrapper.find('button[aria-label="查看站点公告"]').exists()).toBe(true);
    wrapper.unmount();
    delete window.__ASTRHUB_PRERENDER__;
  });

  it("opens when announcements arrive and remembers them across visits while allowing manual reopening", async () => {
    const first = setup();
    expect(first.wrapper.find("button").exists()).toBe(false);
    first.store.announcements = [announcement];
    await flushPromises();
    expect(first.wrapper.get('[role="dialog"]').text()).toContain(announcement.body);
    await first.wrapper.get('[role="dialog"] button').trigger("click");
    expect(first.wrapper.find('[role="dialog"]').exists()).toBe(false);
    first.wrapper.unmount();

    const second = setup();
    second.store.announcements = [announcement];
    await flushPromises();
    expect(second.wrapper.find('[role="dialog"]').exists()).toBe(false);
    await second.wrapper.get('button[aria-label="查看站点公告"]').trigger("click");
    expect(second.wrapper.get('[role="dialog"]').text()).toContain(announcement.body);
    second.wrapper.unmount();
  });

  it("opens again for edited content or a newly published announcement", async () => {
    const { wrapper, store } = setup();
    store.announcements = [announcement];
    await flushPromises();
    await wrapper.get('[role="dialog"] button').trigger("click");
    store.announcements = [{ ...announcement, body: "更新后的公告" }];
    await flushPromises();
    expect(wrapper.get('[role="dialog"]').text()).toContain("更新后的公告");
    await wrapper.get('[role="dialog"] button').trigger("click");
    store.announcements = [{ ...announcement, id: 2, title: "新公告" }, announcement];
    await flushPromises();
    expect(wrapper.get('[role="dialog"]').text()).toContain("新公告");
    wrapper.unmount();
  });
});
