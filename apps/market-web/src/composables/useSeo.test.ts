import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import type { injectHead } from "@unhead/vue";

const { useHeadMock } = vi.hoisted(() => ({ useHeadMock: vi.fn() }));

vi.mock("@unhead/vue", () => ({ useHead: useHeadMock }));

import { commitSeo } from "./useSeo";

describe("commitSeo", () => {
  beforeEach(() => {
    useHeadMock.mockClear();
  });

  it("commits complete author metadata in one head update", () => {
    const headManager = {} as ReturnType<typeof injectHead>;
    commitSeo(
      {
        title: "Alice 的插件",
        description: "查看 Alice 发布的插件。",
        path: "/author/alice",
        jsonLd: { "@type": "ProfilePage", url: "https://plugins.eloina.cn/author/alice" },
      },
      headManager,
    );

    expect(useHeadMock).toHaveBeenCalledTimes(1);
    expect(useHeadMock.mock.calls[0][1]).toEqual({ head: headManager });
    const head = useHeadMock.mock.calls[0][0];
    expect(head.title).toBe("Alice 的插件 - Astrhub 插件市场");
    expect(head.link).toContainEqual({
      rel: "canonical",
      href: "https://plugins.eloina.cn/author/alice",
    });
    expect(head.meta).toEqual(
      expect.arrayContaining([
        { name: "description", content: "查看 Alice 发布的插件。" },
        { name: "robots", content: "index,follow" },
        { property: "og:image", content: "https://plugins.eloina.cn/og-cover.png" },
        { property: "og:url", content: "https://plugins.eloina.cn/author/alice" },
      ]),
    );
    expect(head.script).toEqual([
      {
        id: "page-jsonld",
        type: "application/ld+json",
        innerHTML: JSON.stringify({
          "@type": "ProfilePage",
          url: "https://plugins.eloina.cn/author/alice",
        }),
      },
    ]);
  });
});
