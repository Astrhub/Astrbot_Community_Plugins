import { onUnmounted, watch, type ComputedRef } from "vue";
import type { Plugin } from "../types";
import { resolvePluginLogoUrl } from "../utils/github";

export function usePluginLogoPreload(plugins: ComputedRef<Plugin[]>): void {
  const seen = new Set<string>();
  const pending = new Set<HTMLImageElement>();
  let queue: string[] = [];
  let active = true;

  function pump(): void {
    while (active && pending.size < 2 && queue.length) {
      const url = queue.shift()!;
      if (seen.has(url)) continue;
      seen.add(url);
      if (seen.size > 256) seen.delete(seen.values().next().value!);
      const img = new Image();
      img.fetchPriority = "low";
      pending.add(img);
      img.onload = img.onerror = () => {
        pending.delete(img);
        img.onload = img.onerror = null;
        pump();
      };
      img.src = url;
    }
  }

  watch(
    plugins,
    (items) => {
      const connection = (
        navigator as Navigator & { connection?: { saveData?: boolean; effectiveType?: string } }
      ).connection;
      if (connection?.saveData || ["slow-2g", "2g"].includes(connection?.effectiveType || ""))
        return;
      // Only warm a small upcoming batch. Default local images are shared and need no prefetch.
      queue = [...new Set(items.slice(0, 6).map(resolvePluginLogoUrl))].filter(
        (url) => /^https?:\/\//.test(url) && !seen.has(url),
      );
      pump();
    },
    { immediate: true },
  );

  onUnmounted(() => {
    active = false;
    queue = [];
    for (const img of pending) {
      img.onload = img.onerror = null;
      img.removeAttribute("src");
    }
    pending.clear();
  });
}
