// @vitest-environment jsdom
import { defineComponent, h } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { NCheckbox, NMessageProvider } from "naive-ui";
import { describe, expect, it, vi } from "vite-plus/test";
import AuthModal from "./AuthModal.vue";
import { usePluginStore } from "@/stores/plugins";

vi.mock("naive-ui", async (importOriginal) => {
  const original = await importOriginal<typeof import("naive-ui")>();
  const { defineComponent: defComp, h: createEl } = await import("vue");
  return {
    ...original,
    NModal: defComp({
      name: "NModalStub",
      props: { show: { type: Boolean, default: false } },
      setup(props, { slots }) {
        return () =>
          props.show ? createEl("div", { class: "modal-stub" }, slots.default?.()) : null;
      },
    }),
  };
});

function mountAuthModal() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = usePluginStore();
  store.siteConfig.auth.public_login_enabled = true;
  store.siteConfig.auth.github_login_enabled = true;
  store.siteConfig.auth.oauth_providers = [
    {
      provider: "github",
      label: "GitHub",
      host: "github.com",
      instance_url: "https://github.com",
      enabled: true,
      configured: true,
      login_path: "/v1/auth/github/login",
    },
  ];
  const loginWithPasswordSpy = vi
    .spyOn(store, "loginWithPassword")
    .mockResolvedValue({ id: "u1", role: "user" });
  const loginWithGithubSpy = vi.spyOn(store, "loginWithGithub").mockImplementation(() => {});

  const Host = defineComponent({
    setup() {
      return () => h(NMessageProvider, null, { default: () => h(AuthModal, { show: true }) });
    },
  });

  const wrapper = mount(Host, {
    global: { plugins: [pinia] },
  });

  return { wrapper, store, loginWithPasswordSpy, loginWithGithubSpy };
}

describe("AuthModal trust device option", () => {
  it("defaults to untrusted (7 days) and passes trust_device=true when checked", async () => {
    const { wrapper, loginWithPasswordSpy, loginWithGithubSpy } = mountAuthModal();

    const inputs = wrapper.findAll("input");
    const usernameInput = inputs.find((el) => el.attributes("placeholder")?.includes("用户名"));
    const passwordInput = inputs.find((el) => el.attributes("type") === "password");
    expect(usernameInput).toBeDefined();
    expect(passwordInput).toBeDefined();

    await usernameInput!.setValue("alice");
    await passwordInput!.setValue("password123");

    const loginBtn = wrapper.findAll("button").find((btn) => btn.text().trim() === "登录");
    expect(loginBtn).toBeDefined();
    await loginBtn!.trigger("click");
    await flushPromises();
    expect(loginWithPasswordSpy).toHaveBeenLastCalledWith({
      username: "alice",
      password: "password123",
      trust_device: false,
    });

    const trustCheckbox = wrapper
      .findAllComponents(NCheckbox)
      .find((box) => box.text().includes("信任此设备"));
    expect(trustCheckbox).toBeDefined();
    trustCheckbox!.vm.$emit("update:checked", true);
    await flushPromises();

    await loginBtn!.trigger("click");
    await flushPromises();
    expect(loginWithPasswordSpy).toHaveBeenLastCalledWith({
      username: "alice",
      password: "password123",
      trust_device: true,
    });

    const githubBtn = wrapper.findAll("button").find((btn) => btn.text().includes("GitHub 登录"));
    expect(githubBtn).toBeDefined();
    await githubBtn!.trigger("click");
    expect(loginWithGithubSpy).toHaveBeenLastCalledWith(undefined, { trustDevice: true });

    wrapper.unmount();
  });
});
