// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { flushPromises } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import ArtifactSubmissionPanel from "./ArtifactSubmissionPanel.vue";
import type { Plugin } from "@/types";
import type { PluginArtifact } from "@/types/artifacts";

const plugin: Plugin = {
  id: "astrbot_plugin_demo",
  name: "astrbot_plugin_demo",
  display_name: "Demo",
  version: "v1.0.0",
  logo: "",
  tags: [],
  category: "utilities",
  stars: 0,
  likes: 0,
  comments_count: 0,
  list_index: 0,
};

const supersedes: PluginArtifact = {
  id: "artifact-old",
  plugin_id: "astrbot_plugin_demo",
  version: "v1.0.0",
  normalized_version: "1.0.0",
  source_type: "github",
  archive_sha256: "a".repeat(64),
  size_bytes: 128,
  review_status: "changes_requested",
  publication_status: "unpublished",
  risk_level: "medium",
  created_at: "2026-07-16T00:00:00Z",
  updated_at: "2026-07-16T00:00:00Z",
};

describe("ArtifactSubmissionPanel", () => {
  it("binds a changes-requested resubmission to the immutable prior artifact", async () => {
    const wrapper = mount(ArtifactSubmissionPanel, {
      props: { plugins: [plugin], submitting: false, supersedesArtifact: supersedes },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("重新提交修订版");
    expect(wrapper.text()).toContain("保留原版本、评论和审查历史");
    const file = new File(["zip"], "plugin.zip", { type: "application/zip" });
    const input = wrapper.get('input[type="file"]');
    Object.defineProperty(input.element, "files", { value: [file] });
    await input.trigger("change");
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("重新提交 ZIP"))
      ?.trigger("click");

    expect(wrapper.emitted("upload")).toEqual([
      [
        {
          pluginId: "astrbot_plugin_demo",
          file,
          supersedesArtifactId: "artifact-old",
        },
      ],
    ]);
  });
});
