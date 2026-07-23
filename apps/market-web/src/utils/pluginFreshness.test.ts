import { describe, expect, it } from "vite-plus/test";
import { isNewPlugin, NEW_PLUGIN_DAYS } from "./pluginFreshness";

describe("isNewPlugin", () => {
  const now = Date.parse("2026-07-20T00:00:00Z");

  it("uses an inclusive 14-day freshness window", () => {
    expect(isNewPlugin("2026-07-19T00:00:00Z", now)).toBe(true);
    expect(isNewPlugin("2026-07-06T00:00:00Z", now)).toBe(true);
    expect(isNewPlugin("2026-07-05T23:59:59Z", now)).toBe(false);
    expect(NEW_PLUGIN_DAYS).toBe(14);
  });

  it("rejects missing, invalid, and future dates", () => {
    expect(isNewPlugin("", now)).toBe(false);
    expect(isNewPlugin("invalid", now)).toBe(false);
    expect(isNewPlugin("2026-07-21T00:00:00Z", now)).toBe(false);
  });
});
