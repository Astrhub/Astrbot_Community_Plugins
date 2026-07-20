// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import ReviewPolicyPanel from "./ReviewPolicyPanel.vue";
import type { ReviewOperationsResponse, ReviewPolicyRecord } from "@/types/artifacts";
import { createDefaultReviewPolicy } from "@/utils/reviewPolicy";

function policy(status: ReviewPolicyRecord["status"] = "draft"): ReviewPolicyRecord {
  return {
    id: "policy-1",
    version: "policy-v1",
    schema_version: "1",
    status,
    is_default: true,
    policy: createDefaultReviewPolicy(),
    policy_sha256: "a".repeat(64),
    base_policy_id: null,
    created_by_nickname: "core",
    validation_summary: {
      valid: status !== "draft",
      schema_version: "1",
      policy_sha256: "a".repeat(64),
      readiness_checked: true,
      issues:
        status === "draft"
          ? [{ path: "tools.runtime", code: "health_unknown", message: "Runtime unavailable" }]
          : [],
    },
    validated_at: status === "draft" ? null : "2026-07-17T00:00:00Z",
    activated_at: status === "active" ? "2026-07-17T00:00:00Z" : null,
    retired_at: status === "retired" ? "2026-07-17T00:00:00Z" : null,
    created_at: "2026-07-17T00:00:00Z",
    updated_at: "2026-07-17T00:00:00Z",
  };
}

function operations(): ReviewOperationsResponse {
  return {
    health: {
      review: {
        enabled: true,
        configured: false,
        ready: false,
        degraded: true,
        auto_approve_enabled: false,
        policy_auto_approve_enabled: false,
        auto_approve_effective: false,
        components: {},
      },
      workers: [
        {
          kind: "artifact_worker",
          status: "ready",
          ready: true,
          degraded: false,
          live_instances: 1,
          stale_instances: 0,
          capacity: 4,
          active_count: 1,
          last_observed_at: "2026-07-17T00:00:00Z",
          reasons: [],
        },
        {
          kind: "runtime_runner",
          status: "degraded",
          ready: false,
          degraded: true,
          live_instances: 0,
          stale_instances: 0,
          capacity: 0,
          active_count: 0,
          last_observed_at: null,
          reasons: ["runtime_runner_heartbeat_missing"],
        },
      ],
      tools: [
        {
          name: "policy",
          enabled: true,
          configured: true,
          ready: true,
          degraded: false,
          status: "ready",
          reasons: [],
          version: "policy-v1",
          data_updated_at: null,
          freshness: "current",
          observed_at: null,
        },
      ],
    },
    metrics: {
      available: true,
      window_started_at: "2026-07-16T00:00:00Z",
      collected_at: "2026-07-17T00:00:00Z",
      queue: [{ job_type: "static_scan", status: "queued", count: 2 }],
      stages: [
        {
          run_type: "static",
          sample_count: 4,
          failure_count: 1,
          timeout_count: 0,
          average_duration_ms: 100,
          p95_duration_ms: 180,
        },
      ],
      manual_wait: { waiting_count: 3, average_wait_seconds: 120, max_wait_seconds: 300 },
      routing: [{ action: "approve", source: "admin", count: 1 }],
      revoke: [],
    },
  };
}

function mountPanel(isCoreAdmin: boolean, item = policy()) {
  return mount(ReviewPolicyPanel, {
    props: {
      policies: [item],
      operations: isCoreAdmin ? operations() : null,
      lastDiff: null,
      loading: false,
      busy: false,
      isCoreAdmin,
      error: "",
    },
    global: { stubs: { teleport: true } },
  });
}

describe("ReviewPolicyPanel", () => {
  it("keeps normal administrators on a read-only active snapshot", async () => {
    const wrapper = mountPanel(false, policy("active"));
    await flushPromises();

    expect(wrapper.text()).toContain("只读");
    expect(wrapper.text()).not.toContain("创建草稿");
    expect(wrapper.text()).not.toContain("退役");
    expect(wrapper.text()).not.toContain("config:llm-default");
  });

  it("emits typed validate and activation commands only after a reason", async () => {
    const wrapper = mountPanel(true);
    await flushPromises();
    const reason = wrapper.get('input[placeholder="变更原因"]');
    await reason.setValue("Runtime runner 已部署");
    const validate = wrapper.findAll("button").find((item) => item.text().includes("校验"));
    const activate = wrapper.findAll("button").find((item) => item.text().includes("激活"));
    await validate?.trigger("click");
    await activate?.trigger("click");

    expect(wrapper.emitted("validate")).toEqual([
      [{ policyId: "policy-1", reason: "Runtime runner 已部署" }],
    ]);
    expect(wrapper.emitted("activate")).toEqual([
      [{ policyId: "policy-1", reason: "Runtime runner 已部署" }],
    ]);
  });

  it("creates a new immutable draft while retaining hidden config references", async () => {
    const wrapper = mountPanel(true, policy("active"));
    await flushPromises();
    await wrapper.get('input[placeholder="policy-2026-07-v2"]').setValue("policy-v2");
    await flushPromises();
    const create = wrapper.findAll("button").find((item) => item.text().includes("创建草稿"));
    expect(create).toBeDefined();
    await create?.trigger("click");
    await flushPromises();

    const emitted = wrapper.emitted("create")?.[0]?.[0] as {
      version: string;
      basePolicyId: string;
      policy: ReturnType<typeof createDefaultReviewPolicy>;
    };
    expect(emitted.version).toBe("policy-v2");
    expect(emitted.basePolicyId).toBe("policy-1");
    expect(emitted.policy.llm.provider_config_ref).toBe("config:llm-default");
    expect(wrapper.text()).not.toContain("config:llm-default");
  });

  it("renders bounded worker, tool, and stage metrics", async () => {
    const wrapper = mountPanel(true, policy("active"));
    await flushPromises();

    expect(wrapper.text()).toContain("Artifact Worker");
    expect(wrapper.text()).toContain("Runtime Runner");
    expect(wrapper.text()).toContain("24 小时指标");
    expect(wrapper.text()).toContain("static");
    expect(wrapper.text()).not.toContain("worker_id");
  });
});
