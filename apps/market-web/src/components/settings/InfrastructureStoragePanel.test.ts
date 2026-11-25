// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { createTestingPinia } from "@pinia/testing";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import InfrastructureStoragePanel from "./InfrastructureStoragePanel.vue";
import type { StorageBindingItem, StorageMigrationItem, StorageProfileItem } from "@/types/storage";

vi.mock("naive-ui", async (importOriginal) => {
  const original = await importOriginal<typeof import("naive-ui")>();
  const { defineComponent, h } = await import("vue");
  return {
    ...original,
    NModal: defineComponent({
      props: { show: Boolean, title: String },
      setup:
        (props, { slots }) =>
        () =>
          props.show
            ? h("section", { role: "dialog", "data-title": props.title }, [
                h("h3", props.title),
                slots.default?.(),
                slots.footer?.(),
              ])
            : null,
    }),
  };
});

function sampleProfiles(): StorageProfileItem[] {
  return [
    {
      id: "local-fs-a",
      name: "本地目录 A",
      backend: "local",
      status: "active",
      config: { mode: "filesystem", root: "/tmp/a", public_base_url: "" },
      has_secret: false,
      legacy: true,
      health_status: "healthy",
      health_message: "连接与读写校验通过",
      last_checked_at: "2026-11-25T00:00:00Z",
      created_by: "system",
      updated_by: "system",
      created_at: "2026-11-25T00:00:00Z",
      updated_at: "2026-11-25T00:00:00Z",
      bound_purposes: ["artifact-published"],
      active_object_count: 2,
      pending_object_count: 0,
      has_active_migrations: false,
      capabilities: { conditional_create: true, range_read: true },
    },
    {
      id: "r2-main",
      name: "Cloudflare R2 主桶",
      backend: "s3",
      status: "active",
      config: {
        endpoint_url: "https://example.r2.cloudflarestorage.com",
        region: "auto",
        bucket: "astrhub-pkg",
        public_base_url: "https://cdn.example.com",
      },
      has_secret: true,
      legacy: false,
      health_status: "unknown",
      health_message: "",
      last_checked_at: "",
      created_by: "admin-1",
      updated_by: "admin-1",
      created_at: "2026-11-25T00:00:00Z",
      updated_at: "2026-11-25T00:00:00Z",
      bound_purposes: [],
      active_object_count: 0,
      pending_object_count: 0,
      has_active_migrations: false,
      capabilities: { conditional_create: true, range_read: true, public_read: true },
    },
  ];
}

function sampleBindings(): StorageBindingItem[] {
  return [
    {
      purpose: "artifact-quarantine",
      profile_id: "local-fs-a",
      generation: 1,
      updated_by: "system",
      updated_at: "2026-11-25T00:00:00Z",
    },
    {
      purpose: "artifact-published",
      profile_id: "local-fs-a",
      generation: 1,
      updated_by: "system",
      updated_at: "2026-11-25T00:00:00Z",
    },
    {
      purpose: "avatar",
      profile_id: "",
      generation: 0,
      updated_by: "",
      updated_at: "",
    },
  ];
}

function sampleMigrations(): StorageMigrationItem[] {
  return [
    {
      id: "stmig_demo_1",
      purpose: "artifact-published",
      source_profile_id: "local-fs-a",
      target_profile_id: "r2-main",
      binding_generation: 2,
      status: "completed_with_errors",
      total_count: 3,
      completed_count: 2,
      failed_count: 1,
      skipped_count: 0,
      unknown_objects: [{ namespace: "", key: "orphan.zip", size_bytes: 12 }],
      last_error: "temporary timeout",
      created_by: "admin-1",
      created_at: "2026-11-25T00:00:00Z",
      updated_at: "2026-11-25T00:00:00Z",
      completed_at: "2026-11-25T00:01:00Z",
    },
  ];
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("InfrastructureStoragePanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function installDefaultFetch() {
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const method = (init?.method || "GET").toUpperCase();
      if (url.endsWith("/v1/admin/storage/profiles") && method === "GET") {
        return jsonResponse({ items: sampleProfiles() });
      }
      if (url.endsWith("/v1/admin/storage/bindings") && method === "GET") {
        return jsonResponse({ items: sampleBindings() });
      }
      if (url.endsWith("/v1/admin/storage/migrations") && method === "GET") {
        return jsonResponse({ items: sampleMigrations() });
      }
      if (url.endsWith("/v1/admin/storage/profiles/r2-main/test") && method === "POST") {
        return jsonResponse({
          ok: true,
          profile_id: "r2-main",
          health_status: "healthy",
          health_message: "连接与读写校验通过",
          checked_at: "2026-11-25T01:00:00Z",
          checks: { write: true, read: true, range: true, delete: true, public_url: true },
        });
      }
      if (url.endsWith("/v1/admin/storage/profiles/r2-main") && method === "PATCH") {
        return jsonResponse({ ...sampleProfiles()[1], name: "R2 更新名称" });
      }
      if (url.endsWith("/v1/admin/storage/bindings/artifact-published") && method === "PUT") {
        return jsonResponse({
          binding: { ...sampleBindings()[1], profile_id: "r2-main", generation: 2 },
          migration: sampleMigrations()[0],
        });
      }
      if (
        url.endsWith("/v1/admin/storage/migrations/stmig_demo_1/retry-failed") &&
        method === "POST"
      ) {
        return jsonResponse({ ...sampleMigrations()[0], status: "queued", failed_count: 0 });
      }
      if (
        url.endsWith("/v1/admin/storage/migrations/stmig_demo_1/import-unknown") &&
        method === "POST"
      ) {
        return jsonResponse({ ...sampleMigrations()[0], status: "queued", unknown_objects: [] });
      }
      return jsonResponse({ error: `Unhandled ${method} ${url}` }, 404);
    });
  }

  function mountPanel() {
    const pinia = createTestingPinia({ createSpy: vi.fn });
    return mount(InfrastructureStoragePanel, {
      props: { apiBaseUrl: "http://api.test" },
      global: { plugins: [pinia] },
    });
  }

  it("renders grouped profiles, triggers connection test, and retries failed migration items", async () => {
    installDefaultFetch();
    const wrapper = mountPanel();
    await flushPromises();

    expect(wrapper.text()).toContain("本地与数据库（Local）");
    expect(wrapper.text()).toContain("S3 / Cloudflare R2");
    expect(wrapper.text()).toContain("未登记对象 1");

    const r2Card = wrapper.findAll(".profile-card").find((card) => card.text().includes("r2-main"));
    expect(r2Card).toBeDefined();
    const testBtn = r2Card!.findAll("button").find((btn) => btn.text().includes("测试连接"));
    await testBtn!.trigger("click");
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/v1/admin/storage/profiles/r2-main/test",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
    expect(wrapper.text()).toContain("r2-main 测试通过");

    const retryBtn = wrapper.findAll("button").find((btn) => btn.text().includes("重试失败项"));
    expect(retryBtn).toBeDefined();
    await retryBtn!.trigger("click");
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/v1/admin/storage/migrations/stmig_demo_1/retry-failed",
      expect.objectContaining({ method: "POST" }),
    );

    const importBtn = wrapper
      .findAll("button")
      .find((btn) => btn.text().includes("导入未登记对象"));
    expect(importBtn).toBeDefined();
    await importBtn!.trigger("click");
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/v1/admin/storage/migrations/stmig_demo_1/import-unknown",
      expect.objectContaining({ method: "POST" }),
    );
    wrapper.unmount();
  });

  it("omits secret field when editing an existing profile with blank credential inputs", async () => {
    installDefaultFetch();
    const wrapper = mountPanel();
    await flushPromises();

    const r2Card = wrapper.findAll(".profile-card").find((card) => card.text().includes("r2-main"));
    const editBtn = r2Card!.findAll("button").find((btn) => btn.text().includes("编辑"));
    await editBtn!.trigger("click");
    await flushPromises();

    const dialog = wrapper.get('[role="dialog"]');
    expect(dialog.text()).toContain("编辑存储配置：r2-main");
    const saveBtn = dialog.findAll("button").find((btn) => btn.text().includes("保存修改"));
    expect(saveBtn).toBeDefined();
    await saveBtn!.trigger("click");
    await flushPromises();

    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/v1/admin/storage/profiles/r2-main") &&
        (init as RequestInit)?.method === "PATCH",
    );
    expect(patchCall).toBeDefined();
    const patchBody = (patchCall![1] as RequestInit).body;
    expect(typeof patchBody).toBe("string");
    const sentBody = JSON.parse(patchBody as string);
    expect(sentBody).not.toHaveProperty("secret");
    expect(sentBody.config.bucket).toBe("astrhub-pkg");
    wrapper.unmount();
  });

  it("shows confirmation modal before rebinding a storage purpose", async () => {
    installDefaultFetch();
    const wrapper = mountPanel();
    await flushPromises();

    const vm = wrapper.vm as unknown as {
      bindingDrafts: Record<string, string>;
      openBindConfirm: (purpose: "artifact-published") => void;
    };
    vm.bindingDrafts["artifact-published"] = "r2-main";
    vm.openBindConfirm("artifact-published");
    await flushPromises();

    const dialog = wrapper.get('[role="dialog"]');
    expect(dialog.text()).toContain("确认切换存储用途绑定");
    expect(dialog.text()).toContain("r2-main");

    const confirmBtn = dialog.findAll("button").find((btn) => btn.text().includes("确认切换"));
    await confirmBtn!.trigger("click");
    await flushPromises();

    const putCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/v1/admin/storage/bindings/artifact-published") &&
        (init as RequestInit)?.method === "PUT",
    );
    expect(putCall).toBeDefined();
    const putBody = (putCall![1] as RequestInit).body;
    expect(typeof putBody).toBe("string");
    expect(JSON.parse(putBody as string)).toEqual({
      profile_id: "r2-main",
    });
    wrapper.unmount();
  });

  it("supports creating and editing WebDAV profiles while preserving blank credentials", async () => {
    const webdavProfile: StorageProfileItem = {
      id: "dav-nas",
      name: "NAS WebDAV",
      backend: "webdav",
      status: "active",
      config: {
        base_url: "https://dav.example.com/dav",
        root_path: "astrhub/pkg",
        auth_type: "basic",
        allow_private_network: false,
        verify_tls: true,
        public_base_url: "",
      },
      has_secret: true,
      legacy: false,
      health_status: "healthy",
      health_message: "WebDAV 能力探测与读写校验通过",
      last_checked_at: "2026-11-25T00:00:00Z",
      created_by: "admin-1",
      updated_by: "admin-1",
      created_at: "2026-11-25T00:00:00Z",
      updated_at: "2026-11-25T00:00:00Z",
      bound_purposes: [],
      active_object_count: 0,
      pending_object_count: 0,
      has_active_migrations: false,
      capabilities: { conditional_create: true, range_read: true, public_read: false },
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const method = (init?.method || "GET").toUpperCase();
      if (url.endsWith("/v1/admin/storage/profiles") && method === "GET") {
        return jsonResponse({ items: [webdavProfile] });
      }
      if (url.endsWith("/v1/admin/storage/bindings") && method === "GET") {
        return jsonResponse({ items: sampleBindings() });
      }
      if (url.endsWith("/v1/admin/storage/migrations") && method === "GET") {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/v1/admin/storage/profiles/dav-nas") && method === "PATCH") {
        return jsonResponse(webdavProfile);
      }
      return jsonResponse({ error: `Unhandled ${method} ${url}` }, 404);
    });

    const wrapper = mountPanel();
    await flushPromises();
    expect(wrapper.text()).toContain("WebDAV · https://dav.example.com/dav/astrhub/pkg");

    const davCard = wrapper
      .findAll(".profile-card")
      .find((card) => card.text().includes("dav-nas"));
    const editBtn = davCard!.findAll("button").find((btn) => btn.text().includes("编辑"));
    await editBtn!.trigger("click");
    await flushPromises();

    const dialog = wrapper.get('[role="dialog"]');
    expect(dialog.text()).toContain("编辑存储配置：dav-nas");
    const saveBtn = dialog.findAll("button").find((btn) => btn.text().includes("保存修改"));
    await saveBtn!.trigger("click");
    await flushPromises();

    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/v1/admin/storage/profiles/dav-nas") &&
        (init as RequestInit)?.method === "PATCH",
    );
    expect(patchCall).toBeDefined();
    const patchBody = (patchCall![1] as RequestInit).body;
    expect(typeof patchBody).toBe("string");
    const sent = JSON.parse(patchBody as string);
    expect(sent).not.toHaveProperty("secret");
    expect(sent.config).toEqual(
      expect.objectContaining({
        base_url: "https://dav.example.com/dav",
        root_path: "astrhub/pkg",
        auth_type: "basic",
      }),
    );
    wrapper.unmount();
  });

  it("renders and edits ImgBed profiles with scopes while preserving blank token", async () => {
    const imgbedProfile: StorageProfileItem = {
      id: "imgbed-avatars",
      name: "CloudFlare ImgBed 头像",
      backend: "imgbed",
      status: "active",
      config: {
        base_url: "https://imgbed.example.com",
        upload_folder: "astrhub/avatars",
        scopes: ["upload"],
        image_variants_enabled: true,
        allow_private_network: false,
        verify_tls: true,
      },
      has_secret: true,
      legacy: false,
      health_status: "healthy",
      health_message: "ImgBed 头像上传与公开读取校验通过",
      last_checked_at: "2026-11-25T00:00:00Z",
      created_by: "admin-1",
      updated_by: "admin-1",
      created_at: "2026-11-25T00:00:00Z",
      updated_at: "2026-11-25T00:00:00Z",
      bound_purposes: ["avatar"],
      active_object_count: 1,
      pending_object_count: 0,
      has_active_migrations: false,
      capabilities: {
        conditional_create: false,
        range_read: false,
        public_read: true,
        image_variants: true,
        list_objects: false,
      },
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const method = (init?.method || "GET").toUpperCase();
      if (url.endsWith("/v1/admin/storage/profiles") && method === "GET") {
        return jsonResponse({ items: [imgbedProfile] });
      }
      if (url.endsWith("/v1/admin/storage/bindings") && method === "GET") {
        return jsonResponse({ items: sampleBindings() });
      }
      if (url.endsWith("/v1/admin/storage/migrations") && method === "GET") {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/v1/admin/storage/profiles/imgbed-avatars") && method === "PATCH") {
        return jsonResponse(imgbedProfile);
      }
      return jsonResponse({ error: `Unhandled ${method} ${url}` }, 404);
    });

    const wrapper = mountPanel();
    await flushPromises();
    expect(wrapper.text()).toContain(
      "ImgBed · https://imgbed.example.com/file/astrhub/avatars · scopes=upload",
    );

    const card = wrapper
      .findAll(".profile-card")
      .find((item) => item.text().includes("imgbed-avatars"));
    const editBtn = card!.findAll("button").find((btn) => btn.text().includes("编辑"));
    await editBtn!.trigger("click");
    await flushPromises();

    const dialog = wrapper.get('[role="dialog"]');
    const saveBtn = dialog.findAll("button").find((btn) => btn.text().includes("保存修改"));
    await saveBtn!.trigger("click");
    await flushPromises();

    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/v1/admin/storage/profiles/imgbed-avatars") &&
        (init as RequestInit)?.method === "PATCH",
    );
    expect(patchCall).toBeDefined();
    const patchBody = (patchCall![1] as RequestInit).body;
    expect(typeof patchBody).toBe("string");
    const sent = JSON.parse(patchBody as string);
    expect(sent).not.toHaveProperty("secret");
    expect(sent.config).toEqual(
      expect.objectContaining({
        base_url: "https://imgbed.example.com",
        upload_folder: "astrhub/avatars",
        scopes: ["upload"],
        image_variants_enabled: true,
      }),
    );
    wrapper.unmount();
  });
});
