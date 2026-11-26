// @vitest-environment jsdom
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vite-plus/test";

const dialogWarning = vi.fn();
const messageError = vi.fn();

vi.mock("naive-ui", async (importOriginal) => {
  const original = await importOriginal<typeof import("naive-ui")>();
  const { defineComponent, h } = await import("vue");
  return {
    ...original,
    useDialog: () => ({ warning: dialogWarning }),
    useMessage: () => ({ error: messageError, success: vi.fn(), warning: vi.fn() }),
    NModal: defineComponent({
      props: { show: Boolean, title: String, uploading: Boolean },
      setup:
        (props, { slots }) =>
        () =>
          props.show
            ? h("section", { role: "dialog", "data-title": props.title }, [
                h("h3", props.title),
                slots.default?.(),
                slots.footer?.(),
              ])
            : null,
    }),
  };
});

import ProfileAccountSection from "./ProfileAccountSection.vue";

const baseUser = {
  id: "u1",
  public_username: "alice",
  github_login: "alice",
  internal_username: "",
  email: "",
  has_password: true,
  email_verified: true,
  has_github_token: false,
  oauth_identities: [],
  role: "user",
  avatar_backend: "oauth" as const,
  avatar_url: "https://avatars.githubusercontent.com/u/1?v=4",
  effective_avatar_url: "https://avatars.githubusercontent.com/u/1?v=4",
  uploaded_avatar_url: "",
};

function mountSection(user: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(ProfileAccountSection, {
    props: {
      currentUser: { ...baseUser, ...user } as never,
      authConfig: {},
    },
    global: { plugins: [pinia] },
    attachTo: document.body,
  });
}

describe("ProfileAccountSection avatar controls", () => {
  beforeEach(() => {
    dialogWarning.mockClear();
    document.body.innerHTML = "";
  });

  it("renders the uploaded avatar through the same-origin URL and offers replace + clear", () => {
    const wrapper = mountSection({
      avatar_backend: "uploaded",
      uploaded_avatar_url: "/v1/storage/public/stobj_1",
      effective_avatar_url: "/v1/storage/public/stobj_1",
    });

    const image = wrapper.find("img.profile-avatar__image");
    expect(image.attributes("src")).toBe("/v1/storage/public/stobj_1");
    const buttons = wrapper.findAll("button").map((btn) => btn.text());
    expect(buttons.some((text) => text.includes("更换头像"))).toBe(true);
    expect(buttons.some((text) => text.includes("清除"))).toBe(true);
    wrapper.unmount();
  });

  it("only offers upload when the avatar comes from OAuth", () => {
    const wrapper = mountSection();
    const buttons = wrapper.findAll("button").map((btn) => btn.text());
    expect(buttons.some((text) => text.includes("上传头像"))).toBe(true);
    expect(buttons.some((text) => text.includes("清除"))).toBe(false);
    expect(wrapper.find("img.profile-avatar__image").attributes("src")).toBe(
      "https://avatars.githubusercontent.com/u/1?v=4",
    );
    wrapper.unmount();
  });

  it("asks for confirmation before emitting remove-avatar", async () => {
    const wrapper = mountSection({
      avatar_backend: "uploaded",
      uploaded_avatar_url: "/v1/storage/public/stobj_1",
      effective_avatar_url: "/v1/storage/public/stobj_1",
    });
    const clearBtn = wrapper.findAll("button").find((btn) => btn.text().includes("清除"));
    expect(clearBtn).toBeDefined();
    await clearBtn!.trigger("click");
    expect(wrapper.emitted("remove-avatar")).toBeUndefined();
    expect(dialogWarning).toHaveBeenCalledTimes(1);

    const options = dialogWarning.mock.calls[0][0] as { onPositiveClick: () => void };
    options.onPositiveClick();
    expect(wrapper.emitted("remove-avatar")).toHaveLength(1);
    wrapper.unmount();
  });

  it("opens the cropper with the chosen file and forwards the cropped upload", async () => {
    const wrapper = mountSection();
    const input = wrapper.find("input.profile-avatar-input");
    expect(input.exists()).toBe(true);

    const file = new File(["png"], "pick.png", { type: "image/png" });
    Object.defineProperty(input.element, "files", { value: [file], configurable: true });
    await input.trigger("change");
    await flushPromises();

    expect(wrapper.find("[role=dialog]").exists()).toBe(true);

    const cropped = new File(["webp"], "avatar.webp", { type: "image/webp" });
    const cropper = wrapper.findComponent({ name: "AvatarCropperModal" });
    expect(cropper.exists()).toBe(true);
    cropper.vm.$emit("confirm", { file: cropped, width: 512, height: 512 });

    const emitted = wrapper.emitted("upload-avatar")?.[0]?.[0] as {
      file: File;
      onCompleted?: () => void;
    };
    expect(emitted.file).toBe(cropped);
    expect(typeof emitted.onCompleted).toBe("function");
    emitted.onCompleted?.();
    await flushPromises();
    expect(wrapper.find("[role=dialog]").exists()).toBe(false);
    wrapper.unmount();
  });

  it("shows the format error inside the cropper for unsupported files", async () => {
    const wrapper = mountSection();
    const input = wrapper.find("input.profile-avatar-input");

    const gif = new File(["gif"], "pick.gif", { type: "image/gif" });
    Object.defineProperty(input.element, "files", { value: [gif], configurable: true });
    await input.trigger("change");
    await flushPromises();

    const dialog = wrapper.find("[role=dialog]");
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("仅支持 PNG、JPEG 或 WebP 图片");
    expect(messageError).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});
