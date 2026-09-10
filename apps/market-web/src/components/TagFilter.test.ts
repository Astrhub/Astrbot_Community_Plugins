// @vitest-environment jsdom
import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import TagFilter from "./TagFilter.vue";

describe("TagFilter", () => {
  it("keeps All first and mutually exclusive with multiple selected tags", async () => {
    const wrapper = mount(TagFilter, {
      props: {
        modelValue: [],
        options: [
          { label: "工具", value: "工具" },
          { label: "娱乐", value: "娱乐" },
        ],
      },
    });
    const chips = wrapper.findAll(".tag-chip");
    expect(chips[0]!.text()).toBe("全部");
    await chips[1]!.trigger("click");
    expect(wrapper.emitted("update:modelValue")!.at(-1)).toEqual([["工具"]]);
    await wrapper.setProps({ modelValue: ["工具"] });
    expect(chips[0]!.attributes("aria-pressed")).toBe("false");
    await chips[2]!.trigger("click");
    expect(wrapper.emitted("update:modelValue")!.at(-1)).toEqual([["工具", "娱乐"]]);
    await wrapper.setProps({ modelValue: ["工具", "娱乐"] });
    await chips[0]!.trigger("click");
    expect(wrapper.emitted("update:modelValue")!.at(-1)).toEqual([[]]);
    wrapper.unmount();
  });

  it("keeps All available during search and preserves selections outside results", async () => {
    const wrapper = mount(TagFilter, {
      props: {
        modelValue: ["工具"],
        options: [
          { label: "工具", value: "工具" },
          { label: "娱乐", value: "娱乐" },
        ],
      },
    });
    await wrapper.get(".tag-filter__expand").trigger("click");
    await wrapper.get("input").setValue("没有这个标签");
    expect(wrapper.findAll(".tag-chip")).toHaveLength(1);
    expect(wrapper.get(".tag-chip").text()).toBe("全部");
    expect(wrapper.find("[aria-label='移除标签 工具']").exists()).toBe(true);
    await wrapper.get(".tag-chip").trigger("click");
    expect(wrapper.emitted("update:modelValue")!.at(-1)).toEqual([[]]);
    wrapper.unmount();
  });
});
