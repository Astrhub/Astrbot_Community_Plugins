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

  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;

    if (pathname === "/v1/site") {
      return json(route, {
        name: "Astrhub Plugins Market",
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
      return json(route, {
        id: "admin-1",
        role: "admin",
        github_login: "reviewer",
        internal_username: "reviewer",
      });
    }
    if (pathname === "/v1/me/notifications/unread-count") return json(route, { count: 0 });
    if (pathname === "/v1/me/plugins") return json(route, { items: [plugin] });
    if (pathname === "/v1/me/api-keys") return json(route, { items: [] });
    if (pathname === "/v1/plugins") return json(route, { items: [] });
    if (pathname === `/v1/plugins/${plugin.id}/unlist` && request.method() === "POST") {
      unlistRequested = true;
      return json(route, { ...plugin, status: "unlisted" });
    }

    return json(route, { error: `Unhandled test route: ${request.method()} ${pathname}` }, 404);
  });

  await page.goto("/settings/personal", { waitUntil: "domcontentloaded" });
  await page.locator(".n-tabs-tab__label").getByText("我的插件", { exact: true }).click();

  await expect(page.getByRole("heading", { name: "插件管理" })).toBeVisible();
  await expect(page.getByText("我的测试插件", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "保存标签" })).toBeVisible();

  await page.getByRole("button", { name: "下架" }).click();
  await page.locator(".n-dialog").getByRole("button", { name: "下架" }).click();
  await expect(page.getByText("已下架", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "申请上架" })).toBeVisible();
  expect(unlistRequested).toBe(true);

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByText("审查台", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "reviewer" }).click();
  await expect(page.getByText("审查工作台", { exact: true })).toHaveCount(1);
});
