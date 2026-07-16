// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { flushPromises } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import ReviewCommentThread from "./ReviewCommentThread.vue";
import type { ReviewAnchor, ReviewComment, ReviewCommentListResponse } from "@/types/artifacts";

const anchor: ReviewAnchor = {
  fileId: "file-current",
  filePath: "main.py",
  side: "current",
  lineStart: 10,
  lineEnd: 12,
  diffId: "diff-1",
  hunkId: "hunk-1",
};

function threadFixture(overrides: Partial<ReviewComment> = {}): ReviewComment {
  return {
    id: "thread-1",
    artifact_id: "artifact-1",
    source_thread_id: null,
    file_id: "file-current",
    file_path: "main.py",
    file_sha256: "a".repeat(64),
    side: "current",
    line_start: 10,
    line_end: 12,
    body: "<img src=x onerror=window.__unsafe=1>",
    reviewer_nickname: "reviewer",
    reviewer_role: "admin",
    resolved: false,
    resolved_by_nickname: "",
    locked_at: null,
    version: 2,
    created_at: "2026-07-16T00:00:00Z",
    updated_at: "2026-07-16T00:01:00Z",
    resolved_at: null,
    event_count: 2,
    events_truncated: false,
    events: [
      {
        id: "event-create",
        thread_id: "thread-1",
        type: "create",
        body: "<img src=x onerror=window.__unsafe=1>",
        actor_nickname: "reviewer",
        actor_role: "admin",
        expected_version: 0,
        resulting_version: 1,
        created_at: "2026-07-16T00:00:00Z",
      },
      {
        id: "event-reply",
        thread_id: "thread-1",
        type: "reply",
        body: "<script>unsafe()</script>",
        actor_nickname: "alice",
        actor_role: "author",
        expected_version: 1,
        resulting_version: 2,
        created_at: "2026-07-16T00:01:00Z",
      },
    ],
    ...overrides,
  };
}

function comments(thread: ReviewComment): ReviewCommentListResponse {
  return {
    artifact_id: "artifact-1",
    items: [thread],
    total: 1,
    limit: 20,
    offset: 0,
  };
}

describe("ReviewCommentThread", () => {
  it("keeps comment text inert and emits admin create and resolve commands", async () => {
    const thread = threadFixture();
    const wrapper = mount(ReviewCommentThread, {
      props: {
        comments: comments(thread),
        anchor,
        isAdmin: true,
        currentNickname: "reviewer",
        canCreate: true,
        loading: false,
        busy: false,
      },
    });

    expect(wrapper.text()).toContain("<img src=x onerror=window.__unsafe=1>");
    expect(wrapper.text()).toContain("<script>unsafe()</script>");
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.find("script").exists()).toBe(false);

    await wrapper.find('[data-testid="new-comment"] textarea').setValue("请移除动态执行");
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("发布评论"))
      ?.trigger("click");
    expect(wrapper.emitted("create")).toEqual([
      [
        {
          file_id: "file-current",
          side: "current",
          line_start: 10,
          line_end: 12,
          body: "请移除动态执行",
          diff_id: "diff-1",
          hunk_id: "hunk-1",
        },
      ],
    ]);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("解决"))
      ?.trigger("click");
    expect(wrapper.emitted("resolve")).toEqual([[{ threadId: "thread-1", version: 2 }]]);
  });

  it("gives authors reply and addressed actions but not admin controls", async () => {
    const wrapper = mount(ReviewCommentThread, {
      props: {
        comments: comments(threadFixture()),
        anchor,
        isAdmin: false,
        currentNickname: "alice",
        canCreate: false,
        loading: false,
        busy: false,
      },
    });

    expect(wrapper.find('[data-testid="new-comment"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("标记已处理");
    expect(wrapper.text()).not.toContain("发布评论");
    expect(wrapper.text()).not.toContain("解决");

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("标记已处理"))
      ?.trigger("click");
    expect(wrapper.emitted("address")).toEqual([[{ threadId: "thread-1", version: 2, body: "" }]]);
  });

  it("renders terminal threads as read-only", () => {
    const wrapper = mount(ReviewCommentThread, {
      props: {
        comments: comments(threadFixture({ locked_at: "2026-07-16T00:02:00Z", resolved: true })),
        anchor,
        isAdmin: true,
        currentNickname: "reviewer",
        canCreate: false,
        loading: false,
        busy: false,
      },
    });

    expect(wrapper.text()).toContain("已锁定");
    expect(wrapper.text()).toContain("当前版本已进入只读终态");
    expect(wrapper.text()).not.toContain("回复线程");
  });
});
