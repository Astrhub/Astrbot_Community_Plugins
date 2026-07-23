// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { createDefaultReviewPolicy } from "@/utils/reviewPolicy";
import { ReviewPolicyApiError, useReviewPolicyStore } from "./reviewPolicy";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function policy(status: "draft" | "active" | "retired" = "active") {
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
      valid: true,
      schema_version: "1",
      policy_sha256: "a".repeat(64),
      readiness_checked: true,
      issues: [],
    },
    validated_at: "2026-07-17T00:00:00Z",
    activated_at: status === "active" ? "2026-07-17T00:00:00Z" : null,
    retired_at: status === "retired" ? "2026-07-17T00:00:00Z" : null,
    created_at: "2026-07-17T00:00:00Z",
    updated_at: "2026-07-17T00:00:00Z",
  };
}

describe("useReviewPolicyStore", () => {
  beforeEach(() => setActivePinia(createPinia()));
  afterEach(() => vi.unstubAllGlobals());

  it("loads only the active snapshot for a normal administrator", async () => {
    let requestedUrl = "";
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      requestedUrl = requestUrl(input);
      return jsonResponse({ policy: policy() });
    });
    vi.stubGlobal("fetch", fetchMock);
    const store = useReviewPolicyStore();

    await store.load(false);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(requestedUrl).toContain("/v1/admin/review-policies/active");
    expect(store.policies).toHaveLength(1);
    expect(store.operations).toBeNull();
  });

  it("loads core policy versions and operations in parallel", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.includes("review-tools/health")) {
        return jsonResponse({ health: { workers: [], tools: [] }, metrics: {} });
      }
      return jsonResponse({ items: [policy()] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const store = useReviewPolicyStore();

    await store.load(true);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(store.activePolicy?.id).toBe("policy-1");
    expect(store.operations).not.toBeNull();
  });

  it("keeps structured policy conflicts for UI recovery", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            detail: {
              code: "review_policy_activation_conflict",
              message: "Active policy changed",
            },
          },
          409,
        ),
      ),
    );
    const store = useReviewPolicyStore();

    const caught = await store
      .transitionPolicy("policy-1", "activate", "Activate")
      .catch((error: unknown) => error);

    expect(caught).toBeInstanceOf(ReviewPolicyApiError);
    expect(caught).toMatchObject({ status: 409, code: "review_policy_activation_conflict" });
    expect(store.mutating).toBe(false);
  });

  it("sends no secret values while preserving server config references", async () => {
    let body = "";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        body = typeof init?.body === "string" ? init.body : "";
        return jsonResponse({
          policy: policy("draft"),
          diff: {
            redacted: true,
            before_sha256: "",
            after_sha256: "a".repeat(64),
            added_paths: [],
            removed_paths: [],
            changed_paths: [],
            path_count: 0,
            truncated: false,
          },
        });
      }),
    );
    const store = useReviewPolicyStore();

    await store.createDraft({
      version: "policy-v2",
      policy: createDefaultReviewPolicy(),
      reason: "New policy",
    });

    expect(body).toContain("config:llm-default");
    expect(body).not.toContain("api_key");
    expect(body).not.toContain("endpoint_url");
    expect(store.lastDiff?.redacted).toBe(true);
  });
});
