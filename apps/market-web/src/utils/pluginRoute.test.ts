import { describe, expect, it } from "vite-plus/test";
import { pluginDetailPath } from "./pluginRoute";

describe("pluginDetailPath", () => {
  it("uses the market OAuth username even when the repository belongs to an organization", () => {
    expect(
      pluginDetailPath({
        id: "plugin_opaque",
        name: "astrbot_plugin_same",
        owner_github_login: "Alice",
      }),
    ).toBe("/plugin/Alice/astrbot_plugin_same");
    expect(pluginDetailPath({ id: "astrbot_plugin_legacy" })).toBe("/plugin/astrbot_plugin_legacy");
  });
});
