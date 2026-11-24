// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { usePluginStore } from "../stores/plugins";

const { useHeadMock } = vi.hoisted(() => ({ useHeadMock: vi.fn() }));

vi.mock("@unhead/vue", () => ({ useHead: useHeadMock }));

import { absoluteSiteUrl, useSeo, useSeoContext } from "./useSeo";

describe("useSeo", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    useHeadMock.mockClear();
  });

  it("derives canonical, brand title, og:image, and JSON-LD from dynamic siteConfig", () => {
    const store = usePluginStore();
    store.siteConfig = {
      ...store.siteConfig,
      name: "自托管市场",
      web_url: "https://market.selfhost.example/",
    };

    const { siteOrigin, brandName, defaultOgImage, absoluteSiteUrl: toUrl } = useSeoContext();
    expect(siteOrigin.value).toBe("https://market.selfhost.example");
    expect(brandName.value).toBe("自托管市场");
    expect(defaultOgImage.value).toBe("https://market.selfhost.example/og-cover.png");
    expect(toUrl("/author/alice")).toBe("https://market.selfhost.example/author/alice");
    expect(absoluteSiteUrl("/plugin/alice/demo")).toBe(
      "https://market.selfhost.example/plugin/alice/demo",
    );

    useSeo({
      title: "Alice 的插件",
      description: "查看 Alice 发布的插件。",
      path: "/author/alice",
      jsonLd: { "@type": "ProfilePage", url: toUrl("/author/alice") },
    });

    expect(useHeadMock).toHaveBeenCalledTimes(1);
    const headFactory = useHeadMock.mock.calls[0][0] as () => Record<string, unknown>;
    const head = headFactory();
    expect(head.title).toBe("Alice 的插件 - 自托管市场");
    expect(head.link).toContainEqual({
      rel: "canonical",
      href: "https://market.selfhost.example/author/alice",
    });
    expect(head.meta).toEqual(
      expect.arrayContaining([
        { name: "description", content: "查看 Alice 发布的插件。" },
        { name: "robots", content: "index,follow" },
        { property: "og:site_name", content: "自托管市场" },
        { property: "og:image", content: "https://market.selfhost.example/og-cover.png" },
        { property: "og:url", content: "https://market.selfhost.example/author/alice" },
      ]),
    );
    expect(head.script).toEqual([
      {
        id: "page-jsonld",
        type: "application/ld+json",
        innerHTML: JSON.stringify({
          "@type": "ProfilePage",
          url: "https://market.selfhost.example/author/alice",
        }),
      },
    ]);
  });

  it("falls back to window.location.origin when siteConfig.web_url is empty or invalid", () => {
    const store = usePluginStore();
    store.siteConfig = { ...store.siteConfig, web_url: "not-a-valid-url", name: "" };

    const { siteOrigin, brandName } = useSeoContext();
    expect(siteOrigin.value).toBe(window.location.origin);
    expect(brandName.value).toBe("Astrhub 插件市场");
  });
});
