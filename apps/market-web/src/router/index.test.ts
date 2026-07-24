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
});
