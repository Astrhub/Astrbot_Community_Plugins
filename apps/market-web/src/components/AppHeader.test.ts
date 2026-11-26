// @vitest-environment jsdom
import { defineComponent, nextTick } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { NConfigProvider, NDialogProvider, NMessageProvider } from "naive-ui";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import AppHeader from "./AppHeader.vue";
import { usePluginStore } from "@/stores/plugins";
import type { User } from "@/types";

vi.mock("naive-ui", async (importOriginal) => {
  const original = await importOriginal<typeof import("naive-ui")>();
  const { defineComponent: defComp, h } = await import("vue");
  return {
    ...original,
    NDropdown: defComp({
      name: "NDropdownStub",
      props: {
        show: { type: Boolean, default: false },
        options: { type: Array, default: () => [] },
        menuProps: { type: Function, default: undefined },
      },
      emits: ["select", "clickoutside"],
      setup(props, { slots, emit }) {
        return () => {
          const menuHandlers = props.menuProps ? props.menuProps() : {};
          const items = (
            props.options as Array<{
              key: string;
              label?: string | (() => import("vue").VNodeChild);
              type?: string;
            }>
          )
            .filter((opt) => opt.type !== "divider")
            .map((opt) =>
              h(
                "div",
                {
                  key: opt.key,
                  role: "menuitem",
                  "data-key": opt.key,
                  onClick: () => emit("select", opt.key),
                },
                typeof opt.label === "function" ? [opt.label()] : opt.label,
              ),
            );
          return h("div", { class: "dropdown-stub" }, [
            slots.default?.(),
            props.show
              ? h(
                  "div",
                  {
                    class: "dropdown-menu-stub",
                    onMouseenter: menuHandlers.onMouseenter,
                    onMouseleave: menuHandlers.onMouseleave,
                  },
                  items,
                )
              : null,
          ]);
        };
      },
    }),
  };
});

async function createTestApp(userOverride?: User) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = usePluginStore();
  store.currentUser = userOverride || {
    id: "u1",
    public_username: "AlicePublic",
    github_login: "AliceGH",
    role: "user",
    is_developer: true,
  };

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", name: "Home", component: { template: "<div />" } },
      { path: "/docs/rest", name: "RestDocs", component: { template: "<div />" } },
      { path: "/notifications", name: "Notifications", component: { template: "<div />" } },
      { path: "/author/:username", name: "AuthorPage", component: { template: "<div />" } },
      { path: "/settings/personal", name: "PersonalSettings", component: { template: "<div />" } },
    ],
  });
  await router.push("/");
  await router.isReady();

  const Host = defineComponent({
    components: { NConfigProvider, NDialogProvider, NMessageProvider, AppHeader },
    template: `
      <n-config-provider>
        <n-message-provider>
          <n-dialog-provider>
            <div class="outside-area">outside</div>
            <app-header />
          </n-dialog-provider>
        </n-message-provider>
      </n-config-provider>
    `,
  });

  const wrapper = mount(Host, {
    global: {
      plugins: [pinia, router],
      stubs: {
        AnnouncementButton: { template: "<div />" },
        ThemeModeButton: { template: "<button type='button' />" },
      },
    },
  });

  return { wrapper, router, store };
}

describe("AppHeader user dropdown", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("opens on hover, stays open while pointer enters menu, and auto-closes on leave", async () => {
    const { wrapper } = await createTestApp();
    const trigger = wrapper.get(".user-trigger");

    expect(wrapper.find(".dropdown-menu-stub").exists()).toBe(false);

    await trigger.trigger("mouseenter");
    await nextTick();
    expect(wrapper.find(".dropdown-menu-stub").exists()).toBe(true);

    await trigger.trigger("mouseleave");
    await wrapper.get(".dropdown-menu-stub").trigger("mouseenter");
    vi.advanceTimersByTime(300);
    await nextTick();
    expect(wrapper.find(".dropdown-menu-stub").exists()).toBe(true);

    await wrapper.get(".dropdown-menu-stub").trigger("mouseleave");
    vi.advanceTimersByTime(200);
    await nextTick();
    expect(wrapper.find(".dropdown-menu-stub").exists()).toBe(false);
    wrapper.unmount();
  });

  it("pins dropdown on click so mouseleave keeps it open, and closes on outside click", async () => {
    const { wrapper } = await createTestApp();
    const trigger = wrapper.get(".user-trigger");

    await trigger.trigger("mouseenter");
    await trigger.trigger("click");
    await nextTick();
    expect(wrapper.find(".dropdown-menu-stub").exists()).toBe(true);
    expect(trigger.classes()).toContain("is-pinned");

    await trigger.trigger("mouseleave");
    vi.advanceTimersByTime(500);
    await nextTick();
    expect(wrapper.find(".dropdown-menu-stub").exists()).toBe(true);

    const dropdownComp = wrapper.findComponent({ name: "NDropdownStub" });
    dropdownComp.vm.$emit("clickoutside", { target: wrapper.get(".outside-area").element });
    await nextTick();
    expect(wrapper.find(".dropdown-menu-stub").exists()).toBe(false);
    expect(trigger.classes()).not.toContain("is-pinned");
    wrapper.unmount();
  });

  it("renders 我的插件 as a crawlable router-link and navigates to /author/:username", async () => {
    const { wrapper, router } = await createTestApp();
    const trigger = wrapper.get(".user-trigger");

    await trigger.trigger("click");
    await nextTick();

    const myPluginsLink = wrapper.get('[data-key="my-plugins"] a.user-menu-link');
    expect(myPluginsLink.text()).toBe("我的插件");
    expect(myPluginsLink.attributes("href")).toBe("/author/AlicePublic");

    await myPluginsLink.trigger("click");
    await flushPromises();
    await nextTick();

    expect(router.currentRoute.value.path).toBe("/author/AlicePublic");
    expect(wrapper.find(".dropdown-menu-stub").exists()).toBe(false);
    wrapper.unmount();
  });

  it("omits 我的插件 when user has only an internal_username without public handle", async () => {
    const { wrapper } = await createTestApp({
      id: "u-internal",
      internal_username: "internal_only",
      role: "core_admin",
    });

    await wrapper.get(".user-trigger").trigger("click");
    await nextTick();

    expect(wrapper.find('[data-key="my-plugins"]').exists()).toBe(false);
    expect(wrapper.get('[data-key="profile"] a.user-menu-link').attributes("href")).toBe(
      "/settings/personal",
    );
    wrapper.unmount();
  });
});
