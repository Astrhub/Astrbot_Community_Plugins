// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import router from "./index";

describe("plugin workbench route guard", () => {
  beforeEach(async () => {
    setActivePinia(createPinia());
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 401 })),
    );
    await router.push("/");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("redirects anonymous users and preserves the requested workbench URL", async () => {
    expect(router.resolve("/plugin-workbench").meta.noindex).toBe(true);

    await router.push("/plugin-workbench?status=pending_review");

    expect(router.currentRoute.value.name).toBe("Home");
    expect(router.currentRoute.value.query.login).toBe("required");
    expect(router.currentRoute.value.query.redirect).toBe(
      "/plugin-workbench?status=pending_review",
    );
  });

  it("routes the legacy admin review URL through the new workbench", async () => {
    await router.push("/admin/plugins");

    expect(router.currentRoute.value.name).toBe("Home");
    expect(router.currentRoute.value.query.login).toBe("required");
    expect(router.currentRoute.value.query.redirect).toBe("/plugin-workbench");
  });

  it("routes legacy 2-segment /plugin/:id paths to NotFound and keeps 3-segment plugin routes", () => {
    const legacy = router.resolve("/plugin/astrbot_plugin_demo");
    expect(legacy.name).toBe("NotFound");
    expect(legacy.meta.noindex).toBe(true);

    const canonical = router.resolve("/plugin/Alice/astrbot_plugin_demo");
    expect(canonical.name).toBe("PluginDetails");
    expect(canonical.params).toEqual({ username: "Alice", name: "astrbot_plugin_demo" });
  });
});
