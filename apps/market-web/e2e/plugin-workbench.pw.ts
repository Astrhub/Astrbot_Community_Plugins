/// <reference types="node" />

import { mkdir, stat } from "node:fs/promises";
import path from "node:path";
import { expect, test, type Browser, type Page, type Route, type TestInfo } from "@playwright/test";

type FixtureMode = "loaded" | "loading" | "empty" | "error";

const now = "2026-07-17T04:00:00Z";

function artifact(id: "artifact-alpha" | "artifact-beta") {
  const second = id === "artifact-beta";
  return {
    id,
    plugin_id: second ? "astrbot_plugin_beta" : "astrbot_plugin_audit_guard",
    plugin_name: second ? "Beta 工具集" : "审查护栏",
    plugin_repo: second ? "https://github.com/demo/beta" : "https://github.com/demo/audit-guard",
    version: second ? "2.1.0" : "1.4.0",
    normalized_version: second ? "2.1.0" : "1.4.0",
    repo_version: second ? "2.1.0" : "1.5.0",
    published_version: second ? "2.0.0" : "1.3.0",
    source_type: "github",
    source_ref: second ? "v2.1.0" : "refs/heads/main",
    source_commit_sha: second ? "b".repeat(40) : "a".repeat(40),
    archive_sha256: second ? "b".repeat(64) : "a".repeat(64),
    size_bytes: second ? 98_304 : 131_072,
    review_status: "pending_review",
    publication_status: "unpublished",
    risk_level: second ? "low" : "high",
    download_url: null,
    submitted_by: "review-author",
    owner_user_id: "user-author",
    suggested_category: second ? "utilities" : "productivity",
    category_confidence: second ? 0.82 : 0.91,
    category_reason: "基于 metadata、README 与入口能力的自动审查建议",
    policy_version_id: "policy-2026-07",
    review_coverage: {
      routing: {
        route: "manual_review",
        target_status: "pending_review",
        reason_codes: ["deterministic_finding_requires_review"],
      },
    },
    created_at: now,
    updated_at: now,
  };
}

function detail(id: "artifact-alpha" | "artifact-beta") {
  return {
    artifact: artifact(id),
    runs: [
      {
        id: `${id}-runtime`,
        artifact_id: id,
        type: "runtime",
        status: "succeeded",
        attempt: 1,
        advisory: false,
        label: "确定性检查",
        summary: "隔离容器完成安装、导入、启动与 handler 注册检查。",
        tool_name: "astrbot-runtime-probe",
        tool_version: "4.26.6",
        policy_version_id: "policy-2026-07",
        coverage: { target_status: "manual_review" },
        astrbot_version: "4.26.6",
        python_version: "3.12",
        created_at: now,
        completed_at: now,
      },
      {
        id: `${id}-llm`,
        artifact_id: id,
        type: "llm_summary",
        status: "succeeded",
        attempt: 1,
        advisory: true,
        label: "自动审查建议",
        summary: "建议人工核对网络访问范围；该建议不构成最终安全背书。",
        model: "review-adapter-contract",
        policy_version_id: "policy-2026-07",
        coverage: { outcome: "advisory" },
        created_at: now,
        completed_at: now,
      },
    ],
    findings:
      id === "artifact-beta"
        ? []
        : [
            {
              id: "finding-network",
              artifact_id: id,
              run_id: `${id}-static`,
              fingerprint: "network-fingerprint",
              rule_id: "STATIC.NETWORK_DYNAMIC_HOST",
              file_path: "main.py",
              line_start: 42,
              line_end: 42,
              severity: "high",
              category: "network",
              message:
                '<img src="x" onerror="window.__workbenchXss=1"> 入口读取动态主机配置，需要人工核对允许范围。',
              suggestion: "限制目标域名并在运行时策略中声明。",
              evidence_excerpt: "target = config.get('endpoint')",
              confidence: 1,
              status: "open",
              source: "static",
              deterministic: true,
              advisory: false,
              label: "确定性检查",
              affects_current_release: false,
              version: 1,
              created_at: now,
            },
          ],
    decisions: [],
  };
}

function filesPayload(id: string) {
  return {
    artifact_id: id,
    tree_sha256: "c".repeat(64),
    items: [
      {
        id: `${id}-main`,
        artifact_id: id,
        path: "main.py",
        language: "python",
        mime_type: "text/x-python",
        sha256: "d".repeat(64),
        size_bytes: 512,
        line_count: 3,
        is_text: true,
        is_entrypoint: true,
        is_reachable: true,
        graph_status: "complete",
        content_available: true,
      },
    ],
    total: 1,
    limit: 200,
    offset: 0,
  };
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

async function installApi(page: Page, mode: FixtureMode, unknown: string[]): Promise<void> {
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;

    if (pathname === "/v1/site") return json(route, {});
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
    if (pathname === "/v1/plugins") return json(route, { items: [] });
    if (pathname === "/v1/me/notifications/unread-count") return json(route, { count: 0 });
    if (pathname === "/v1/me") {
      return json(route, {
        id: "admin-1",
        role: "core_admin",
        github_login: "reviewer",
        username: "reviewer",
        internal_username: "reviewer",
      });
    }
    if (pathname === "/v1/admin/artifacts") {
      if (mode === "loading") await new Promise((resolve) => setTimeout(resolve, 2_200));
      return json(route, {
        items: mode === "empty" ? [] : [artifact("artifact-alpha"), artifact("artifact-beta")],
      });
    }

    const detailMatch = pathname.match(/^\/v1\/artifacts\/(artifact-(?:alpha|beta))$/);
    if (detailMatch) {
      return json(route, detail(detailMatch[1] as "artifact-alpha" | "artifact-beta"));
    }
    const filesMatch = pathname.match(/^\/v1\/artifacts\/(artifact-(?:alpha|beta))\/files$/);
    if (filesMatch) {
      if (mode === "error") {
        return json(
          route,
          { detail: { code: "tool_unavailable", message: "文件索引暂时不可用" } },
          503,
        );
      }
      return json(route, filesPayload(filesMatch[1]));
    }
    const contentMatch = pathname.match(
      /^\/v1\/artifacts\/(artifact-(?:alpha|beta))\/files\/(.+)\/content$/,
    );
    if (contentMatch) {
      const payload = filesPayload(contentMatch[1]);
      return json(route, {
        artifact_id: contentMatch[1],
        tree_sha256: payload.tree_sha256,
        file: payload.items[0],
        encoding: "utf-8",
        start_line: 1,
        end_line: 3,
        total_lines: 3,
        truncated: false,
        lines: [
          { number: 1, text: "from astrbot.api.event import filter" },
          { number: 2, text: "class Main:" },
          { number: 3, text: "    pass" },
        ],
      });
    }
    const diffMatch = pathname.match(/^\/v1\/artifacts\/(artifact-(?:alpha|beta))\/diff$/);
    if (diffMatch) {
      return json(route, {
        artifact_id: diffMatch[1],
        tree_sha256: "c".repeat(64),
        items: [],
        total: 0,
        limit: 100,
        offset: 0,
      });
    }
    const commentsMatch = pathname.match(/^\/v1\/artifacts\/(artifact-(?:alpha|beta))\/comments$/);
    if (commentsMatch) {
      return json(route, {
        artifact_id: commentsMatch[1],
        items: [],
        total: 0,
        limit: 100,
        offset: 0,
      });
    }
    const historyMatch = pathname.match(/^\/v1\/artifacts\/(artifact-(?:alpha|beta))\/history$/);
    if (historyMatch) {
      return json(route, {
        artifact_id: historyMatch[1],
        items: [],
        has_more: false,
        next_cursor: null,
      });
    }

    unknown.push(`${request.method()} ${pathname}`);
    return json(route, { detail: `Unhandled visual fixture route: ${pathname}` }, 404);
  });
}

async function createPage(
  browser: Browser,
  viewport: { width: number; height: number },
  mode: FixtureMode,
  unknown: string[],
) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, locale: "zh-CN" });
  const page = await context.newPage();
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    const expected503 = mode === "error" && message.text().includes("503 (Service Unavailable)");
    if (message.type() === "error" && !expected503) {
      problems.push(`console: ${message.text()}`);
    }
  });
  await installApi(page, mode, unknown);
  return { context, page, problems };
}

async function auditLayout(page: Page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    const overlaps: string[] = [];
    const selectors = [
      ".workbench-header",
      ".workbench-header__actions",
      ".review-sidebar__filters",
      ".summary-tags",
      ".run-item__title",
      ".finding-item__title",
    ].join(",");
    for (const container of document.querySelectorAll(selectors)) {
      const children = [...container.children].filter((node) => {
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return (
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          rect.width > 1 &&
          rect.height > 1
        );
      });
      for (let leftIndex = 0; leftIndex < children.length; leftIndex += 1) {
        for (let rightIndex = leftIndex + 1; rightIndex < children.length; rightIndex += 1) {
          const left = children[leftIndex].getBoundingClientRect();
          const right = children[rightIndex].getBoundingClientRect();
          const width = Math.min(left.right, right.right) - Math.max(left.left, right.left);
          const height = Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top);
          if (width > 2 && height > 2) {
            overlaps.push(
              `${container.className}: ${children[leftIndex].className} <> ${children[rightIndex].className}`,
            );
          }
        }
      }
    }
    return {
      viewportWidth: root.clientWidth,
      scrollWidth: root.scrollWidth,
      horizontalOverflow: Math.max(0, root.scrollWidth - root.clientWidth),
      overlaps,
    };
  });
}

async function expectLayout(page: Page, label: string): Promise<void> {
  const result = await auditLayout(page);
  expect(result.horizontalOverflow, `${label}: global horizontal overflow`).toBeLessThanOrEqual(1);
  expect(result.overlaps, `${label}: key sibling overlap`).toEqual([]);
}

async function capture(
  page: Page,
  testInfo: TestInfo,
  name: string,
  fullPage = true,
): Promise<void> {
  const evidenceDirectory = process.env.WORKBENCH_EVIDENCE_DIR;
  const output = evidenceDirectory
    ? path.resolve(evidenceDirectory, `${name}.png`)
    : testInfo.outputPath(`${name}.png`);
  await mkdir(path.dirname(output), { recursive: true });
  await page.screenshot({ path: output, fullPage, animations: "disabled" });
  expect((await stat(output)).size, `${name}: screenshot must not be blank`).toBeGreaterThan(
    10_000,
  );
}

test("workbench renders loaded, loading, empty, error, and mobile queue states", async ({
  browser,
}, testInfo) => {
  const unknown: string[] = [];

  {
    const { context, page, problems } = await createPage(
      browser,
      { width: 1_440, height: 1_000 },
      "loading",
      unknown,
    );
    await page.goto("/plugin-workbench", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "版本审查队列" })).toBeVisible();
    await expect(page.locator(".review-workspace__sidebar .n-spin-body")).toBeVisible();
    await expect(page.locator(".review-workspace__sidebar .artifact-row")).toHaveCount(0);
    await expect(page.getByText("暂无版本记录", { exact: true })).toBeVisible();
    await capture(page, testInfo, "workbench-loading-desktop", false);
    await expect(page.locator(".artifact-row").first()).toBeVisible();
    await expect(page.locator(".review-workspace__sidebar .n-spin-body")).toBeHidden();
    expect(problems).toEqual([]);
    await context.close();
  }

  {
    const { context, page, problems } = await createPage(
      browser,
      { width: 1_440, height: 1_000 },
      "loaded",
      unknown,
    );
    await page.goto("/plugin-workbench", { waitUntil: "domcontentloaded" });
    const rows = page.locator(".review-workspace__sidebar .artifact-row");
    await expect(rows.first()).toBeVisible();
    await expect(page.getByText("入口读取动态主机配置", { exact: false })).toBeVisible();
    await expect(page.getByText("<img", { exact: false })).toBeVisible();
    await expect(page.locator('img[src="x"]')).toHaveCount(0);
    expect(await page.evaluate(() => Reflect.get(window, "__workbenchXss"))).toBeUndefined();
    await expectLayout(page, "desktop loaded");
    await capture(page, testInfo, "workbench-loaded-desktop");

    await page.locator('.review-tabs [data-name="files"]').click();
    await expect(page).toHaveURL(/view=files/);
    await expect(
      page.getByText("from astrbot.api.event import filter", { exact: true }),
    ).toBeVisible();
    await page.locator('.review-tabs [data-name="diff"]').click();
    await expect(page).toHaveURL(/view=diff/);
    await expect(page.getByText("当前版本没有可用 diff", { exact: true })).toBeVisible();
    await page.locator('.review-tabs [data-name="comments"]').click();
    await expect(page).toHaveURL(/view=comments/);
    await expect(page.getByLabel("行级审查评论")).toBeVisible();
    await page.locator('.review-tabs [data-name="history"]').click();
    await expect(page).toHaveURL(/view=history/);
    await expect(page.getByText("暂无审查历史", { exact: true })).toBeVisible();

    await rows.nth(1).click();
    await expect(page).toHaveURL(/artifact=artifact-beta/);
    await expect(page.getByText("2.1.0", { exact: true }).first()).toBeVisible();
    await expectLayout(page, "desktop selection");
    expect(problems).toEqual([]);
    await context.close();
  }

  {
    const { context, page, problems } = await createPage(
      browser,
      { width: 1_280, height: 900 },
      "empty",
      unknown,
    );
    await page.goto("/plugin-workbench", { waitUntil: "domcontentloaded" });
    await expect(page.getByText("暂无版本记录", { exact: true })).toBeVisible();
    await expectLayout(page, "desktop empty");
    await capture(page, testInfo, "workbench-empty-desktop");
    expect(problems).toEqual([]);
    await context.close();
  }

  {
    const { context, page, problems } = await createPage(
      browser,
      { width: 1_280, height: 900 },
      "error",
      unknown,
    );
    await page.goto("/plugin-workbench?artifact=artifact-alpha&view=files", {
      waitUntil: "domcontentloaded",
    });
    await expect(page.getByText("文件索引暂时不可用", { exact: true })).toBeVisible();
    await expectLayout(page, "desktop error");
    await capture(page, testInfo, "workbench-error-desktop");
    expect(problems).toEqual([]);
    await context.close();
  }

  {
    const { context, page, problems } = await createPage(
      browser,
      { width: 390, height: 844 },
      "loaded",
      unknown,
    );
    await page.goto("/plugin-workbench", { waitUntil: "domcontentloaded" });
    await expect(page.getByText("入口读取动态主机配置", { exact: false })).toBeVisible();
    await expectLayout(page, "mobile loaded");
    await capture(page, testInfo, "workbench-loaded-mobile");

    await page.getByRole("button", { name: "打开版本队列" }).click();
    const drawer = page.locator(".n-drawer");
    await expect(drawer).toBeVisible();
    await capture(page, testInfo, "workbench-drawer-mobile", false);
    await drawer.locator(".artifact-row").nth(1).click();
    await expect(page).toHaveURL(/artifact=artifact-beta/);
    await expect(page.getByText("2.1.0", { exact: true }).first()).toBeVisible();
    await expectLayout(page, "mobile selection");
    expect(problems).toEqual([]);
    await context.close();
  }

  expect([...new Set(unknown)]).toEqual([]);
});
