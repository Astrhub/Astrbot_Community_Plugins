import { computed, toValue, type MaybeRefOrGetter } from "vue";
import { useHead, type ReactiveHead } from "@unhead/vue";
import { usePluginStore } from "../stores/plugins";

export const DEFAULT_BRAND_NAME = "Astrhub 插件市场";

interface SeoOptions {
  title: MaybeRefOrGetter<string>;
  description: MaybeRefOrGetter<string>;
  path: MaybeRefOrGetter<string>;
  image?: MaybeRefOrGetter<string>;
  type?: "website" | "article";
  robots?: MaybeRefOrGetter<string>;
  jsonLd?: MaybeRefOrGetter<Record<string, unknown> | null>;
  jsonLdId?: string;
}

interface ResolvedSeoOptions {
  title: string;
  description: string;
  path: string;
  image?: string;
  type?: "website" | "article";
  robots?: string;
  jsonLd?: Record<string, unknown> | null;
  jsonLdId?: string;
}

function normalizeOrigin(rawUrl?: string): string {
  const value = String(rawUrl || "").trim();
  if (value) {
    try {
      const parsed = new URL(value);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") {
        return parsed.origin;
      }
    } catch {
      // Fall back to the browser origin when siteConfig.web_url is malformed.
    }
  }
  if (typeof window !== "undefined" && window.location?.origin) {
    return window.location.origin;
  }
  return "http://127.0.0.1:8787";
}

function activeSiteConfig() {
  try {
    return usePluginStore().siteConfig;
  } catch {
    return null;
  }
}

export function resolveSiteOrigin(webUrl?: string): string {
  return normalizeOrigin(webUrl !== undefined ? webUrl : activeSiteConfig()?.web_url);
}

export function resolveBrandName(name?: string): string {
  const value = String(name !== undefined ? name : activeSiteConfig()?.name || "").trim();
  return value || DEFAULT_BRAND_NAME;
}

export function absoluteSiteUrl(path: string, webUrl?: string): string {
  return new URL(path || "/", `${resolveSiteOrigin(webUrl)}/`).toString();
}

export function defaultOgImageUrl(webUrl?: string): string {
  return `${resolveSiteOrigin(webUrl)}/og-cover.png`;
}

export function useSeoContext() {
  const store = usePluginStore();
  const siteOrigin = computed(() => normalizeOrigin(store.siteConfig.web_url));
  const brandName = computed(
    () => String(store.siteConfig.name || "").trim() || DEFAULT_BRAND_NAME,
  );
  const defaultOgImage = computed(() => `${siteOrigin.value}/og-cover.png`);
  const absoluteUrl = (path: string): string =>
    new URL(path || "/", `${siteOrigin.value}/`).toString();

  return {
    siteOrigin,
    brandName,
    defaultOgImage,
    absoluteSiteUrl: absoluteUrl,
  };
}

function seoHead(options: ResolvedSeoOptions): ReactiveHead {
  const origin = resolveSiteOrigin();
  const brand = resolveBrandName();
  const canonical = new URL(options.path || "/", `${origin}/`).toString();
  const title = `${options.title} - ${brand}`;
  const image = options.image || `${origin}/og-cover.png`;
  return {
    title,
    link: [{ rel: "canonical", href: canonical }],
    meta: [
      { name: "description", content: options.description },
      { name: "robots", content: options.robots || "index,follow" },
      { property: "og:title", content: title },
      { property: "og:description", content: options.description },
      { property: "og:type", content: options.type || "website" },
      { property: "og:url", content: canonical },
      { property: "og:site_name", content: brand },
      { property: "og:image", content: image },
      { name: "twitter:card", content: "summary_large_image" },
    ],
    script: options.jsonLd
      ? [
          {
            id: options.jsonLdId || "page-jsonld",
            type: "application/ld+json",
            innerHTML: JSON.stringify(options.jsonLd),
          },
        ]
      : [],
  };
}

export function useSeo(options: SeoOptions): void {
  useHead(() =>
    seoHead({
      title: toValue(options.title),
      description: toValue(options.description),
      path: toValue(options.path),
      image: options.image ? toValue(options.image) : undefined,
      type: options.type,
      robots: options.robots ? toValue(options.robots) : undefined,
      jsonLd: options.jsonLd ? toValue(options.jsonLd) : undefined,
      jsonLdId: options.jsonLdId,
    }),
  );
}
