import { describe, expect, it } from "vite-plus/test";
import { DEFAULT_PLUGIN_LOGO_URL, resolvePluginLogoUrl, setDefaultPluginLogo } from "./github";

describe("resolvePluginLogoUrl", () => {
  it("uses an explicit plugin logo when one is provided", () => {
    const logo = "https://example.com/logo.png";

    expect(resolvePluginLogoUrl({ logo, repo: "https://github.com/owner/repo" })).toBe(logo);
  });

  it("builds a GitHub logo candidate for plugins without an explicit logo", () => {
    const url = resolvePluginLogoUrl({
      logo: "",
      repo: "https://github.com/chengzhi-c/astrbot_plugin_human_chat_quality",
    });

    expect(url).toContain("chengzhi-c");
    expect(url).toContain("astrbot_plugin_human_chat_quality");
    expect(url).toContain("logo.png");
    expect(url).not.toContain("${owner}");
    expect(url).not.toContain("${repo}");
  });

  it("strips .git suffixes before building the GitHub logo candidate", () => {
    const url = resolvePluginLogoUrl({
      repo: "https://github.com/example/astrbot_plugin_demo.git",
    });

    expect(url).toContain("example");
    expect(url).toContain("astrbot_plugin_demo");
    expect(url).not.toContain("astrbot_plugin_demo.git");
  });

  it("falls back to the market default logo when no usable logo source exists", () => {
    expect(resolvePluginLogoUrl({ logo: "", repo: "" })).toBe(DEFAULT_PLUGIN_LOGO_URL);
    expect(resolvePluginLogoUrl({ repo: "https://gitlab.com/owner/repo" })).toBe(
      DEFAULT_PLUGIN_LOGO_URL,
    );
  });

  it("upgrades legacy default-logo paths to the cache-busted brand asset", () => {
    expect(resolvePluginLogoUrl({ logo: "/plugin_default.png" })).toBe(
      "/plugin_default.png?v=20260725",
    );
  });

  it("replaces a failed remote logo with the market default", () => {
    const image = {
      getAttribute: () => "https://example.com/missing.png",
      onerror: () => undefined,
      src: "https://example.com/missing.png",
    } as unknown as HTMLImageElement;

    setDefaultPluginLogo({ currentTarget: image } as unknown as Event);

    expect(image.src).toBe(DEFAULT_PLUGIN_LOGO_URL);
  });
});
