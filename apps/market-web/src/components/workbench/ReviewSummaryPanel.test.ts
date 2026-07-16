// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import ReviewSummaryPanel from "./ReviewSummaryPanel.vue";
import type { ArtifactDetail } from "@/types/artifacts";

function detailFixture(): ArtifactDetail {
  return {
    artifact: {
      id: "artifact-summary",
      plugin_id: "astrbot_plugin_demo",
      plugin_name: "Demo",
      version: "v1.0.0",
      normalized_version: "1.0.0",
      repo_version: "v1.0.0",
      source_type: "github",
      archive_sha256: "a".repeat(64),
      size_bytes: 128,
      review_status: "pending_review",
      publication_status: "unpublished",
      risk_level: "high",
      suggested_category: "utilities",
      category_confidence: 0.92,
      category_reason: "Metadata indicates an operational tool",
      review_coverage: {
        routing: {
          route: "manual_review",
          target_status: "pending_review",
          reason_codes: ["finding_requires_manual_review"],
        },
      },
      created_at: "2026-07-10T00:00:00Z",
      updated_at: "2026-07-10T00:00:00Z",
    },
    runs: [
      {
        id: "run-summary",
        artifact_id: "artifact-summary",
        type: "llm_summary",
        status: "succeeded",
        attempt: 1,
        advisory: true,
        label: "自动审查建议",
        summary: "建议人工检查网络访问",
        model: "review-model",
        tool_name: "structured-llm",
        tool_version: "llm-v1",
        coverage: { outcome: "completed", stage_name: "llm_summary" },
        created_at: "2026-07-10T00:00:00Z",
      },
    ],
    findings: [
      {
        id: "finding-summary",
        artifact_id: "artifact-summary",
        run_id: "run-summary",
        fingerprint: "network-risk",
        severity: "high",
        message: "可能存在未说明的网络访问",
        status: "open",
        source: "llm",
        deterministic: false,
        advisory: true,
        label: "自动审查建议",
        version: 1,
        created_at: "2026-07-10T00:00:00Z",
      },
    ],
    decisions: [],
  };
}

describe("ReviewSummaryPanel", () => {
  it("labels advisory evidence and exposes route coverage without claiming safety", () => {
    const wrapper = mount(ReviewSummaryPanel, {
      props: { detail: detailFixture(), loading: false },
    });

    expect(wrapper.text()).toContain("自动审查建议");
    expect(wrapper.text()).toContain("structured-llm llm-v1");
    expect(wrapper.text()).toContain("manual_review");
    expect(wrapper.text()).toContain("finding_requires_manual_review");
    expect(wrapper.text()).not.toContain("绝对安全");
  });

  it("emits the structured finding instead of constructing a path client-side", async () => {
    const detail = detailFixture();
    detail.findings[0] = {
      ...detail.findings[0]!,
      file_path: "main.py",
      line_start: 12,
      line_end: 14,
    };
    const wrapper = mount(ReviewSummaryPanel, {
      props: { detail, loading: false },
    });

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("定位"))
      ?.trigger("click");

    expect(wrapper.emitted("openFinding")).toEqual([[detail.findings[0]]]);
  });
});
