import { onBeforeUnmount, onMounted, readonly, ref } from "vue";

export const PAGE_BASE = 1400;
export const PAGE_ZOOM_MAX = 1.5;
export const PAGE_EDGE = 48;

export function calculatePageZoom(
  viewportWidth: number,
  baseWidth = PAGE_BASE,
  maxZoom = PAGE_ZOOM_MAX,
  edge = PAGE_EDGE,
): number {
  if (viewportWidth <= baseWidth + edge * 2) return 1;
  return Math.min((viewportWidth - edge * 2) / baseWidth, maxZoom);
}

export function usePageZoom() {
  const pageZoom = ref(1);
  let previousZoom = "";
  let previousPageZoom: string | undefined;

  const refreshPageZoom = () => {
    if (typeof window === "undefined" || typeof document === "undefined") return;
    const nextZoom = calculatePageZoom(window.innerWidth);
    pageZoom.value = nextZoom;
    document.body.style.setProperty("zoom", String(nextZoom));
    document.body.dataset.pageZoom = String(nextZoom);
  };

  onMounted(() => {
    previousZoom = document.body.style.getPropertyValue("zoom");
    previousPageZoom = document.body.dataset.pageZoom;
    refreshPageZoom();
    window.addEventListener("resize", refreshPageZoom, { passive: true });
  });

  onBeforeUnmount(() => {
    window.removeEventListener("resize", refreshPageZoom);
    if (previousZoom) document.body.style.setProperty("zoom", previousZoom);
    else document.body.style.removeProperty("zoom");

    if (previousPageZoom !== undefined) document.body.dataset.pageZoom = previousPageZoom;
    else delete document.body.dataset.pageZoom;
  });

  return {
    pageZoom: readonly(pageZoom),
    refreshPageZoom,
  };
}
