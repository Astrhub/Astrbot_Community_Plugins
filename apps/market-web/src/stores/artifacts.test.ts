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

    await store.submitUpload("plugin/id", archive, "artifact-old");
    await store.submitGithub("plugin/id", "release-v1", "artifact-old");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining("/v1/plugins/plugin%2Fid/artifacts/upload"),
      expect.objectContaining({ method: "POST" }),
    );
    const uploadInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(uploadInit.method).toBe("POST");
    expect(uploadInit.body).toBeInstanceOf(FormData);
    expect((uploadInit.body as FormData).get("file")).toBe(archive);
    expect((uploadInit.body as FormData).get("supersedes_artifact_id")).toBe("artifact-old");
    const githubInit = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(githubInit.body).toBe(
      JSON.stringify({ source_ref: "release-v1", supersedes_artifact_id: "artifact-old" }),
    );
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

  it("does not let a completed decision refresh overwrite a newly selected artifact", async () => {
    const artifactA = {
      id: "artifact/A",
      plugin_id: "astrbot_plugin_demo",
      version: "v1.0.0",
      normalized_version: "1.0.0",
      source_type: "upload",
      archive_sha256: "a".repeat(64),
      size_bytes: 128,
      review_status: "pending_review",
      publication_status: "unpublished",
      risk_level: "high",
      created_at: "2026-07-10T00:00:00Z",
      updated_at: "2026-07-10T00:00:00Z",
    };
    const artifactB = {
      ...artifactA,
      id: "artifact/B",
      version: "v2.0.0",
      normalized_version: "2.0.0",
      risk_level: "low",
    };
    let resolveDecision: ((response: Response) => void) | undefined;
    const decision = new Promise<Response>((resolve) => {
      resolveDecision = resolve;
    });
    const requestedUrls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      requestedUrls.push(url);
      if (init?.method === "POST") return decision;
      const artifact = url.endsWith("artifact%2FB") ? artifactB : artifactA;
      return new Response(JSON.stringify({ artifact, runs: [], findings: [], decisions: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const store = useArtifactStore();

    await store.loadDetail("artifact/A");
    const pendingDecision = store.reject("artifact/A", "存在严重风险");
    await store.loadDetail("artifact/B");
    resolveDecision?.(
      new Response(JSON.stringify({ artifact: { ...artifactA, review_status: "rejected" } }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await pendingDecision;

    expect(store.detail?.artifact.id).toBe("artifact/B");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(requestedUrls[0]).toContain("artifact%2FA");
    expect(requestedUrls[1]).toContain("artifact%2FA/reject");
  });

  it("keeps the newest queue filter result when an older request finishes last", async () => {
    let resolveOlder: ((response: Response) => void) | undefined;
    const older = new Promise<Response>((resolve) => {
      resolveOlder = resolve;
    });
    const highRisk = {
      id: "artifact-high",
      plugin_id: "astrbot_plugin_demo",
      version: "v1.0.0",
      normalized_version: "1.0.0",
      source_type: "upload",
      archive_sha256: "a".repeat(64),
      size_bytes: 128,
      review_status: "pending_review",
      publication_status: "unpublished",
      risk_level: "high",
      created_at: "2026-07-10T00:00:00Z",
      updated_at: "2026-07-10T00:00:00Z",
    };
    const criticalRisk = { ...highRisk, id: "artifact-critical", risk_level: "critical" };
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(older)
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [criticalRisk] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const store = useArtifactStore();

    const olderRequest = store.loadQueue({ riskLevel: "high" });
    await store.loadQueue({ riskLevel: "critical" });
    resolveOlder?.(
      new Response(JSON.stringify({ items: [highRisk] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await olderRequest;

    expect(store.items.map((item) => item.id)).toEqual(["artifact-critical"]);
    expect(store.loadingList).toBe(false);
  });
});
