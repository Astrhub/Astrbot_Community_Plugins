export const CHUNK_RELOAD_STORAGE_KEY = "astrhub_chunk_reload_at_v1";
export const CHUNK_RELOAD_COOLDOWN_MS = 10_000;

declare global {
  interface Window {
    __showBootError?: () => void;
  }
}

const CHUNK_ERROR_PATTERNS = [
  /failed to fetch dynamically imported module/i,
  /importing a module script failed/i,
  /error loading dynamically imported module/i,
  /unable to preload css/i,
  /loading chunk [\w-]+ failed/i,
];

export function isChunkLoadError(error: unknown): boolean {
  const rawMessage =
    typeof error === "object" && error !== null && "message" in error
      ? (error as { message?: unknown }).message
      : undefined;
  const message =
    typeof error === "string"
      ? error
      : error instanceof Error
        ? error.message
        : typeof rawMessage === "string"
          ? rawMessage
          : "";
  return CHUNK_ERROR_PATTERNS.some((pattern) => pattern.test(message));
}

export function handleChunkLoadFailure(
  error?: unknown,
  options: { now?: () => number; reload?: () => void } = {},
): "reloaded" | "error_screen" {
  console.error("Failed to load application module:", error);
  const now = options.now ? options.now() : Date.now();
  const reload = options.reload ?? (() => window.location.reload());

  let canReload = false;
  try {
    const raw = window.sessionStorage.getItem(CHUNK_RELOAD_STORAGE_KEY);
    const previous = raw ? Number(raw) : Number.NaN;
    if (!Number.isFinite(previous) || now - previous > CHUNK_RELOAD_COOLDOWN_MS) {
      window.sessionStorage.setItem(CHUNK_RELOAD_STORAGE_KEY, String(now));
      canReload = true;
    }
  } catch {
    canReload = false;
  }

  if (canReload) {
    reload();
    return "reloaded";
  }

  window.__showBootError?.();
  return "error_screen";
}
