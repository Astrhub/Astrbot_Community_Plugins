// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import { flushPromises } from "@vue/test-utils";
import ReviewDecisionPanel from "./ReviewDecisionPanel.vue";
import type { PluginArtifact } from "@/types/artifacts";

function pendingArtifact(): PluginArtifact {
  return {
    id: "artifact-1",
    plugin_id: "astrbot_plugin_demo",
    plugin_name: "astrbot_plugin_demo",
    version: "v1.0.0",
    normalized_version: "1.0.0",
    source_type: "upload",
    archive_sha256: "a".repeat(64),
    size_bytes: 128,
    review_status: "pending_review",
    publication_status: "unpublished",
    risk_level: "low",
    created_at: "2026-07-10T00:00:00Z",
    updated_at: "2026-07-10T00:00:00Z",
  };
}

describe("ReviewDecisionPanel", () => {
  it("emits a rejection only after the reviewer enters a reason", async () => {
    const wrapper = mount(ReviewDecisionPanel, {
      props: { artifact: pendingArtifact(), isAdmin: true, busy: false },
    });

    const buttons = wrapper.findAll("button");
    const rejectButton = buttons.find((button) => button.text().includes("拒绝"));
    expect(rejectButton?.attributes("disabled")).toBeDefined();

    await wrapper.find("textarea").setValue("发现未说明的命令执行");
    await flushPromises();
    await rejectButton?.trigger("click");

    expect(wrapper.emitted("reject")).toEqual([["发现未说明的命令执行"]]);
  });

  it("shows authors the no-CDN fallback without decision controls", () => {
    const wrapper = mount(ReviewDecisionPanel, {
      props: { artifact: pendingArtifact(), isAdmin: false, busy: false },
    });

    expect(wrapper.text()).toContain("不会获得插件源 CDN 链接");
    expect(wrapper.text()).toContain("GitHub 直连");
    expect(wrapper.find("textarea").exists()).toBe(false);
  });

  it("emits request changes with the review reason", async () => {
    const wrapper = mount(ReviewDecisionPanel, {
      props: { artifact: pendingArtifact(), isAdmin: true, busy: false },
    });

    await wrapper.find("textarea").setValue("请固定依赖版本");
    await flushPromises();
    const button = wrapper.findAll("button").find((item) => item.text().includes("要求修改"));
    await button?.trigger("click");

    expect(wrapper.emitted("requestChanges")).toEqual([["请固定依赖版本"]]);
  });

  it("offers an explicit revoke retry after a terminal revoke failure", async () => {
    const artifact: PluginArtifact = {
      ...pendingArtifact(),
      review_status: "approved",
      publication_status: "revoke_failed",
    };
    const wrapper = mount(ReviewDecisionPanel, {
      props: { artifact, isAdmin: true, busy: false },
    });

    expect(wrapper.text()).toContain("重试下架");
    await wrapper.find("textarea").setValue("再次清理公开对象");
    await flushPromises();
    const retryButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("重试下架"));
    await retryButton?.trigger("click");

    expect(wrapper.emitted("revoke")).toEqual([["再次清理公开对象"]]);
  });
});
