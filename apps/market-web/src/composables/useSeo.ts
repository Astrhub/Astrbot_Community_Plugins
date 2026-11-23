import { toValue, type MaybeRefOrGetter } from "vue";
import { injectHead, useHead, type ReactiveHead } from "@unhead/vue";

export const SITE_ORIGIN = "https://plugins.eloina.cn";
export const BRAND_NAME = "Astrhub 插件市场";
export const DEFAULT_OG_IMAGE = `${SITE_ORIGIN}/og-cover.png`;

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

export function absoluteSiteUrl(path: string): string {
  return new URL(path || "/", `${SITE_ORIGIN}/`).toString();
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

function seoHead(options: ResolvedSeoOptions): ReactiveHead {
  const canonical = absoluteSiteUrl(options.path);
  const title = `${options.title} - ${BRAND_NAME}`;
  const image = options.image || DEFAULT_OG_IMAGE;
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
      { property: "og:site_name", content: BRAND_NAME },
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

export function commitSeo(options: ResolvedSeoOptions, head: ReturnType<typeof injectHead>): void {
  useHead(seoHead(options), { head });
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
