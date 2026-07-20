// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { useReviewWorkspaceStore, WorkspaceApiError } from "./reviewWorkspace";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function fileList(artifactId: string, fileId: string, tree = "a".repeat(64)) {
  return {
    artifact_id: artifactId,
    tree_sha256: tree,
    items: [
      {
        id: fileId,
        artifact_id: artifactId,
        path: "main.py",
        language: "python",
        mime_type: "text/x-python",
        sha256: "b".repeat(64),
        size_bytes: 10,
        line_count: 1,
        is_text: true,
        is_entrypoint: true,
        is_reachable: true,
        graph_status: "complete",
        content_available: true,
      },
    ],
    total: 1,
    limit: 200,
    offset: 0,
  };
}

function fileContent(artifactId: string, fileId: string, text: string) {
  const list = fileList(artifactId, fileId);
  return {
    artifact_id: artifactId,
    tree_sha256: list.tree_sha256,
    file: list.items[0],
    encoding: "utf-8",
    start_line: 1,
    end_line: 1,
    total_lines: 1,
    truncated: false,
    lines: [{ number: 1, text }],
  };
}

describe("useReviewWorkspaceStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reuses a bounded current content page instead of refetching it", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(fileList("artifact-1", "file-1")))
      .mockResolvedValueOnce(jsonResponse(fileContent("artifact-1", "file-1", "value = 1")));
    vi.stubGlobal("fetch", fetchMock);
    const store = useReviewWorkspaceStore();

    await store.loadFiles("artifact-1");
    await store.loadFileContent("artifact-1", "file-1");
    await store.loadFileContent("artifact-1", "file-1");

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(store.fileContent?.lines[0]?.text).toBe("value = 1");
  });

  it("drops a late response after the selected artifact changes", async () => {
    let resolveFirst: ((response: Response) => void) | undefined;
    const first = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce(jsonResponse(fileContent("artifact-2", "file-2", "new")));
    vi.stubGlobal("fetch", fetchMock);
    const store = useReviewWorkspaceStore();

    const oldRequest = store.loadFileContent("artifact-1", "file-1");
    const newRequest = store.loadFileContent("artifact-2", "file-2");
    resolveFirst?.(jsonResponse(fileContent("artifact-1", "file-1", "old")));
    await Promise.all([oldRequest, newRequest]);

    expect(store.activeArtifactId).toBe("artifact-2");
    expect(store.fileContent?.file.id).toBe("file-2");
    expect(store.fileContent?.lines[0]?.text).toBe("new");
  });

  it("keeps structured optimistic concurrency errors for comment recovery", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          { detail: { code: "comment_version_conflict", message: "评论版本已变化" } },
          409,
        ),
      ),
    );
    const store = useReviewWorkspaceStore();

    const request = store.replyComment("artifact-1", "thread-1", 2, "已修复");
    const error = await request.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(WorkspaceApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "comment_version_conflict",
      message: "评论版本已变化",
    });
    expect(store.mutating).toBe(false);
  });

  it("appends cursor history without duplicating replayed events", async () => {
    const firstEvent = {
      id: "event-1",
      type: "artifact_submitted",
      occurred_at: "2026-07-16T00:00:00Z",
      source: "user",
      actor_nickname: "alice",
      actor_role: "author",
      idempotency_key: "",
      policy_version_id: null,
      payload: {},
    };
    const secondEvent = { ...firstEvent, id: "event-2", type: "run" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          artifact_id: "artifact-1",
          items: [firstEvent],
          has_more: true,
          next_cursor: "cursor-1",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          artifact_id: "artifact-1",
          items: [firstEvent, secondEvent],
          has_more: false,
          next_cursor: null,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const store = useReviewWorkspaceStore();

    await store.loadHistory("artifact-1", { reset: true });
    await store.loadHistory("artifact-1");

    expect(store.historyItems.map((item) => item.id)).toEqual(["event-1", "event-2"]);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("cursor=cursor-1");
    expect(store.historyHasMore).toBe(false);
  });

  it("does not apply a late comment mutation after the active artifact generation changes", async () => {
    let resolveMutation: ((response: Response) => void) | undefined;
    const mutation = new Promise<Response>((resolve) => {
      resolveMutation = resolve;
    });
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(mutation)
      .mockResolvedValueOnce(
        jsonResponse({
          artifact_id: "artifact-2",
          items: [],
          total: 0,
          limit: 20,
          offset: 0,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const store = useReviewWorkspaceStore();

    const oldMutation = store.replyComment("artifact-1", "thread-1", 1, "旧回复");
    await store.loadComments("artifact-2");
    resolveMutation?.(
      jsonResponse({
        comment: {
          id: "thread-1",
          artifact_id: "artifact-1",
          source_thread_id: null,
          file_id: "file-1",
          file_path: "main.py",
          file_sha256: "a".repeat(64),
          side: "current",
          line_start: 1,
          line_end: 1,
          body: "旧线程",
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
          events: [],
        },
      }),
    );
    await oldMutation;

    expect(store.activeArtifactId).toBe("artifact-2");
    expect(store.comments?.artifact_id).toBe("artifact-2");
    expect(store.comments?.items).toEqual([]);
  });

  it("sends stable-risk finding version and explicit current-release confirmation", async () => {
    let capturedUrl = "";
    let capturedInit: RequestInit | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      capturedUrl =
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      capturedInit = init;
      return jsonResponse({
        candidate_artifact_id: "artifact-1",
        finding_id: "finding-1",
        affects_current_release: true,
        correlation: {
          kind: "admin_confirmation",
          deterministic: false,
          candidate_artifact_id: "artifact-1",
          stable_artifact_id: "artifact-stable",
          finding_id: "finding-1",
        },
        stable_artifact: {},
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const store = useReviewWorkspaceStore();

    await store.requestStableRisk("artifact/1", "finding/1", 7, "已核对稳定版本证据", true);

    expect(capturedUrl).toContain("/artifacts/artifact%2F1/findings/finding%2F1/stable-risk");
    expect(typeof capturedInit?.body).toBe("string");
    expect(JSON.parse(capturedInit?.body as string)).toMatchObject({
      expected_version: 7,
      reason: "已核对稳定版本证据",
      confirm_affects_current_release: true,
    });
  });

  it("cancels an older comments page before applying a successful mutation", async () => {
    let resolveOlderPage: ((response: Response) => void) | undefined;
    const olderPage = new Promise<Response>((resolve) => {
      resolveOlderPage = resolve;
    });
    const updatedThread = {
      id: "thread-1",
      artifact_id: "artifact-1",
      source_thread_id: null,
      file_id: "file-1",
      file_path: "main.py",
      file_sha256: "a".repeat(64),
      side: "current",
      line_start: 1,
      line_end: 1,
      body: "线程",
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
      events: [],
    };
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(olderPage)
      .mockResolvedValueOnce(jsonResponse({ comment: updatedThread }));
    vi.stubGlobal("fetch", fetchMock);
    const store = useReviewWorkspaceStore();

    const pendingPage = store.loadComments("artifact-1");
    await store.replyComment("artifact-1", "thread-1", 1, "新回复");
    resolveOlderPage?.(
      jsonResponse({
        artifact_id: "artifact-1",
        items: [],
        total: 0,
        limit: 20,
        offset: 0,
      }),
    );
    await pendingPage;

    expect(store.comments?.items.map((item) => item.id)).toEqual(["thread-1"]);
    expect(store.comments?.items[0]?.version).toBe(2);
  });
});
