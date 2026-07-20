// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import FieldHint from "./FieldHint.vue";

describe("FieldHint", () => {
  it("opens from hover, focus, and click while exposing expanded state", async () => {
    const wrapper = mount(FieldHint, {
      props: { content: "字段规则说明" },
      attachTo: document.body,
    });
    const trigger = wrapper.get("button");

    await trigger.trigger("mouseenter");
    expect(trigger.attributes("aria-expanded")).toBe("true");
    expect(wrapper.text()).toContain("字段规则说明");

    await trigger.trigger("mouseleave");
    expect(trigger.attributes("aria-expanded")).toBe("false");

    await trigger.trigger("focus");
    expect(trigger.attributes("aria-expanded")).toBe("true");

    await trigger.trigger("click");
    await trigger.trigger("blur");
    expect(trigger.attributes("aria-expanded")).toBe("true");

    await trigger.trigger("click");
    expect(trigger.attributes("aria-expanded")).toBe("false");
    wrapper.unmount();
  });
});
