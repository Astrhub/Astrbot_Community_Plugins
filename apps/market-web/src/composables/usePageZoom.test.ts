// @vitest-environment jsdom

import { defineComponent, h, nextTick } from "vue";
import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import { PAGE_BASE, PAGE_EDGE, PAGE_ZOOM_MAX, calculatePageZoom, usePageZoom } from "./usePageZoom";

describe("calculatePageZoom", () => {
  it("keeps normal viewports at the native scale", () => {
    expect(calculatePageZoom(PAGE_BASE + PAGE_EDGE * 2)).toBe(1);
  });

  it("uses the available wide-screen space and caps the result", () => {
    expect(calculatePageZoom(1920)).toBeCloseTo((1920 - PAGE_EDGE * 2) / PAGE_BASE);
    expect(calculatePageZoom(4096)).toBe(PAGE_ZOOM_MAX);
  });
});

describe("usePageZoom", () => {
  it("keeps body teleports in the same zoomed coordinate system and restores styles", async () => {
    const originalWidth = window.innerWidth;
    const originalZoom = document.body.style.getPropertyValue("zoom");
    const originalPageZoom = document.body.dataset.pageZoom;

    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1920 });
    document.body.style.setProperty("zoom", "0.9");
    document.body.dataset.pageZoom = "existing";

    const wrapper = mount(
      defineComponent({
        setup() {
          usePageZoom();
          return () => h("div");
        },
      }),
      { attachTo: document.body },
    );

    expect(Number(document.body.style.getPropertyValue("zoom"))).toBeCloseTo(
      calculatePageZoom(1920),
    );

    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    window.dispatchEvent(new Event("resize"));
    await nextTick();
    expect(document.body.style.getPropertyValue("zoom")).toBe("1");

    wrapper.unmount();
    expect(document.body.style.getPropertyValue("zoom")).toBe("0.9");
    expect(document.body.dataset.pageZoom).toBe("existing");

    Object.defineProperty(window, "innerWidth", { configurable: true, value: originalWidth });
    if (originalZoom) document.body.style.setProperty("zoom", originalZoom);
    else document.body.style.removeProperty("zoom");
    if (originalPageZoom !== undefined) document.body.dataset.pageZoom = originalPageZoom;
    else delete document.body.dataset.pageZoom;
  });
});
