import { expect, test, type Route } from "@playwright/test";

const plugin = {
  id: "astrbot_plugin_owned",
  name: "astrbot_plugin_owned",
  display_name: "我的测试插件",
  desc: "作者可编辑的插件",
  tags: ["工具"],
  author: "reviewer",
  stars: 1,
  likes: 0,
  comments_count: 0,
  version: "1.0.0",
  category: "utilities",
  repo: "https://github.com/demo/astrbot_plugin_owned",
  logo: "",
  pinned: false,
  status: "listed",
};

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

test("authors keep plugin editing while admins have one review entry", async ({ page }) => {
  let unlistRequested = false;
  let savedPayload: Record<string, unknown> | null = null;
  let synthesizedLogoRequests = 0;
  let releaseSiteConfig: () => void = () => undefined;
  let releaseCurrentUser: () => void = () => undefined;
  const siteConfigGate = new Promise<void>((resolve) => {
    releaseSiteConfig = resolve;
  });
  const currentUserGate = new Promise<void>((resolve) => {
    releaseCurrentUser = resolve;
  });

  await page.route("**/demo/astrbot_plugin_owned*/logo.png", (route) => {
    synthesizedLogoRequests += 1;
    return route.abort();
  });

  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;

    if (pathname === "/v1/site") {
      await siteConfigGate;
      return json(route, {
        name: "自定义插件市场",
        icon_url: "/logo.webp",
        auth: { github_login_enabled: true },
        market: { max_plugin_tags: 8, comments_enabled: true, likes_enabled: true },
      });
    }
    if (pathname === "/v1/setup/status") {
      return json(route, {
        required: false,
        missing: [],
        database_configured: true,
        redis_configured: true,
        saved_setup: {},
        restart_required: false,
      });
    }
    if (pathname === "/v1/me") {
      await currentUserGate;
      return json(route, {
        id: "admin-1",
        role: "admin",
        github_login: "reviewer",
        internal_username: "reviewer",
        avatar_url: "/plugin_default.png?v=20260725",
      });
    }
    if (pathname === "/v1/me/notifications/unread-count") return json(route, { count: 0 });
    if (pathname === "/v1/me/plugins") return json(route, { items: [plugin] });
    if (pathname === "/v1/me/api-keys") return json(route, { items: [] });
    if (pathname === "/v1/plugins") return json(route, { items: [] });
    if (pathname === `/v1/plugins/${plugin.id}` && request.method() === "PATCH") {
      savedPayload = request.postDataJSON() as Record<string, unknown>;
      return json(route, { ...plugin, ...savedPayload });
    }
    if (pathname === `/v1/plugins/${plugin.id}/unlist` && request.method() === "POST") {
      unlistRequested = true;
      return json(route, { ...plugin, status: "unlisted" });
    }

    return json(route, { error: `Unhandled test route: ${request.method()} ${pathname}` }, 404);
  });

  await page.goto("/", { waitUntil: "commit" });
  const brandName = page.locator(".brand-name");
  await brandName.waitFor({ state: "attached" });
  await expect(brandName).toBeHidden();
  releaseSiteConfig();
  await page.waitForLoadState("domcontentloaded");
  await expect(brandName).toHaveText("自定义插件市场");
  await expect(brandName).toBeVisible();
  const loginTrigger = page.locator(".login-trigger");
  await loginTrigger.waitFor({ state: "attached" });
  await expect(loginTrigger).toBeHidden();
  releaseCurrentUser();
  await expect(page.getByRole("button", { name: "账户：reviewer" })).toBeVisible();
  await expect(loginTrigger).toHaveCount(0);

  await page.goto("/settings/personal", { waitUntil: "domcontentloaded" });
  await page.locator(".n-tabs-tab__label").getByText("我的插件", { exact: true }).click();

  await expect(page.getByText("我的测试插件", { exact: true })).toBeVisible();
  await expect(page.getByText("astrbot_plugin_owned", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "仓库 ↗" })).toBeVisible();
  await expect(page.locator(".pm-logo")).toHaveAttribute("src", "/plugin_default.png?v=20260725");
  expect(synthesizedLogoRequests).toBe(0);

  await page.getByRole("button", { name: "编辑" }).click();
  await expect(page.getByText("展示名称", { exact: true })).toBeVisible();
  const editor = page.locator(".pm-editor");
  await editor.getByRole("textbox").first().fill("预览版插件名称");
  await editor.getByRole("button", { name: "保存修改" }).click();
  await expect(page.getByText("预览版插件名称", { exact: true })).toBeVisible();
  expect(savedPayload).toMatchObject({
    display_name: "预览版插件名称",
    desc: plugin.desc,
    tags: plugin.tags,
    category: plugin.category,
    social_link: "",
  });

  await page.getByRole("button", { name: "下架" }).click();
  await page.locator(".n-dialog").getByRole("button", { name: "下架" }).click();
  await expect(page.getByText("已下架", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "申请上架" })).toBeVisible();
  expect(unlistRequested).toBe(true);

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByText("审查台", { exact: true })).toHaveCount(0);
  await expect(page.locator(".user-avatar__image")).toHaveAttribute(
    "src",
    "/plugin_default.png?v=20260725",
  );
  await page.getByRole("button", { name: "账户：reviewer" }).click();
  await expect(page.getByText("审查工作台", { exact: true })).toHaveCount(1);
});

test("machine-readable notice stays collapsed and can be restored", async ({ page }) => {
  await page.addInitScript(() => {
    const initializedKey = "astrbot_docs_endpoint_alert_test_initialized";
    if (sessionStorage.getItem(initializedKey)) return;
    localStorage.removeItem("astrbot_docs_endpoint_alert_collapsed");
    sessionStorage.setItem(initializedKey, "true");
  });
  await page.route("**/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/v1/site") {
      return json(route, {
        name: "Astrhub 插件市场",
        icon_url: "/logo.webp",
        auth: { github_login_enabled: true },
        market: { max_plugin_tags: 8, comments_enabled: true, likes_enabled: true },
      });
    }
    if (pathname === "/v1/setup/status") {
      return json(route, {
        required: false,
        missing: [],
        database_configured: true,
        redis_configured: true,
        saved_setup: {},
        restart_required: false,
      });
    }
    if (pathname === "/v1/me") return json(route, { error: "Unauthorized" }, 401);
    if (pathname === "/v1/plugins") return json(route, { items: [] });
    return json(route, { error: "Not found" }, 404);
  });
  await page.route("**/openapi.json", (route) =>
    json(route, { openapi: "3.1.0", info: { title: "Test API", version: "1.0.0" }, paths: {} }),
  );

  await page.goto("/docs/rest", { waitUntil: "domcontentloaded" });
  await expect(page.getByText("机器可读入口", { exact: true })).toBeVisible();
  await page.locator(".endpoint-alert .n-base-close").click();
  await expect(page.getByRole("button", { name: "展开机器可读入口" })).toBeVisible();

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByRole("button", { name: "展开机器可读入口" })).toBeVisible();
  await page.getByRole("button", { name: "展开机器可读入口" }).click();
  await expect(page.getByText("机器可读入口", { exact: true })).toBeVisible();

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByText("机器可读入口", { exact: true })).toBeVisible();
});
