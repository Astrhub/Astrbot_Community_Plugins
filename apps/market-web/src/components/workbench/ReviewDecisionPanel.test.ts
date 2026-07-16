// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { flushPromises } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import ReviewDecisionPanel from "./ReviewDecisionPanel.vue";
import type { ArtifactFinding, PluginArtifact } from "@/types/artifacts";

function pendingArtifact(): PluginArtifact {
  return {
    id: "artifact-1",
    plugin_id: "astrbot_plugin_demo",
    plugin_name: "astrbot_plugin_demo",
    version: "v1.0.0",
    normalized_version: "1.0.0",
    published_version: "v0.9.0",
    source_type: "upload",
    archive_sha256: "a".repeat(64),
    size_bytes: 128,
    review_status: "pending_review",
    publication_status: "unpublished",
    risk_level: "critical",
    created_at: "2026-07-10T00:00:00Z",
    updated_at: "2026-07-10T00:00:00Z",
  };
}

function criticalFinding(): ArtifactFinding {
  return {
    id: "finding-critical",
    artifact_id: "artifact-1",
    run_id: "run-static",
    fingerprint: "critical-risk",
    rule_id: "STATIC-001",
    file_path: "main.py",
    line_start: 8,
    line_end: 8,
    severity: "critical",
    message: "检测到确定性危险调用",
    status: "open",
    source: "static",
    deterministic: true,
    advisory: false,
    label: "确定性检查",
    version: 3,
    created_at: "2026-07-10T00:00:00Z",
  };
}

function mountPanel(
  artifact: PluginArtifact = pendingArtifact(),
  findings: ArtifactFinding[] = [],
  isAdmin = true,
) {
  return mount(ReviewDecisionPanel, {
    props: { artifact, findings, isAdmin, busy: false },
    global: { stubs: { teleport: true } },
  });
}

async function openAction(wrapper: ReturnType<typeof mountPanel>, label: string): Promise<void> {
  const button = wrapper.findAll("button").find((item) => item.text().includes(label));
  expect(button).toBeDefined();
  await button?.trigger("click");
  await flushPromises();
}

async function confirmReason(
  wrapper: ReturnType<typeof mountPanel>,
  reason: string,
): Promise<void> {
  await wrapper.find("textarea").setValue(reason);
  await flushPromises();
  const confirm = wrapper.findAll("button").find((item) => item.text().includes("确认执行"));
  await confirm?.trigger("click");
}

describe("ReviewDecisionPanel", () => {
  it("requires a reason before emitting a rejection", async () => {
    const wrapper = mountPanel();
    await openAction(wrapper, "拒绝");

    const confirm = wrapper.findAll("button").find((item) => item.text().includes("确认执行"));
    expect(confirm?.attributes("disabled")).toBeDefined();
    await confirmReason(wrapper, "发现未说明的命令执行");

    expect(wrapper.emitted("reject")).toEqual([["发现未说明的命令执行"]]);
  });

  it("shows authors the fallback and a resubmission command for changes requested", async () => {
    const wrapper = mountPanel(
      { ...pendingArtifact(), review_status: "changes_requested" },
      [],
      false,
    );

    expect(wrapper.text()).toContain("不会获得插件源 CDN 链接");
    expect(wrapper.text()).toContain("GitHub 直连");
    expect(wrapper.text()).toContain("重新提交修订版");
    expect(wrapper.find("textarea").exists()).toBe(false);
    await wrapper.get("button").trigger("click");
    expect(wrapper.emitted("resubmit")).toHaveLength(1);
  });

  it("emits request changes with the review reason", async () => {
    const wrapper = mountPanel();
    await openAction(wrapper, "要求修改");
    await confirmReason(wrapper, "请固定依赖版本");

    expect(wrapper.emitted("requestChanges")).toEqual([["请固定依赖版本"]]);
  });

  it("offers an explicit manual revoke retry after a terminal revoke failure", async () => {
    const wrapper = mountPanel({
      ...pendingArtifact(),
      review_status: "approved",
      publication_status: "revoke_failed",
    });

    await openAction(wrapper, "重试下架");
    await confirmReason(wrapper, "再次清理公开对象");

    expect(wrapper.emitted("revoke")).toEqual([["再次清理公开对象"]]);
  });

  it("requires finding selection, reason, and explicit confirmation for stable risk", async () => {
    const wrapper = mountPanel(pendingArtifact(), [criticalFinding()]);
    await openAction(wrapper, "严重风险关联稳定版");

    await wrapper.find("textarea").setValue("已核对相同文件 SHA");
    await wrapper.find(".n-checkbox").trigger("click");
    await flushPromises();
    const confirm = wrapper.findAll("button").find((item) => item.text().includes("确认执行"));
    await confirm?.trigger("click");

    expect(wrapper.emitted("stableRisk")).toEqual([
      [
        {
          findingId: "finding-critical",
          expectedVersion: 3,
          reason: "已核对相同文件 SHA",
          confirmAffectsCurrentRelease: true,
        },
      ],
    ]);
  });
});
