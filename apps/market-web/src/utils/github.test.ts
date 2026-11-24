import { describe, expect, it } from "vite-plus/test";
import {
  DEFAULT_PLUGIN_LOGO_URL,
  detectGitProvider,
  getGitProviderLabel,
  isGithubRepoUrl,
  isSupportedGitRepoUrl,
  resolvePluginLogoUrl,
  setDefaultPluginLogo,
} from "./github";

describe("resolvePluginLogoUrl", () => {
  it("uses an explicit plugin logo when one is provided", () => {
    const logo = "https://example.com/logo.png";

    expect(resolvePluginLogoUrl({ logo, repo: "https://github.com/owner/repo" })).toBe(logo);
  });

  it("uses the default immediately when the API does not provide a logo", () => {
    const url = resolvePluginLogoUrl({
      logo: "",
      repo: "https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_bug_catcher",
    });

    expect(url).toBe(DEFAULT_PLUGIN_LOGO_URL);
    expect(url).not.toContain("cdn.jsdelivr.net");
    expect(url).not.toContain("astrbot_plugin_bug_catcher");
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

describe("multi-platform Git URL helpers", () => {
  it("recognizes all five allowlisted OAuth Git providers and GitLab nested groups", () => {
    expect(detectGitProvider("https://github.com/alice/astrbot_plugin_a")).toBe("github");
    expect(detectGitProvider("https://gitlab.com/org/team/sub/astrbot_plugin_b.git")).toBe(
      "gitlab",
    );
    expect(detectGitProvider("https://gitee.com/alice/astrbot_plugin_c")).toBe("gitee");
    expect(detectGitProvider("https://atomgit.com/alice/astrbot_plugin_d")).toBe("atomgit");
    expect(detectGitProvider("https://gitcode.com/alice/astrbot_plugin_e.git/")).toBe("atomgit");
    expect(detectGitProvider("https://gitea.com/alice/astrbot_plugin_f")).toBe("gitea");
    expect(isGithubRepoUrl("https://github.com/alice/astrbot_plugin_a")).toBe(true);
    expect(isGithubRepoUrl("https://gitlab.com/alice/astrbot_plugin_b")).toBe(false);
    expect(getGitProviderLabel("atomgit")).toBe("AtomGit");
  });

  it("rejects arbitrary hosts, non-https URLs, credentials, query params, and traversal", () => {
    for (const invalid of [
      "http://github.com/alice/astrbot_plugin_a",
      "https://bitbucket.org/alice/astrbot_plugin_a",
      "https://codeberg.org/alice/astrbot_plugin_a",
      "https://user:pass@gitlab.com/alice/astrbot_plugin_a",
      "https://gitee.com/alice/astrbot_plugin_a?foo=1",
      "https://gitea.com/alice/astrbot_plugin_a#readme",
      "https://gitee.com/org/sub/astrbot_plugin_too_deep",
      "https://gitlab.com/alice/../astrbot_plugin_bad",
    ]) {
      expect(isSupportedGitRepoUrl(invalid)).toBe(false);
    }
  });
});
