// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { createTestingPinia } from "@pinia/testing";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { NDialogProvider, NMessageProvider } from "naive-ui";
import SiteAnnouncementPanel from "./SiteAnnouncementPanel.vue";
import { usePluginStore } from "@/stores/plugins";
import type { Announcement } from "@/types";

const baseAnnouncement: Announcement = {
  id: "announcement-1",
  title: "原始标题",
  body: "原始正文",
  created_at: "2026-11-26T10:00:00Z",
  updated_at: "2026-11-26T10:00:00Z",
};

let activeWrapper: VueWrapper | null = null;

function mountPanel(announcements: Announcement[] = []) {
  const pinia = createTestingPinia({
    createSpy: vi.fn,
    initialState: {
      plugins: {
        announcements,
        currentUser: { id: "core-1", role: "core_admin" },
      },
    },
  });
  const store = usePluginStore(pinia);
  const wrapper = mount(
    {
      components: { NDialogProvider, NMessageProvider, SiteAnnouncementPanel },
      template:
        "<n-dialog-provider><n-message-provider><site-announcement-panel /></n-message-provider></n-dialog-provider>",
    },
    {
      attachTo: document.body,
      global: { plugins: [pinia] },
    },
  );
  activeWrapper = wrapper;
  return { wrapper, store };
}

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll("button").find((item) => item.text().includes(text));
  expect(found, `missing button: ${text}`).toBeDefined();
  return found!;
}

afterEach(() => {
  activeWrapper?.unmount();
  activeWrapper = null;
  document.body.innerHTML = "";
});

describe("SiteAnnouncementPanel", () => {
  it("publishes a new announcement and saves edits", async () => {
    const { wrapper, store } = mountPanel();
    await wrapper.find('input[placeholder="例如：维护通知"]').setValue("新公告");
    await wrapper.find("textarea").setValue("公告 **正文**");
    await button(wrapper, "发布公告").trigger("click");
    await flushPromises();

    expect(store.publishAnnouncement).toHaveBeenCalledWith({
      title: "新公告",
      body: "公告 **正文**",
    });

    await wrapper.unmount();
    const editView = mountPanel([baseAnnouncement]);
    await button(editView.wrapper, "编辑").trigger("click");
    await editView.wrapper.find('input[placeholder="例如：维护通知"]').setValue("修改后的标题");
    await editView.wrapper.find("textarea").setValue("修改后的正文");
    await button(editView.wrapper, "保存修改").trigger("click");
    await flushPromises();

    expect(editView.store.updateAnnouncement).toHaveBeenCalledWith("announcement-1", {
      title: "修改后的标题",
      body: "修改后的正文",
    });
  });

  it("deletes an announcement after confirmation", async () => {
    const { wrapper, store } = mountPanel([baseAnnouncement]);
    await button(wrapper, "删除").trigger("click");
    await flushPromises();

    const dialogButton = Array.from(document.body.querySelectorAll(".n-dialog button")).find(
      (item) => item.textContent?.trim() === "删除",
    );
    expect(dialogButton).toBeDefined();
    (dialogButton as HTMLButtonElement).click();
    await flushPromises();

    expect(store.deleteAnnouncement).toHaveBeenCalledWith("announcement-1");
    const dialog = document.body.querySelector(".n-dialog") as HTMLElement | null;
    expect(dialog === null || dialog.style.display === "none").toBe(true);
  });

  it("inserts uploaded and HTTPS images into the Markdown editor", async () => {
    const { wrapper, store } = mountPanel();
    vi.mocked(store.uploadAnnouncementImage).mockResolvedValue({
      url: "/v1/storage/public/uploaded-image",
      content_type: "image/png",
      size_bytes: 3,
      sha256: "abc",
    });
    const file = new File(["png"], "cover.png", { type: "image/png" });
    const fileInput = wrapper.find('input[type="file"]');
    Object.defineProperty(fileInput.element, "files", { configurable: true, value: [file] });
    await fileInput.trigger("change");
    await flushPromises();

    expect(store.uploadAnnouncementImage).toHaveBeenCalledWith(file);
    expect((wrapper.find("textarea").element as HTMLTextAreaElement).value).toContain(
      "![公告图片](/v1/storage/public/uploaded-image)",
    );

    await wrapper
      .find('input[placeholder="或输入 HTTPS 图片链接"]')
      .setValue("https://img.example.com/cover(1).png");
    await button(wrapper, "插入链接").trigger("click");
    const body = (wrapper.find("textarea").element as HTMLTextAreaElement).value;
    expect(body).toContain("![公告图片](https://img.example.com/cover%281%29.png)");
    expect(wrapper.find(".announcement-preview").exists()).toBe(true);
  });
});
