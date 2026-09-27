// @vitest-environment jsdom
import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import { NCheckbox } from "naive-ui";
import ReviewManualPanel from "./ReviewManualPanel.vue";
import type { ArtifactDecision, PluginArtifact } from "@/types/artifacts";

const artifact: PluginArtifact = {
  id: "artifact-1",
  plugin_id: "astrbot_plugin_demo",
  version: "1.0.0",
  normalized_version: "1.0.0",
  source_type: "github",
  archive_sha256: "a".repeat(64),
  size_bytes: 123,
  review_status: "processing_failed",
  publication_status: "unpublished",
  risk_level: "none",
  created_at: "2026-09-27T00:00:00Z",
  updated_at: "2026-09-27T00:00:00Z",
};

function setup() {
  return mount(ReviewManualPanel, {
    props: { artifact, decisions: [], isAdmin: true, busy: false, error: "" },
  });
}

describe("independent manual review", () => {
  it("allows version comments without a file anchor or successful scanner", async () => {
    const wrapper = setup();
    await wrapper.find("textarea").setValue("人工检查完成");
    const button = wrapper.findAll("button").find((node) => node.text() === "仅发表评论")!;
    await button.trigger("click");
    expect(wrapper.emitted("action")).toEqual([[{ action: "comment", reason: "人工检查完成" }]]);
    wrapper.unmount();
  });

  it("requires an opinion and confirmation before manual approval", async () => {
    const wrapper = setup();
    const button = wrapper.findAll("button").find((node) => node.text() === "评论并人工放行")!;
    expect(button.attributes("disabled")).toBeDefined();
    await wrapper.find("textarea").setValue("已核对失败项，不需要调整");
    expect(button.attributes("disabled")).toBeDefined();
    wrapper.findComponent(NCheckbox).vm.$emit("update:checked", true);
    await wrapper.vm.$nextTick();
    await button.trigger("click");
    expect(wrapper.emitted("action")).toEqual([
      [{ action: "manual_approve", reason: "已核对失败项，不需要调整" }],
    ]);
    await wrapper.setProps({ artifact: { ...artifact, id: "artifact-2" } });
    expect(button.attributes("disabled")).toBeDefined();
    wrapper.unmount();
  });

  it("renders saved opinions as text and exposes manual provenance", () => {
    const wrapper = setup();
    return wrapper
      .setProps({
        decisions: [
          {
            id: "decision-1",
            artifact_id: artifact.id,
            action: "manual_approve",
            source: "admin",
            from_status: "processing_failed",
            to_status: "approved",
            reason: "<img src=x onerror=alert(1)>",
            metadata: {},
            input_run_ids: [],
            input_fingerprints: [],
            created_at: artifact.created_at,
          } satisfies ArtifactDecision,
        ],
      })
      .then(() => {
        expect(wrapper.text()).toContain("人工批准");
        expect(wrapper.find("img").exists()).toBe(false);
        wrapper.unmount();
      });
  });
});
