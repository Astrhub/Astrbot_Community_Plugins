// @vitest-environment jsdom
import { defineComponent, nextTick } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { NConfigProvider, NDialogProvider, NMessageProvider } from "naive-ui";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import AuthorPage from "./AuthorPage.vue";
import { usePluginStore } from "@/stores/plugins";
import type { Plugin, User } from "@/types";

vi.mock("naive-ui", async (importOriginal) => {
  const original = await importOriginal<typeof import("naive-ui")>();
  const { defineComponent: defComp, h } = await import("vue");
  return {
    ...original,
    NModal: defComp({
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
    NDropdown: defComp({
      name: "NDropdownStub",
      props: {
        options: { type: Array, default: () => [] },
      },
      emits: ["select"],
      setup(props, { slots, emit }) {
        return () =>
          h("div", { class: "dropdown-stub" }, [
            slots.default?.(),
            h(
              "div",
              { class: "dropdown-options-stub" },
              (props.options as Array<{ key: string; label?: string }>).map((opt) =>
                h(
                  "button",
                  {
                    key: opt.key,
                    type: "button",
                    "data-dropdown-key": opt.key,
                    onClick: () => emit("select", opt.key),
                  },
                  opt.label,
                ),
              ),
            ),
          ]);
      },
    }),
  };
});

const confirmExternalOpenMock = vi.fn();

vi.mock("@/composables/useExternalOpenConfirm", () => ({
  useExternalOpenConfirm: () => ({ confirmExternalOpen: confirmExternalOpenMock }),
}));

vi.mock("@/composables/useSeo", () => ({
  useSeo: vi.fn(),
  useSeoContext: () => ({
    brandName: { value: "Astrhub 插件市场" },
    absoluteSiteUrl: (path: string) => `https://market.example${path}`,
  }),
}));

const listedPlugin: Plugin = {
  id: "p1",
  name: "astrbot_plugin_alpha",
  display_name: "Alpha Plugin",
  desc: "Alpha desc",
  version: "1.2.0",
  logo: "",
  author: "Alice",
  owner_username: "Alice",
  repo: "https://github.com/alice/astrbot_plugin_alpha",
  social_link: "https://alice.dev",
  tags: ["tools"],
  category: "utilities",
  stars: 10,
  likes: 3,
  comments_count: 1,
  download_count: 50,
  list_index: 0,
  status: "listed",
  cdn_enabled: true,
};

const pendingPlugin: Plugin = {
  ...listedPlugin,
  id: "p2",
  name: "astrbot_plugin_beta",
  display_name: "Beta Plugin",
  social_link: "https://market.example/author/Alice",
  status: "pending",
  cdn_enabled: false,
};

const unlistedPlugin: Plugin = {
  ...listedPlugin,
  id: "p3",
  name: "astrbot_plugin_gamma",
  display_name: "Gamma Plugin",
  social_link: "https://blog.alice.dev",
  status: "unlisted",
  cdn_enabled: false,
};

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

async function mountAuthorPage(options: {
  username?: string;
  currentUser?: User | null;
  authorPayload?: Record<string, unknown>;
  myPlugins?: Plugin[];
  patchResponse?: { ok: boolean; body: Record<string, unknown> };
}) {
  confirmExternalOpenMock.mockReset();
  const username = options.username || "Alice";
  const authorPayload = options.authorPayload || {
    username: "Alice",
    display_name: "Alice Dev",
    avatar_url: "",
    github_url: "https://github.com/alice",
    oauth_profiles: [{ provider: "github", login: "alice", url: "https://github.com/alice" }],
    items: [listedPlugin],
  };

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = requestUrl(input);
    if (url.endsWith("/v1/me")) {
      return options.currentUser
        ? new Response(JSON.stringify(options.currentUser), { status: 200 })
        : new Response("{}", { status: 401 });
    }
    if (url.includes(`/v1/authors/${encodeURIComponent(username)}/plugins`)) {
      return new Response(JSON.stringify(authorPayload), { status: 200 });
    }
    if (url.endsWith("/v1/me/plugins")) {
      return new Response(JSON.stringify({ items: options.myPlugins || [] }), { status: 200 });
    }
    if (url.includes("/v1/plugins/") && init?.method === "PATCH") {
      const res = options.patchResponse || {
        ok: true,
        body: { ...listedPlugin, display_name: "Alpha Renamed" },
      };
      return new Response(JSON.stringify(res.body), { status: res.ok ? 200 : 400 });
    }
    return new Response("{}", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);

  const pinia = createPinia();
  setActivePinia(pinia);
  const store = usePluginStore();
  store.siteConfig.web_url = "https://market.example";

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", name: "Home", component: { template: "<div />" } },
      { path: "/author/:username", name: "AuthorPage", component: { template: "<div />" } },
      {
        path: "/plugin/:username/:name",
        name: "PluginDetails",
        component: { template: "<div />" },
      },
      { path: "/submit", name: "SubmitPlugin", component: { template: "<div />" } },
      { path: "/settings/personal", name: "PersonalSettings", component: { template: "<div />" } },
    ],
  });
  await router.push(`/author/${username}`);
  await router.isReady();

  const Host = defineComponent({
    components: { NConfigProvider, NDialogProvider, NMessageProvider, AuthorPage },
    template: `
      <n-config-provider>
        <n-message-provider>
          <n-dialog-provider>
            <author-page />
          </n-dialog-provider>
        </n-message-provider>
      </n-config-provider>
    `,
  });

  const wrapper = mount(Host, {
    global: {
      plugins: [pinia, router],
      stubs: {
        AppHeader: { template: "<header />" },
        AppFooter: { template: "<footer />" },
      },
    },
  });

  await flushPromises();
  await nextTick();
  return { wrapper, fetchMock, store, router };
}

describe("AuthorPage visitor vs owner views and external link aggregation", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders visitor view with aggregated top links and external open confirmation", async () => {
    const { wrapper, fetchMock } = await mountAuthorPage({
      currentUser: null,
      authorPayload: {
        username: "Alice",
        display_name: "Alice Dev",
        oauth_profiles: [
          { provider: "github", login: "alice", url: "https://github.com/alice" },
          { provider: "gitlab", login: "alice_gl", url: "https://gitlab.com/alice_gl" },
        ],
        items: [listedPlugin],
      },
    });

    expect(wrapper.find(".owner-badge").exists()).toBe(false);
    expect(wrapper.text()).toContain("已上架插件");
    expect(wrapper.find(".status-filters").exists()).toBe(false);
    expect(wrapper.find(".author-profile-action").exists()).toBe(false);
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining("/v1/me/plugins"),
      expect.anything(),
    );

    // Multiple bound Git profiles are aggregated into a single dropdown button.
    expect(wrapper.text()).toContain("Git 主页");
    await wrapper.get('[data-dropdown-key="https://gitlab.com/alice_gl"]').trigger("click");
    expect(confirmExternalOpenMock).toHaveBeenCalledWith("https://gitlab.com/alice_gl");

    // Distinct plugin social_link is aggregated into the top 作者主页 button with external confirmation.
    const socialLinkAnchor = wrapper
      .findAll("a.header-action-btn")
      .find((item) => item.text().includes("作者主页"));
    expect(socialLinkAnchor).toBeTruthy();
    await socialLinkAnchor!.trigger("click");
    expect(confirmExternalOpenMock).toHaveBeenCalledWith("https://alice.dev/");
    wrapper.unmount();
  });

  it("uses a direct top button with external confirmation when only one Git profile is bound", async () => {
    const { wrapper } = await mountAuthorPage({
      currentUser: null,
      authorPayload: {
        username: "Alice",
        display_name: "Alice Dev",
        oauth_profiles: [{ provider: "github", login: "alice", url: "https://github.com/alice" }],
        items: [{ ...listedPlugin, social_link: "https://github.com/alice" }],
      },
    });

    const topLinks = wrapper.findAll("a.header-action-btn");
    expect(topLinks).toHaveLength(1);
    expect(topLinks[0]!.text()).toContain("GitHub 主页");
    await topLinks[0]!.trigger("click");
    expect(confirmExternalOpenMock).toHaveBeenCalledWith("https://github.com/alice");
    wrapper.unmount();
  });

  it("renders owner workspace with status filters and modal editing when viewing own page", async () => {
    const { wrapper, fetchMock } = await mountAuthorPage({
      currentUser: {
        id: "u1",
        public_username: "Alice",
        github_login: "alice",
        role: "user",
        is_developer: true,
      },
      myPlugins: [listedPlugin, pendingPlugin, unlistedPlugin],
    });

    expect(wrapper.get(".owner-badge").text()).toBe("本人主页");
    expect(wrapper.text()).toContain("我的插件");
    expect(wrapper.text()).toContain("Alpha Plugin");
    expect(wrapper.text()).toContain("Beta Plugin");
    expect(wrapper.text()).toContain("Gamma Plugin");

    // Pending and unlisted plugins should not render broken detail links.
    expect(wrapper.findAll(".plugin-card__detail-link")).toHaveLength(1);

    // Filter to unlisted only.
    const unlistedTab = wrapper
      .findAll(".status-filter-btn")
      .find((btn) => btn.text().includes("已下架"));
    await unlistedTab!.trigger("click");
    expect(wrapper.text()).toContain("Gamma Plugin");
    expect(wrapper.text()).not.toContain("Alpha Plugin");

    // Switch back to all and open edit modal for Alpha Plugin.
    await wrapper.findAll(".status-filter-btn")[0]!.trigger("click");
    const editBtn = wrapper.findAll("button").find((btn) => btn.text().trim() === "编辑");
    expect(editBtn).toBeTruthy();
    await editBtn!.trigger("click");
    await vi.dynamicImportSettled();
    await flushPromises();
    await nextTick();

    expect(wrapper.find(".modal-stub").exists()).toBe(true);
    await wrapper.get("input#edit-display-name-p1").setValue("Alpha Renamed");
    const saveBtn = wrapper.findAll("button").find((btn) => btn.text().includes("保存修改"));
    await saveBtn!.trigger("click");
    await flushPromises();
    await nextTick();

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/v1/plugins/p1"),
      expect.objectContaining({ method: "PATCH" }),
    );
    expect(wrapper.find(".modal-stub").exists()).toBe(false);
    expect(wrapper.text()).toContain("Alpha Renamed");
    wrapper.unmount();
  });

  it("skips /v1/me/plugins for non-developer owners and shows onboarding prompt", async () => {
    const { wrapper, fetchMock } = await mountAuthorPage({
      currentUser: {
        id: "u2",
        public_username: "Alice",
        role: "user",
        is_developer: false,
      },
      authorPayload: {
        username: "Alice",
        display_name: "Alice",
        oauth_profiles: [],
        items: [],
      },
    });

    expect(wrapper.get(".owner-badge").text()).toBe("本人主页");
    expect(wrapper.text()).toContain("绑定至少一个 OAuth 账号成为开发者");
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining("/v1/me/plugins"),
      expect.anything(),
    );
    wrapper.unmount();
  });
});
