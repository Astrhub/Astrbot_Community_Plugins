// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import ReviewDiffViewer from "./ReviewDiffViewer.vue";
import type {
  ArtifactDiff,
  ArtifactDiffContentResponse,
  ArtifactDiffListResponse,
} from "@/types/artifacts";

function diffFixture(): ArtifactDiff {
  return {
    id: "diff-1",
    artifact_id: "artifact-1",
    base_artifact_id: "artifact-0",
    base_file_id: "file-base",
    current_file_id: "file-current",
    path: "main.py",
    base_path: "main.py",
    change_type: "modified",
    base_sha256: "a".repeat(64),
    current_sha256: "b".repeat(64),
    base_tree_sha256: "c".repeat(64),
    current_tree_sha256: "d".repeat(64),
    stats: {
      base_size_bytes: 20,
      current_size_bytes: 24,
      base_line_count: 2,
      current_line_count: 2,
      forced_review: true,
      binary: false,
      added_lines: 1,
      deleted_lines: 1,
      hunk_count: 1,
      hunks_complete: true,
      hunks_omitted: 0,
      hunks_omitted_reason: "",
      hunks_truncated: false,
    },
    has_hunks: true,
    created_at: "2026-07-16T00:00:00Z",
  };
}

function diffList(diff: ArtifactDiff): ArtifactDiffListResponse {
  return {
    artifact_id: "artifact-1",
    tree_sha256: diff.current_tree_sha256,
    items: [diff],
    total: 1,
    limit: 200,
    offset: 0,
  };
}

function diffContent(diff: ArtifactDiff): ArtifactDiffContentResponse {
  return {
    artifact_id: "artifact-1",
    tree_sha256: diff.current_tree_sha256,
    diff,
    hunks_available: true,
    unavailable_reason: "",
    schema_version: "artifact-diff-hunks-v1",
    tool_version: "diff-v1",
    context_lines: 3,
    truncated: false,
    omitted_hunks: 0,
    hunks: [
      {
        id: "hunk-1",
        header: "@@ -1,2 +1,2 @@",
        old_start: 1,
        old_lines: 2,
        new_start: 1,
        new_lines: 2,
        lines: [
          {
            kind: "delete",
            prefix: "-",
            text: "value = 1",
            newline: "lf",
            old_line: 1,
            new_line: null,
          },
          {
            kind: "add",
            prefix: "+",
            text: "<script>window.__unsafe=1</script>",
            newline: "lf",
            old_line: null,
            new_line: 1,
          },
        ],
      },
    ],
  };
}

describe("ReviewDiffViewer", () => {
  it("renders diff lines as text and anchors added lines to the current file", async () => {
    const diff = diffFixture();
    const wrapper = mount(ReviewDiffViewer, {
      props: {
        diffs: diffList(diff),
        content: diffContent(diff),
        selectedDiffId: diff.id,
        selectedSide: "current",
        selectedHunkId: "",
        selectedLineStart: null,
        selectedLineEnd: null,
        loadingDiffs: false,
        loadingContent: false,
      },
    });

    expect(wrapper.text()).toContain("<script>window.__unsafe=1</script>");
    expect(wrapper.find("script").exists()).toBe(false);
    await wrapper.find(".diff-line--add").trigger("click");

    expect(wrapper.emitted("selectLine")).toEqual([
      [
        {
          fileId: "file-current",
          side: "current",
          lineStart: 1,
          lineEnd: 1,
          diffId: "diff-1",
          hunkId: "hunk-1",
        },
      ],
    ]);
  });

  it("keeps explicit incomplete diff coverage visible", () => {
    const diff = diffFixture();
    const unavailable = {
      ...diffContent(diff),
      hunks_available: false,
      unavailable_reason: "base_manifest_unavailable",
      hunks: [],
    };
    const wrapper = mount(ReviewDiffViewer, {
      props: {
        diffs: diffList(diff),
        content: unavailable,
        selectedDiffId: diff.id,
        selectedSide: "current",
        selectedHunkId: "",
        selectedLineStart: null,
        selectedLineEnd: null,
        loadingDiffs: false,
        loadingContent: false,
      },
    });

    expect(wrapper.text()).toContain("base_manifest_unavailable");
    expect(wrapper.text()).not.toContain("无风险");
  });
});
