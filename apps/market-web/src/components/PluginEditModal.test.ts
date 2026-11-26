// @vitest-environment jsdom
import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vite-plus/test";
import PluginEditModal from "./PluginEditModal.vue";
import type { Plugin } from "@/types";

vi.mock("naive-ui", async (importOriginal) => {
  const original = await importOriginal<typeof import("naive-ui")>();
  const { defineComponent, h } = await import("vue");
  return {
    ...original,
    NModal: defineComponent({
      name: "NModal",
      props: { show: Boolean },
      setup:
        (props, { slots }) =>
        () =>
          props.show
            ? h("section", { class: "modal-stub", role: "dialog" }, [
                slots.header?.(),
                slots.default?.(),
                slots.footer?.(),
              ])
            : null,
    }),
  };
});

vi.mock("@/composables/useExternalOpenConfirm", () => ({
  useExternalOpenConfirm: () => ({ confirmExternalOpen: vi.fn() }),
}));

const plugin: Plugin = {
  id: "demo",
  name: "astrbot_plugin_demo",
  display_name: "Demo",
  desc: "Initial desc",
  version: "1.0.0",
  logo: "",
  tags: ["demo"],
  category: "utilities",
  social_link: "https://example.com/alice",
  stars: 0,
  likes: 0,
  comments_count: 0,
  list_index: 0,
  status: "listed",
  cdn_enabled: true,
};

function mountModal(propsOverride: Record<string, unknown> = {}) {
  return mount(PluginEditModal, {
    props: {
      show: true,
      plugin,
      ...propsOverride,
    },
    global: {
      stubs: {
        RouterLink: {
          props: ["to"],
          template: '<a class="modal-detail-link" :data-to="to"><slot /></a>',
        },
      },
    },
  });
}

describe("PluginEditModal", () => {
  it("emits CDN toggle and waits for confirmed server state without clobbering unsaved draft", async () => {
    const wrapper = mountModal();
    const displayInput = wrapper.get("input#edit-display-name-demo");
    await displayInput.setValue("Draft Name");

    const control = wrapper.get('[role="switch"]');
    expect(control.attributes("aria-checked")).toBe("true");
    await control.trigger("click");
    expect(wrapper.emitted("toggleCdn")).toEqual([[{ plugin, enabled: false }]]);
    expect(control.attributes("aria-checked")).toBe("true");

    await wrapper.setProps({ plugin: { ...plugin, cdn_enabled: false } });
    expect(control.attributes("aria-checked")).toBe("false");
    expect((displayInput.element as HTMLInputElement).value).toBe("Draft Name");
    expect(wrapper.text()).toContain("通过前不提供该版本的 CDN 链接");
    wrapper.unmount();
  });

  it("prevents overlapping toggles while a plugin action is pending", async () => {
    const wrapper = mountModal({ busyAction: "cdn" });
    await wrapper.get('[role="switch"]').trigger("click");
    expect(wrapper.emitted("toggleCdn")).toBeUndefined();
    wrapper.unmount();
  });

  it("shows admin-only CDN whitelist switch and emits whitelist toggle", async () => {
    const targetPlugin = { ...plugin, cdn_whitelist: false };
    const wrapper = mountModal({ plugin: targetPlugin, isAdminUser: true });
    const switches = wrapper.findAll('[role="switch"]');
    expect(switches.length).toBe(2);
    expect(wrapper.text()).toContain("白名单直发");
    await switches[1]!.trigger("click");
    expect(wrapper.emitted("toggleCdn")).toEqual([
      [{ plugin: targetPlugin, enabled: true, whitelist: true }],
    ]);
    wrapper.unmount();
  });

  it("submits trimmed draft changes via save event", async () => {
    const wrapper = mountModal();
    await wrapper.get("input#edit-display-name-demo").setValue("  Updated Demo  ");
    await wrapper.get("textarea#edit-description-demo").setValue("  Better desc  ");
    await wrapper.get("input#edit-social-link-demo").setValue("  https://alice.dev  ");

    const saveButton = wrapper.findAll("button").find((btn) => btn.text().includes("保存修改"));
    expect(saveButton).toBeTruthy();
    await saveButton!.trigger("click");

    expect(wrapper.emitted("save")).toEqual([
      [
        {
          plugin,
          changes: {
            display_name: "Updated Demo",
            desc: "Better desc",
            tags: ["demo"],
            category: "utilities",
            social_link: "https://alice.dev",
          },
        },
      ],
    ]);
    wrapper.unmount();
  });

  it("renders detail link only when plugin is listed and has a canonical owner path", () => {
    const withoutOwner = mountModal();
    expect(withoutOwner.find(".modal-detail-link").exists()).toBe(false);
    withoutOwner.unmount();

    const unlisted = mountModal({
      plugin: { ...plugin, status: "unlisted", owner_username: "Alice" },
    });
    expect(unlisted.find(".modal-detail-link").exists()).toBe(false);
    unlisted.unmount();

    const listed = mountModal({
      plugin: { ...plugin, status: "listed", owner_username: "Alice" },
    });
    expect(listed.get(".modal-detail-link").attributes("data-to")).toBe(
      "/plugin/Alice/astrbot_plugin_demo",
    );
    listed.unmount();
  });
});
