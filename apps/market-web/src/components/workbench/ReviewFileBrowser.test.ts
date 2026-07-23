// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import ReviewFileBrowser from "./ReviewFileBrowser.vue";
import type {
  ArtifactFile,
  ArtifactFileContentResponse,
  ArtifactFileListResponse,
} from "@/types/artifacts";

function textFile(overrides: Partial<ArtifactFile> = {}): ArtifactFile {
  return {
    id: "file-main",
    artifact_id: "artifact-1",
    path: "main.py",
    language: "python",
    mime_type: "text/x-python",
    sha256: "a".repeat(64),
    size_bytes: 64,
    line_count: 2,
    is_text: true,
    is_entrypoint: true,
    is_reachable: true,
    graph_status: "complete",
    content_available: true,
    ...overrides,
  };
}

function fileList(file: ArtifactFile): ArtifactFileListResponse {
  return {
    artifact_id: "artifact-1",
    tree_sha256: "b".repeat(64),
    items: [file],
    total: 1,
    limit: 200,
    offset: 0,
  };
}

function content(file: ArtifactFile): ArtifactFileContentResponse {
  return {
    artifact_id: "artifact-1",
    tree_sha256: "b".repeat(64),
    file,
    encoding: "utf-8",
    start_line: 1,
    end_line: 2,
    total_lines: 2,
    truncated: false,
    lines: [
      { number: 1, text: "<img src=x onerror=window.__unsafe=1>" },
      { number: 2, text: "value = 2" },
    ],
  };
}

describe("ReviewFileBrowser", () => {
  it("renders untrusted source as text and emits a typed line anchor", async () => {
    const file = textFile();
    const wrapper = mount(ReviewFileBrowser, {
      props: {
        files: fileList(file),
        content: content(file),
        selectedFileId: file.id,
        selectedLineStart: null,
        selectedLineEnd: null,
        loadingFiles: false,
        loadingContent: false,
      },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("<img src=x onerror=window.__unsafe=1>");
    expect(wrapper.find("img").exists()).toBe(false);
    const secondLine = wrapper.findAll(".code-line")[1];
    expect(secondLine).toBeDefined();
    await secondLine?.trigger("click");
    await flushPromises();

    expect(wrapper.emitted("selectLine")).toEqual([
      [{ fileId: "file-main", lineStart: 2, lineEnd: 2 }],
    ]);
  });

  it("shows binary metadata without requesting or rendering source lines", () => {
    const file = textFile({
      id: "file-binary",
      path: "assets/icon.png",
      mime_type: "image/png",
      is_text: false,
      content_available: false,
      line_count: null,
    });
    const wrapper = mount(ReviewFileBrowser, {
      props: {
        files: fileList(file),
        content: null,
        selectedFileId: file.id,
        selectedLineStart: null,
        selectedLineEnd: null,
        loadingFiles: false,
        loadingContent: false,
      },
    });

    expect(wrapper.text()).toContain("binary");
    expect(wrapper.text()).toContain("二进制或不可用正文不会发送到浏览器");
    expect(wrapper.find(".code-line").exists()).toBe(false);
  });
});
