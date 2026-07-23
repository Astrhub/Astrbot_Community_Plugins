// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import ReviewHistoryTimeline from "./ReviewHistoryTimeline.vue";
import type { ReviewHistoryEvent } from "@/types/artifacts";

const event: ReviewHistoryEvent = {
  id: "event-1",
  type: "decision",
  occurred_at: "2026-07-16T00:00:00Z",
  source: "admin",
  actor_nickname: "reviewer",
  actor_role: "admin",
  idempotency_key: "private-key-not-rendered",
  policy_version_id: "policy-version-1234567890",
  payload: {
    action: "request_changes",
    message: "<img src=x onerror=window.__unsafe=1>",
  },
};

describe("ReviewHistoryTimeline", () => {
  it("renders public actor snapshots and payload values as inert text", () => {
    const wrapper = mount(ReviewHistoryTimeline, {
      props: { items: [event], loading: false, hasMore: false },
    });

    expect(wrapper.text()).toContain("审查决定");
    expect(wrapper.text()).toContain("reviewer");
    expect(wrapper.text()).toContain("<img src=x onerror=window.__unsafe=1>");
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("private-key-not-rendered");
  });

  it("emits cursor pagination from the visible load-more command", async () => {
    const wrapper = mount(ReviewHistoryTimeline, {
      props: { items: [event], loading: false, hasMore: true },
    });

    await wrapper.get("button").trigger("click");
    expect(wrapper.emitted("loadMore")).toHaveLength(1);
  });
});
