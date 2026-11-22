// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils";
import { createTestingPinia } from "@pinia/testing";
import { describe, expect, it, vi } from "vite-plus/test";
import { usePluginStore } from "../stores/plugins";
import Setup from "./Setup.vue";

const messages = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn() }));
const routerReplace = vi.hoisted(() => vi.fn());

vi.mock("vue-router", () => ({ useRouter: () => ({ replace: routerReplace }) }));
vi.mock("naive-ui", async (original) => ({
  ...(await original<typeof import("naive-ui")>()),
  useMessage: () => messages,
}));

function mountSetup(storageMode: "local" | "postgres") {
  messages.success.mockClear();
  messages.error.mockClear();
  routerReplace.mockClear();
  const pinia = createTestingPinia({ createSpy: vi.fn });
  const store = usePluginStore(pinia);
  const status = {
    required: true,
    missing: storageMode === "local" ? ["core_admin"] : ["database_url", "redis_url"],
    storage_mode: storageMode,
    local_data_path:
      storageMode === "local" ? "/var/lib/astrbot-market/data/market-state.json" : undefined,
    database_configured: false,
    redis_configured: false,
    saved_setup: store.setupStatus.saved_setup,
    restart_required: false,
  };
  store.setupStatus = status;
  vi.mocked(store.loadSetupStatus).mockResolvedValue(status);
  vi.mocked(store.saveSetupConfig).mockResolvedValue({ saved: true, activated: true });
  vi.mocked(store.loadPlugins).mockResolvedValue([] as never);
  vi.mocked(store.loadCurrentUser).mockResolvedValue(null as never);
  return {
    store,
    wrapper: mount(Setup, { global: { plugins: [pinia] } }),
  };
}

describe("Setup wizard modes", () => {
  it("shows only site, admin, and review steps in local mode and submits without postgres or redis", async () => {
    const { store, wrapper } = mountSetup("local");
    await flushPromises();

    expect(wrapper.text()).toContain("本地单进程模式");
    const stepsText = wrapper.get(".setup-steps").text();
    expect(stepsText).toContain("站点");
    expect(stepsText).toContain("管理员");
    expect(stepsText).toContain("确认");
    expect(stepsText).not.toContain("PostgreSQL");
    expect(stepsText).not.toContain("Redis");

    const nextButton = () => wrapper.findAll("button").find((button) => button.text() === "下一步");
    await nextButton()!.trigger("click");
    await flushPromises();

    const passwordInput = wrapper.get('input[type="password"]');
    await passwordInput.setValue("password123");
    await nextButton()!.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("本地单文件持久化");
    const saveButton = wrapper.findAll("button").find((button) => button.text() === "保存并启用");
    await saveButton!.trigger("click");
    await flushPromises();

    expect(store.saveSetupConfig).toHaveBeenCalledWith({
      site: expect.objectContaining({ icon_url: "/logo.webp" }),
      admin: { username: "admin", password: "password123" },
    });
    wrapper.unmount();
  });

  it("retains PostgreSQL and Redis steps in standard postgres mode", async () => {
    const { wrapper } = mountSetup("postgres");
    await flushPromises();

    expect(wrapper.text()).toContain("PostgreSQL");
    expect(wrapper.text()).toContain("Redis");
    wrapper.unmount();
  });
});
