// @vitest-environment jsdom
import { mount } from "@vue/test-utils";
import { NCheckbox } from "naive-ui";
import { describe, expect, it } from "vite-plus/test";
import AccessKeyManager from "./AccessKeyManager.vue";

describe("AccessKeyManager tiering", () => {
  it("disables market:write for registered users and shows developer upgrade hint", async () => {
    const wrapper = mount(AccessKeyManager, {
      props: {
        keys: [],
        canCreateWriteKey: false,
      },
    });

    const checkboxes = wrapper.findAllComponents(NCheckbox);
    const writeBox = checkboxes.find((box) => box.props("value") === "market:write");
    expect(writeBox?.props("disabled")).toBe(true);
    expect(wrapper.text()).toContain("仅可创建只读（market:read）访问密钥");

    const createBtn = wrapper.findAll("button").find((btn) => btn.text().includes("生成访问密钥"));
    expect(createBtn).toBeDefined();
    await createBtn!.trigger("click");
    expect(wrapper.emitted("create")?.[0]?.[0]).toEqual(
      expect.objectContaining({ scopes: ["market:read"] }),
    );
    wrapper.unmount();
  });

  it("enables market:write for developer and above", () => {
    const wrapper = mount(AccessKeyManager, {
      props: {
        keys: [],
        canCreateWriteKey: true,
      },
    });

    const checkboxes = wrapper.findAllComponents(NCheckbox);
    const writeBox = checkboxes.find((box) => box.props("value") === "market:write");
    expect(writeBox?.props("disabled")).toBe(false);
    wrapper.unmount();
  });
});
