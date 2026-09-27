import { expect, test, type Page } from "@playwright/test";

async function mockMarket(page: Page) {
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  let plugin = {
    id: "cdn-demo",
    name: "astrbot_plugin_demo",
    display_name: "CDN 测试插件",
    desc: "用于验证社区源选择",
    author: "Alice",
    repo: "https://github.com/alice/astrbot_plugin_demo",
    owner_github_login: "alice",
    version: "v1.0.0",
    status: "listed",
    cdn_enabled: true,
    tags: [],
    category: "other",
  };
  await page.addInitScript(() => {
    (window as unknown as Record<string, unknown>).__ASTRHUB_PRERENDER__ = true;
  });
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let body: unknown = { items: [] };
    if (path === "/v1/site") body = { market: { plugin_auto_approve_enabled: true } };
    else if (path === "/v1/setup/status") body = { required: false };
    else if (path === "/v1/me")
      body = { id: "alice", role: "user", github_login: "alice", username: "alice" };
    else if (path === "/v1/me/notifications/unread-count") body = { count: 0 };
    else if (path === "/v1/me/plugins") body = { items: [plugin] };
    else if (path === "/v1/me/github/repositories")
      body = {
        items: [
          {
            id: "123",
            name: plugin.name,
            full_name: "alice/astrbot_plugin_demo",
            repo: plugin.repo,
            owner: "alice",
            owner_type: "User",
          },
        ],
        next_page: null,
      };
    else if (path === "/v1/plugins/submissions/metadata-preview") body = plugin;
    else if (request.method() === "POST" && path === "/v1/plugins/submissions") {
      const data = request.postDataJSON();
      writes.push({ path, body: data });
      body = { ...plugin, ...data, status: data.cdn_enabled ? "pending" : "listed" };
    } else if (request.method() === "PATCH" && path === "/v1/plugins/cdn-demo/cdn") {
      const data = request.postDataJSON();
      writes.push({ path, body: data });
      plugin = { ...plugin, cdn_enabled: data.enabled };
      body = plugin;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  return writes;
}

for (const enabled of [false, true]) {
  test(`submission sends explicit CDN consent: ${enabled}`, async ({ page }, testInfo) => {
    const writes = await mockMarket(page);
    await page.goto("/submit");
    const checkbox = page.getByRole("checkbox", { name: "使用社区源 CDN（需审查）" });
    await expect(checkbox).toHaveAttribute("aria-checked", "false");
    await expect(page.getByText("每次版本更新都需要审查", { exact: false })).toBeVisible();
    await page.getByLabel("选择 GitHub 插件仓库").click();
    await page.getByText("alice/astrbot_plugin_demo", { exact: true }).click();
    await expect(page.locator('[name="plugin-name"]')).toHaveValue("astrbot_plugin_demo");
    if (enabled) await checkbox.click();
    await page.screenshot({ path: testInfo.outputPath(`submit-${enabled}.png`), fullPage: true });
    await page
      .getByRole("button", { name: enabled ? "提交 CDN 审查" : "提交并上架", exact: true })
      .click();
    await expect(page).toHaveURL(/\/$/);
    expect(writes).toHaveLength(1);
    expect(writes[0]!.body.cdn_enabled).toBe(enabled);
  });
}

test("personal CDN switch preserves listing and follows server state on mobile", async ({
  page,
}, testInfo) => {
  const writes = await mockMarket(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/settings/personal");
  await page.getByText("我的插件", { exact: true }).click();
  const control = page.getByRole("switch", { name: "社区源 CDN" });
  await expect(control).toHaveAttribute("aria-checked", "true");
  await control.click();
  await expect(control).toHaveAttribute("aria-checked", "false");
  await expect(page.getByText("已上架", { exact: true })).toBeVisible();
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute("content", "noindex,nofollow");
  expect(writes).toEqual([{ path: "/v1/plugins/cdn-demo/cdn", body: { enabled: false } }]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("personal-mobile.png"), fullPage: true });
});
