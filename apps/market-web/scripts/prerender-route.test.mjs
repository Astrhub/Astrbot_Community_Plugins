import { describe, expect, it } from "vite-plus/test";
import { markPrerenderPath, normalizePrerenderPath } from "./prerender-route.mjs";

describe("prerender route markers", () => {
  it("normalizes root and trailing slashes", () => {
    expect(normalizePrerenderPath("/")).toBe("/");
    expect(normalizePrerenderPath("/docs/rest/")).toBe("/docs/rest");
  });

  it("marks the html element with the captured route", () => {
    expect(markPrerenderPath('<html lang="zh-CN"><head></head></html>', "/docs/rest/")).toContain(
      'data-prerender-path="/docs/rest"',
    );
  });

  it("replaces an existing marker instead of duplicating it", () => {
    const html = '<html data-prerender-path="/old" class="dark"><head></head></html>';
    const marked = markPrerenderPath(html, "/plugin/example");

    expect(marked.match(/data-prerender-path/g)).toHaveLength(1);
    expect(marked).toContain('data-prerender-path="/plugin/example"');
  });

  it("rejects invalid documents", () => {
    expect(() => markPrerenderPath("<head></head>", "/")).toThrow(/html element/);
  });
});
