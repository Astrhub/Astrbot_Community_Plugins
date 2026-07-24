// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import {
  SITE_CONFIG_CACHE_KEY,
  USER_PREVIEW_CACHE_KEY,
  USER_PREVIEW_MAX_AGE_MS,
  clearCachedUserPreview,
  readCachedSiteConfig,
  readCachedUserPreview,
  writeCachedSiteConfig,
  writeCachedUserPreview,
} from "./bootstrapCache";

describe("bootstrap cache", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("persists public site configuration for synchronous bootstrap", () => {
    const config = {
      name: "Cached Market",
      icon_url: "/logo.webp",
      web_url: "https://plugins.example.com",
      subtitle: "subtitle",
      description: "description",
      contact_email: "",
      docs_url: "https://docs.example.com",
      auth: {
        github_login_enabled: true,
        public_login_enabled: true,
        login_agreement_enabled: false,
        login_agreement_text: "",
        service_terms_enabled: false,
        service_terms_text: "",
        terms_revision: "",
      },
      market: {
        submissions_enabled: true,
        comments_enabled: true,
        likes_enabled: true,
        max_plugin_tags: 8,
      },
    };

    writeCachedSiteConfig(config);

    expect(readCachedSiteConfig()).toEqual(config);
    expect(JSON.parse(localStorage.getItem(SITE_CONFIG_CACHE_KEY) || "{}").version).toBe(1);
  });

  it("stores only display-safe user fields and clears them explicitly", () => {
    writeCachedUserPreview({
      id: "user-1",
      role: "core_admin",
      github_login: "reviewer",
      avatar_url: "https://example.com/avatar.png",
      access_token: "must-not-be-cached",
    });

    const raw = localStorage.getItem(USER_PREVIEW_CACHE_KEY) || "";
    expect(readCachedUserPreview()).toEqual({
      github_login: "reviewer",
      avatar_url: "https://example.com/avatar.png",
    });
    expect(raw).not.toContain("core_admin");
    expect(raw).not.toContain("must-not-be-cached");

    clearCachedUserPreview();
    expect(readCachedUserPreview()).toBeNull();
  });

  it("expires stale user previews", () => {
    vi.spyOn(Date, "now").mockReturnValue(USER_PREVIEW_MAX_AGE_MS + 10);
    localStorage.setItem(
      USER_PREVIEW_CACHE_KEY,
      JSON.stringify({
        version: 1,
        savedAt: 1,
        value: { github_login: "stale-user" },
      }),
    );

    expect(readCachedUserPreview()).toBeNull();
    expect(localStorage.getItem(USER_PREVIEW_CACHE_KEY)).toBeNull();
  });
});
