import type { SiteConfig, User } from "@/types";

export const SITE_CONFIG_CACHE_KEY = "astrhub_site_config_v1";
export const USER_PREVIEW_CACHE_KEY = "astrhub_user_preview_v1";
export const USER_PREVIEW_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

const CACHE_VERSION = 1;

interface CacheEnvelope<T> {
  version: number;
  savedAt: number;
  value: T;
}

export interface UserPreview {
  username?: string;
  internal_username?: string;
  github_login?: string;
  login?: string;
  avatar?: string;
  avatar_url?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readEnvelope<T>(key: string, maxAgeMs?: number): T | null {
  if (typeof localStorage === "undefined") return null;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const envelope = JSON.parse(raw) as CacheEnvelope<T>;
    if (
      !isRecord(envelope) ||
      envelope.version !== CACHE_VERSION ||
      !Number.isFinite(envelope.savedAt) ||
      !isRecord(envelope.value)
    ) {
      localStorage.removeItem(key);
      return null;
    }
    if (maxAgeMs && Date.now() - envelope.savedAt > maxAgeMs) {
      localStorage.removeItem(key);
      return null;
    }
    return envelope.value;
  } catch {
    return null;
  }
}

function writeEnvelope<T>(key: string, value: T): void {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(
      key,
      JSON.stringify({ version: CACHE_VERSION, savedAt: Date.now(), value }),
    );
  } catch {
    // Storage may be unavailable or full; network data remains the source of truth.
  }
}

function optionalText(value: unknown): string | undefined {
  const text = typeof value === "string" ? value.trim() : "";
  return text || undefined;
}

export function readCachedSiteConfig(): Partial<SiteConfig> | null {
  return readEnvelope<Partial<SiteConfig>>(SITE_CONFIG_CACHE_KEY);
}

export function writeCachedSiteConfig(config: SiteConfig): void {
  writeEnvelope(SITE_CONFIG_CACHE_KEY, config);
}

export function readCachedUserPreview(): UserPreview | null {
  return readEnvelope<UserPreview>(USER_PREVIEW_CACHE_KEY, USER_PREVIEW_MAX_AGE_MS);
}

export function writeCachedUserPreview(user: User): UserPreview | null {
  const preview: UserPreview = {
    username: optionalText(user.username),
    internal_username: optionalText(user.internal_username),
    github_login: optionalText(user.github_login),
    login: optionalText(user.login),
    avatar: optionalText(user.avatar),
    avatar_url: optionalText(user.avatar_url),
  };
  if (!Object.values(preview).some(Boolean)) {
    clearCachedUserPreview();
    return null;
  }
  writeEnvelope(USER_PREVIEW_CACHE_KEY, preview);
  return preview;
}

export function clearCachedUserPreview(): void {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.removeItem(USER_PREVIEW_CACHE_KEY);
  } catch {
    // The in-memory authentication state is still cleared by the caller.
  }
}
