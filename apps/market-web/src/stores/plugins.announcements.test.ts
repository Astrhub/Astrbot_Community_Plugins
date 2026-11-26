// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { usePluginStore } from "./plugins";
import type { Announcement } from "@/types";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("plugin store announcement actions", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("synchronizes publish, edit, delete and image upload requests", async () => {
    const created: Announcement = {
      id: "announcement-1",
      title: "标题",
      body: "正文",
      created_at: "2026-11-26T10:00:00Z",
      updated_at: "2026-11-26T10:00:00Z",
    };
    const updated = { ...created, title: "新标题", updated_at: "2026-11-26T11:00:00Z" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(created, 201))
      .mockResolvedValueOnce(jsonResponse(updated))
      .mockResolvedValueOnce(jsonResponse({ deleted: true }))
      .mockResolvedValueOnce(
        jsonResponse(
          {
            url: "/v1/storage/public/image-1",
            content_type: "image/png",
            size_bytes: 3,
            sha256: "abc",
          },
          201,
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const store = usePluginStore();

    await store.publishAnnouncement({ title: "标题", body: "正文" });
    expect(store.announcements).toEqual([created]);
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: "POST",
      credentials: "include",
      body: JSON.stringify({ title: "标题", body: "正文" }),
    });

    await store.updateAnnouncement(created.id, { title: "新标题" });
    expect(store.announcements[0]?.title).toBe("新标题");
    expect(fetchMock.mock.calls[1]?.[0]).toContain("/v1/core/announcements/announcement-1");
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({ title: "新标题" }),
    });

    await store.deleteAnnouncement(created.id);
    expect(store.announcements).toEqual([]);
    expect(fetchMock.mock.calls[2]?.[0]).toContain("/v1/core/announcements/announcement-1");
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({ method: "DELETE" });

    const image = new File(["png"], "cover.png", { type: "image/png" });
    const uploaded = await store.uploadAnnouncementImage(image);
    expect(uploaded.url).toBe("/v1/storage/public/image-1");
    const uploadInit = fetchMock.mock.calls[3]?.[1] as RequestInit;
    expect(uploadInit.method).toBe("POST");
    expect(uploadInit.body).toBeInstanceOf(FormData);
    expect((uploadInit.body as FormData).get("image")).toBe(image);
  });

  it("does not let stale loads overwrite newer edits or deletes", async () => {
    const created: Announcement = {
      id: "announcement-1",
      title: "旧标题",
      body: "正文",
      created_at: "2026-11-26T10:00:00Z",
      updated_at: "2026-11-26T10:00:00Z",
    };
    const updated = { ...created, title: "新标题", updated_at: "2026-11-26T11:00:00Z" };
    let resolveFirstLoad: ((response: Response) => void) | undefined;
    let resolveSecondLoad: ((response: Response) => void) | undefined;
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveFirstLoad = resolve;
          }),
      )
      .mockResolvedValueOnce(jsonResponse(updated))
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveSecondLoad = resolve;
          }),
      )
      .mockResolvedValueOnce(jsonResponse({ deleted: true }));
    vi.stubGlobal("fetch", fetchMock);
    const store = usePluginStore();
    store.announcements = [created];

    const firstLoad = store.loadAnnouncements({ force: true });
    await store.updateAnnouncement(created.id, { title: "新标题" });
    resolveFirstLoad?.(jsonResponse({ items: [created] }));
    await firstLoad;
    expect(store.announcements[0]?.title).toBe("新标题");

    const secondLoad = store.loadAnnouncements({ force: true });
    await store.deleteAnnouncement(created.id);
    resolveSecondLoad?.(jsonResponse({ items: [updated] }));
    await secondLoad;
    expect(store.announcements).toEqual([]);
  });

  it("treats an already deleted announcement as a successful retry", async () => {
    const created: Announcement = {
      id: "announcement-1",
      title: "标题",
      body: "正文",
      created_at: "2026-11-26T10:00:00Z",
      updated_at: "2026-11-26T10:00:00Z",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ error: "not found" }, 404)));
    const store = usePluginStore();
    store.announcements = [created];

    await store.deleteAnnouncement(created.id);

    expect(store.announcements).toEqual([]);
  });
});
