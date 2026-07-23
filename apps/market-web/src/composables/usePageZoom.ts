import { onBeforeUnmount, onMounted, readonly, shallowRef } from "vue";

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
  const pageZoom = shallowRef(1);
  let zoomTarget: HTMLElement | null = null;
  let previousZoom = "";
  let previousPageZoom: string | undefined;

  const refreshPageZoom = () => {
    if (typeof window === "undefined" || typeof document === "undefined") return;
    if (!zoomTarget) zoomTarget = document.getElementById("app");
    if (!zoomTarget) return;

    const nextZoom = calculatePageZoom(window.innerWidth);
    pageZoom.value = nextZoom;
    zoomTarget.style.setProperty("zoom", String(nextZoom));
    zoomTarget.dataset.pageZoom = String(nextZoom);
  };

  onMounted(() => {
    zoomTarget = document.getElementById("app");
    if (zoomTarget) {
      previousZoom = zoomTarget.style.getPropertyValue("zoom");
      previousPageZoom = zoomTarget.dataset.pageZoom;
    }
    refreshPageZoom();
    window.addEventListener("resize", refreshPageZoom, { passive: true });
  });

  onBeforeUnmount(() => {
    window.removeEventListener("resize", refreshPageZoom);
    if (!zoomTarget) return;

    if (previousZoom) zoomTarget.style.setProperty("zoom", previousZoom);
    else zoomTarget.style.removeProperty("zoom");

    if (previousPageZoom !== undefined) zoomTarget.dataset.pageZoom = previousPageZoom;
    else delete zoomTarget.dataset.pageZoom;
  });

  return {
    pageZoom: readonly(pageZoom),
    refreshPageZoom,
  };
}
