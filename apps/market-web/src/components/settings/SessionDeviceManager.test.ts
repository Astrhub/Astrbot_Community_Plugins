// @vitest-environment jsdom
import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vite-plus/test";
import SessionDeviceManager from "./SessionDeviceManager.vue";
import type { UserSession } from "@/types";

const SAMPLE_SESSIONS: UserSession[] = [
  {
    id: "sess_current",
    trusted: true,
    device_label: "Chrome · Windows",
    ip_address: "198.51.100.12",
    created_at: "2026-11-27T00:00:00Z",
    last_seen_at: "2026-11-27T01:00:00Z",
    expires_at: "2027-11-27T00:00:00Z",
    is_current: true,
  },
  {
    id: "sess_phone",
    trusted: false,
    device_label: "Safari · iPhone",
    ip_address: "203.0.113.9",
    created_at: "2026-11-26T00:00:00Z",
    last_seen_at: "2026-11-26T12:00:00Z",
    expires_at: "2026-12-03T00:00:00Z",
    is_current: false,
  },
];

describe("SessionDeviceManager", () => {
  it("renders trusted and regular device badges and emits revoke events", async () => {
    const wrapper = mount(SessionDeviceManager, {
      props: {
        sessions: SAMPLE_SESSIONS,
      },
    });

    expect(wrapper.text()).toContain("Chrome · Windows");
    expect(wrapper.text()).toContain("当前设备");
    expect(wrapper.text()).toContain("信任设备 · 1 年");
    expect(wrapper.text()).toContain("Safari · iPhone");
    expect(wrapper.text()).toContain("常规会话 · 7 天");

    const revokeOtherBtn = wrapper.findAll("button").find((btn) => btn.text().includes("下线设备"));
    expect(revokeOtherBtn).toBeDefined();
    await revokeOtherBtn!.trigger("click");
    expect(wrapper.emitted("revoke")?.[0]?.[0]).toEqual(SAMPLE_SESSIONS[1]);

    const revokeOthersBtn = wrapper
      .findAll("button")
      .find((btn) => btn.text().includes("注销其他设备"));
    expect(revokeOthersBtn).toBeDefined();
    expect(revokeOthersBtn!.attributes("disabled")).toBeUndefined();
    await revokeOthersBtn!.trigger("click");
    expect(wrapper.emitted("revoke-others")).toHaveLength(1);
    wrapper.unmount();
  });

  it("disables revoking other devices when only the current session is active", () => {
    const wrapper = mount(SessionDeviceManager, {
      props: {
        sessions: [SAMPLE_SESSIONS[0]],
      },
    });

    const revokeOthersBtn = wrapper
      .findAll("button")
      .find((btn) => btn.text().includes("注销其他设备"));
    expect(revokeOthersBtn).toBeDefined();
    expect(revokeOthersBtn!.attributes("disabled")).toBeDefined();
    wrapper.unmount();
  });
});
