// @vitest-environment jsdom

import { defineComponent } from "vue";
import { mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { NConfigProvider, NDialogProvider, NMessageProvider } from "naive-ui";
import { describe, expect, it } from "vite-plus/test";
import PluginCard from "./PluginCard.vue";
import type { Plugin } from "@/types";

const plugin: Plugin = {
  id: "astrbot_plugin_new",
  name: "astrbot_plugin_new",
  display_name: "New Plugin",
  version: "1.0.0",
  logo: "",
  desc: "Plugin card fixture",
  author: "Alice",
  repo: "https://github.com/alice/astrbot_plugin_new",
  tags: ["demo"],
  category: "utilities",
  stars: 12,
  likes: 7,
  comments_count: 3,
  list_index: 0,
  created_at: new Date().toISOString(),
};

describe("PluginCard", () => {
  it("renders colored metrics and the NEW title badge", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/plugin/:name", name: "PluginDetails", component: { template: "<div />" } },
      ],
    });
    await router.push("/");
    await router.isReady();

    const Host = defineComponent({
      components: {
        NConfigProvider,
        NDialogProvider,
        NMessageProvider,
        PluginCard,
      },
      setup: () => ({ plugin }),
      template: `
        <n-config-provider>
          <n-message-provider>
            <n-dialog-provider>
              <plugin-card :plugin="plugin" />
            </n-dialog-provider>
          </n-message-provider>
        </n-config-provider>
      `,
    });
    const wrapper = mount(Host, { global: { plugins: [createPinia(), router] } });

    expect(wrapper.find(".metric-item--star").text()).toContain("12");
    expect(wrapper.find(".metric-item--like").text()).toContain("7");
    expect(wrapper.find(".metric-item--comment").text()).toContain("3");
    expect(wrapper.find(".new-badge").text()).toBe("NEW");
  });
});
