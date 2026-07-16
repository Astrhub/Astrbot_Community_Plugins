// @vitest-environment jsdom

import { reactive } from "vue";
import { describe, expect, it, vi } from "vite-plus/test";
import type { LocationQuery, RouteLocationNormalizedLoaded, Router } from "vue-router";
import { buildReviewQuery, parseReviewSelection, useReviewSelection } from "./useReviewSelection";

describe("review workspace route selection", () => {
  it("restores a valid deep link and normalizes invalid values", () => {
    expect(
      parseReviewSelection({
        artifact: "artifact-1",
        view: "diff",
        file: "file-1",
        diff: "diff-1",
        hunk: "hunk-1",
        side: "base",
        line: "12",
        line_end: "18",
        status: "pending_review",
        risk: "critical",
      }),
    ).toEqual({
      artifactId: "artifact-1",
      view: "diff",
      fileId: "file-1",
      diffId: "diff-1",
      hunkId: "hunk-1",
      side: "base",
      lineStart: 12,
      lineEnd: 18,
      status: "pending_review",
      risk: "critical",
    });

    const invalid = parseReviewSelection({
      view: "unsafe",
      side: "unknown",
      line: "-5",
      line_end: "abc",
      status: "missing",
      risk: "severe",
    });
    expect(invalid.view).toBe("summary");
    expect(invalid.side).toBe("current");
    expect(invalid.lineStart).toBeNull();
    expect(invalid.lineEnd).toBeNull();
    expect(invalid.status).toBe("");
    expect(invalid.risk).toBe("");
  });

  it("merges query state and removes cleared selections", () => {
    const query = buildReviewQuery(
      { artifact: "old", status: "approved", file: "file-old" },
      { artifact: "new", status: "", file: "", view: "summary" },
    );

    expect(query).toMatchObject({ artifact: "new", view: "summary" });
    expect(query.status).toBeUndefined();
    expect(query.file).toBeUndefined();
  });

  it("writes line selection through router.replace without keeping selection in a store", async () => {
    const route = reactive({
      query: { artifact: "artifact-1", view: "diff" } as LocationQuery,
    }) as unknown as RouteLocationNormalizedLoaded;
    const replace = vi.fn(async ({ query }: { query: LocationQuery }) => {
      route.query = query;
    });
    const router = { replace } as unknown as Router;
    const state = useReviewSelection(route, router);

    await state.selectLine({
      fileId: "file-current",
      diffId: "diff-1",
      hunkId: "hunk-1",
      side: "current",
      lineStart: 24,
      lineEnd: 26,
    });

    expect(replace).toHaveBeenCalledWith({
      query: expect.objectContaining({
        artifact: "artifact-1",
        view: "diff",
        file: "file-current",
        diff: "diff-1",
        hunk: "hunk-1",
        side: "current",
        line: "24",
        line_end: "26",
      }),
    });
    expect(state.selection.value.lineStart).toBe(24);
  });
});
