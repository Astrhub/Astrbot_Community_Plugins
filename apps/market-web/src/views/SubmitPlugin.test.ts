// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils";
import { createTestingPinia } from "@pinia/testing";
import { NSelect, NDynamicTags } from "naive-ui";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { usePluginStore } from "../stores/plugins";
import SubmitPlugin from "./SubmitPlugin.vue";

vi.mock("../composables/useSeo", () => ({ useSeo: vi.fn() }));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn(), back: vi.fn() }) }));
vi.mock("naive-ui", async (original) => ({
  ...(await original<typeof import("naive-ui")>()),
  useMessage: () => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn() }),
}));

const repositories = ["a", "b"].map((id) => ({
  id,
  name: `astrbot_plugin_${id}`,
  full_name: `my-org/astrbot_plugin_${id}`,
  repo: `https://github.com/my-org/astrbot_plugin_${id}`,
  owner: "my-org",
  owner_type: "Organization" as const,
  description: `Plugin ${id}`,
}));

function setup() {
  const pinia = createTestingPinia({ createSpy: vi.fn });
  const store = usePluginStore(pinia);
  store.currentUser = { id: "user-1", github_login: "alice" } as never;
  vi.mocked(store.loadCurrentUser).mockResolvedValue(undefined);
  vi.mocked(store.loadGithubRepositories).mockResolvedValue({
    items: repositories,
    next_page: null,
  });
  return { store, mount: () => mount(SubmitPlugin, { global: { plugins: [pinia] } }) };
}

describe("SubmitPlugin repository selection", () => {
  afterEach(() => vi.restoreAllMocks());
  it("leaves every missing metadata field empty instead of using repository details", async () => {
    const context = setup();
    vi.mocked(context.store.fetchPluginSubmissionMetadata)
      .mockResolvedValueOnce({
        name: "astrbot_plugin_a",
        display_name: "名称",
        desc: "描述",
        short_desc: "短描述",
        tags: ["文件标签"],
        author: "文件作者",
      } as never)
      .mockResolvedValueOnce({ repo: repositories[1]!.repo } as never);
    const wrapper = context.mount();
    await flushPromises();
    const select = wrapper.findAllComponents(NSelect)[0]!;
    select.vm.$emit("update:value", "a");
    await flushPromises();
    expect(wrapper.findComponent(NDynamicTags).props("value")).toEqual(["文件标签"]);
    select.vm.$emit("update:value", "b");
    await flushPromises();
    for (const name of [
      "plugin-name",
      "plugin-display-name",
      "plugin-description",
      "plugin-short-description",
      "plugin-author",
    ]) {
      expect((wrapper.get(`[name="${name}"]`).element as HTMLInputElement).value).toBe("");
    }
    expect(wrapper.findComponent(NDynamicTags).props("value")).toEqual([]);
    wrapper.unmount();
  });

  it("retains the complete metadata description and keeps short_desc separate", async () => {
    const context = setup();
    const description = "完整描述".repeat(80);
    vi.mocked(context.store.fetchPluginSubmissionMetadata).mockResolvedValue({
      desc: description,
      short_desc: "卡片短描述",
    } as never);
    const wrapper = context.mount();
    await flushPromises();
    wrapper.findAllComponents(NSelect)[0]!.vm.$emit("update:value", "a");
    await flushPromises();
    expect((wrapper.get('[name="plugin-description"]').element as HTMLTextAreaElement).value).toBe(
      description,
    );
    expect(
      (wrapper.get('[name="plugin-short-description"]').element as HTMLInputElement).value,
    ).toBe("卡片短描述");
    wrapper.unmount();
  });
  it("loads matching repositories across empty pages and fills metadata on selection", async () => {
    const context = setup();
    vi.mocked(context.store.loadGithubRepositories)
      .mockResolvedValueOnce({ items: [], next_page: 2 })
      .mockResolvedValueOnce({ items: repositories, next_page: null });
    vi.mocked(context.store.fetchPluginSubmissionMetadata).mockResolvedValue({
      name: "astrbot_plugin_a",
      display_name: "自动名称",
      author: "作者",
      desc: "自动简介",
    } as never);
    const wrapper = context.mount();
    await flushPromises();
    expect(context.store.loadGithubRepositories).toHaveBeenNthCalledWith(2, 2);
    wrapper.findAllComponents(NSelect)[0]!.vm.$emit("update:value", "a");
    await flushPromises();
    expect(context.store.fetchPluginSubmissionMetadata).toHaveBeenCalledWith(
      repositories[0]!.repo,
      "a",
    );
    expect(
      (wrapper.get('input[name="plugin-display-name"]').element as HTMLInputElement).value,
    ).toBe("自动名称");
    expect(wrapper.find('input[name="plugin-repo"]').exists()).toBe(false);
    expect(wrapper.findAll("button").some((button) => button.text() === "刷新仓库")).toBe(false);
    wrapper.unmount();
  });

  it("does not let a slow previous repository overwrite the current selection", async () => {
    const context = setup();
    let finishFirst!: (value: never) => void;
    vi.mocked(context.store.fetchPluginSubmissionMetadata)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishFirst = resolve;
          }),
      )
      .mockResolvedValueOnce({ name: "astrbot_plugin_b", display_name: "仓库 B" } as never);
    const wrapper = context.mount();
    await flushPromises();
    const select = wrapper.findAllComponents(NSelect)[0]!;
    select.vm.$emit("update:value", "a");
    await flushPromises();
    select.vm.$emit("update:value", "b");
    await flushPromises();
    finishFirst({ name: "astrbot_plugin_a", display_name: "仓库 A" } as never);
    await flushPromises();
    expect(
      (wrapper.get('input[name="plugin-display-name"]').element as HTMLInputElement).value,
    ).toBe("仓库 B");
    wrapper.unmount();
  });

  it("offers a GitHub reconnect instead of requesting a personal token", async () => {
    const context = setup();
    vi.mocked(context.store.loadGithubRepositories).mockRejectedValue(
      Object.assign(new Error("请连接 GitHub"), { code: "github_authorization_required" }),
    );
    const wrapper = context.mount();
    await flushPromises();
    const button = wrapper.findAll("button").find((item) => item.text() === "连接 GitHub");
    expect(button).toBeDefined();
    await button!.trigger("click");
    expect(context.store.loginWithGithub).toHaveBeenCalledWith("/submit");
    expect(wrapper.find('input[type="password"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("refreshes periodically without showing loading or overwriting the draft", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1000);
    const timers = vi.spyOn(window, "setInterval");
    const clear = vi.spyOn(window, "clearInterval");
    const context = setup();
    vi.mocked(context.store.fetchPluginSubmissionMetadata).mockResolvedValue({
      display_name: "自动名称",
    } as never);
    const wrapper = context.mount();
    await flushPromises();
    const select = wrapper.findAllComponents(NSelect)[0]!;
    select.vm.$emit("update:value", "a");
    await flushPromises();
    await wrapper.get('input[name="plugin-display-name"]').setValue("用户正在编辑的名称");
    let finish!: (value: { items: typeof repositories; next_page: null }) => void;
    vi.mocked(context.store.loadGithubRepositories).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const tick = timers.mock.calls.find(([, delay]) => delay === 60_000)?.[0] as () => void;
    expect(tick).toBeTypeOf("function");
    now.mockReturnValue(61_000);
    tick();
    await flushPromises();
    expect(context.store.loadGithubRepositories).toHaveBeenCalledTimes(2);
    expect(select.props("loading")).toBe(false);
    expect(
      wrapper
        .findAll("button")
        .find((button) => button.text() === "提交审核")!
        .attributes("disabled"),
    ).toBeUndefined();
    finish({ items: [repositories[1]!], next_page: null });
    await flushPromises();
    expect(
      (wrapper.get('input[name="plugin-display-name"]').element as HTMLInputElement).value,
    ).toBe("用户正在编辑的名称");
    expect(select.props("value")).toBe("a");
    expect(context.store.fetchPluginSubmissionMetadata).toHaveBeenCalledTimes(1);
    wrapper.unmount();
    expect(clear).toHaveBeenCalled();
  });

  it("pauses refresh while hidden and updates once when returning", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1000);
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const context = setup();
    const wrapper = context.mount();
    await flushPromises();
    now.mockReturnValue(61_000);
    visibility.mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();
    expect(context.store.loadGithubRepositories).toHaveBeenCalledTimes(1);
    visibility.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
    await flushPromises();
    expect(context.store.loadGithubRepositories).toHaveBeenCalledTimes(2);
    wrapper.unmount();
    now.mockReturnValue(121_000);
    window.dispatchEvent(new Event("focus"));
    expect(context.store.loadGithubRepositories).toHaveBeenCalledTimes(2);
  });
});
