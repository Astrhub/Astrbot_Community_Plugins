import { computed, toValue, type MaybeRefOrGetter } from "vue";
import { useHead } from "@unhead/vue";

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

export function useSeo(options: SeoOptions): void {
  const canonical = computed(() => absoluteSiteUrl(toValue(options.path)));
  useHead(() => {
    const title = `${toValue(options.title)} - ${BRAND_NAME}`;
    const description = toValue(options.description);
    const image = options.image ? toValue(options.image) : DEFAULT_OG_IMAGE;
    const jsonLd = options.jsonLd ? toValue(options.jsonLd) : null;
    return {
      title,
      link: [{ rel: "canonical", href: canonical.value }],
      meta: [
        { name: "description", content: description },
        { name: "robots", content: options.robots ? toValue(options.robots) : "index,follow" },
        { property: "og:title", content: title },
        { property: "og:description", content: description },
        { property: "og:type", content: options.type || "website" },
        { property: "og:url", content: canonical.value },
        { property: "og:site_name", content: BRAND_NAME },
        { property: "og:image", content: image },
        { name: "twitter:card", content: "summary_large_image" },
      ],
      script: jsonLd
        ? [
            {
              id: options.jsonLdId || "page-jsonld",
              type: "application/ld+json",
              innerHTML: JSON.stringify(jsonLd),
            },
          ]
        : [],
    };
  });
}
