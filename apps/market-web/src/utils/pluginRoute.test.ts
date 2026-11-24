import { describe, expect, it } from "vite-plus/test";
import { pluginDetailPath } from "./pluginRoute";

describe("pluginDetailPath", () => {
  it("uses owner_username first and falls back to owner_github_login", () => {
    expect(
      pluginDetailPath({
        id: "plugin_opaque",
        name: "astrbot_plugin_same",
        owner_username: "AlicePublic",
        owner_github_login: "AliceGH",
      }),
    ).toBe("/plugin/AlicePublic/astrbot_plugin_same");
    expect(
      pluginDetailPath({
        id: "plugin_opaque",
        name: "astrbot_plugin_same",
        owner_github_login: "Alice",
      }),
    ).toBe("/plugin/Alice/astrbot_plugin_same");
    expect(pluginDetailPath({ id: "astrbot_plugin_legacy" })).toBeNull();
    expect(
      pluginDetailPath({
        id: "astrbot_plugin_legacy",
        name: "astrbot_plugin_legacy",
        canonical_path: "",
      }),
    ).toBeNull();
    expect(
      pluginDetailPath({
        id: "astrbot_plugin_legacy",
        canonical_path: "/plugin/astrbot_plugin_legacy",
      }),
    ).toBeNull();
    expect(
      pluginDetailPath({
        id: "plugin_opaque",
        canonical_path: "/plugin/AlicePublic/astrbot_plugin_same",
      }),
    ).toBe("/plugin/AlicePublic/astrbot_plugin_same");
  });
});
