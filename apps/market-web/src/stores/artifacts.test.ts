// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { useArtifactStore } from "./artifacts";

describe("useArtifactStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads the current author's artifact list and clears loading state", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(
          JSON.stringify({
            items: [
              {
                id: "artifact-1",
                plugin_id: "astrbot_plugin_demo",
                version: "v1.0.0",
                normalized_version: "1.0.0",
                source_type: "github",
                archive_sha256: "a".repeat(64),
                size_bytes: 128,
                review_status: "pending_review",
                publication_status: "unpublished",
                risk_level: "none",
                created_at: "2026-07-10T00:00:00Z",
                updated_at: "2026-07-10T00:00:00Z",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const store = useArtifactStore();

    const items = await store.loadMine();

    expect(items).toHaveLength(1);
    expect(store.items[0]?.review_status).toBe("pending_review");
    expect(store.loadingList).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/v1/me/artifacts"),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("builds isolated ZIP and GitHub submission requests", async () => {
    const artifact = {
      id: "artifact-2",
      plugin_id: "plugin/id",
      version: "v1.0.0",
      normalized_version: "1.0.0",
      source_type: "upload",
      archive_sha256: "b".repeat(64),
      size_bytes: 128,
      review_status: "quarantined",
      publication_status: "unpublished",
      risk_level: "none",
      created_at: "2026-07-10T00:00:00Z",
      updated_at: "2026-07-10T00:00:00Z",
    };
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ artifact }), {
          status: 202,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const store = useArtifactStore();
    const archive = new File(["zip"], "plugin.zip", { type: "application/zip" });

    await store.submitUpload("plugin/id", archive);
    await store.submitGithub("plugin/id", "release-v1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining("/v1/plugins/plugin%2Fid/artifacts/upload"),
      expect.objectContaining({ method: "POST" }),
    );
    const uploadInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(uploadInit.method).toBe("POST");
    expect(uploadInit.body).toBeInstanceOf(FormData);
    expect((uploadInit.body as FormData).get("file")).toBe(archive);
    const githubInit = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(githubInit.body).toBe(JSON.stringify({ source_ref: "release-v1" }));
    expect(store.submitting).toBe(false);
  });

  it("surfaces structured API errors and clears loading state", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ code: "repo_version_changed", message: "仓库版本已变化" }), {
          status: 409,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const store = useArtifactStore();

    await expect(store.loadMine()).rejects.toThrow("仓库版本已变化");

    expect(store.loadingList).toBe(false);
  });

  it("sends the request-changes command and refreshes typed detail", async () => {
    const artifact = {
      id: "artifact-changes",
      plugin_id: "astrbot_plugin_demo",
      version: "v1.0.0",
      normalized_version: "1.0.0",
      source_type: "upload",
      archive_sha256: "c".repeat(64),
      size_bytes: 128,
      review_status: "changes_requested",
      publication_status: "unpublished",
      risk_level: "medium",
      created_at: "2026-07-10T00:00:00Z",
      updated_at: "2026-07-10T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ artifact }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ artifact, runs: [], findings: [], decisions: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const store = useArtifactStore();

    await store.requestChanges("artifact-changes", "请固定依赖版本");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining("/v1/admin/artifacts/artifact-changes/request-changes"),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ reason: "请固定依赖版本" }),
      }),
    );
    expect(store.detail?.artifact.review_status).toBe("changes_requested");
    expect(store.deciding).toBe(false);
  });
});
