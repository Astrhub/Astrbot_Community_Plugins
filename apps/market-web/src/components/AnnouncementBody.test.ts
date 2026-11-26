// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import AnnouncementBody from "./AnnouncementBody.vue";

describe("AnnouncementBody", () => {
  it("renders Markdown images and removes executable or unsafe content", () => {
    const wrapper = mount(AnnouncementBody, {
      props: {
        body: [
          "**重要通知**",
          "",
          "![本站图片](/v1/storage/public/image-1)",
          "",
          "![外链图片](https://img.example.com/cover.png)",
          "",
          "![不安全图片](http://img.example.com/cover.png)",
          "",
          "[危险链接](javascript:alert(1))",
          "",
          "![公告图片](/v1/storage/public/generic)",
          "",
          '<img src="/v1/storage/public/missing-alt">',
          "",
          '<img src="x" onerror="alert(1)"><script>alert(1)</script>',
        ].join("\n"),
      },
    });

    expect(wrapper.find("strong").text()).toBe("重要通知");
    const images = wrapper.findAll("img[src]");
    expect(images).toHaveLength(2);
    expect(images.map((image) => image.attributes("src"))).toEqual([
      "/v1/storage/public/image-1",
      "https://img.example.com/cover.png",
    ]);
    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.html()).not.toContain("onerror");
    expect(wrapper.html()).not.toContain("javascript:");
  });
});
