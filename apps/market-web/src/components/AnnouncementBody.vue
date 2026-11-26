<template>
  <div class="announcement-body" v-html="renderedBody"></div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { isDescriptiveImageAlt } from "@/utils/announcement";

const props = defineProps<{ body: string }>();

const renderedBody = computed(() => {
  const html = marked.parse(props.body, { async: false, gfm: true, breaks: true });
  const sanitized = DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed", "svg", "math"],
    FORBID_ATTR: ["style"],
    ALLOWED_URI_REGEXP: /^(?:(?:https):|\/(?!\/)|#)/i,
  });
  const parsed = new DOMParser().parseFromString(sanitized, "text/html");
  for (const image of parsed.querySelectorAll("img")) {
    if (!isDescriptiveImageAlt(image.getAttribute("alt") || "")) image.remove();
  }
  return parsed.body.innerHTML;
});
</script>

<style scoped>
.announcement-body {
  margin-top: 12px;
  color: var(--text-primary);
  font-size: 14px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}

.announcement-body :deep(p),
.announcement-body :deep(ul),
.announcement-body :deep(ol),
.announcement-body :deep(blockquote),
.announcement-body :deep(pre) {
  margin: 0 0 12px;
}

.announcement-body :deep(> :last-child) {
  margin-bottom: 0;
}

.announcement-body :deep(ul),
.announcement-body :deep(ol) {
  padding-left: 22px;
}

.announcement-body :deep(a) {
  color: var(--primary-color);
  text-decoration: underline;
}

.announcement-body :deep(img) {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 12px 0;
  border-radius: 12px;
}

.announcement-body :deep(pre) {
  padding: 12px;
  overflow-x: auto;
  background: var(--bg-hover);
  border-radius: 10px;
}

.announcement-body :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}
</style>
