const GENERIC_IMAGE_ALTS = new Set([
  "公告图片",
  "图片",
  "图像",
  "截图",
  "image",
  "photo",
  "picture",
  "img",
  "screenshot",
]);

export function normalizeImageAlt(value: string): string {
  return value.trim().replace(/\s+/g, " ").replaceAll("[", "［").replaceAll("]", "］");
}

export function isDescriptiveImageAlt(value: string): boolean {
  const normalized = normalizeImageAlt(value);
  return normalized.length >= 2 && !GENERIC_IMAGE_ALTS.has(normalized.toLowerCase());
}

export function hasOnlyDescriptiveImages(body: string): boolean {
  for (const match of body.matchAll(/!\[([^\]]*)\]\(/g)) {
    if (!isDescriptiveImageAlt(match[1] || "")) return false;
  }

  for (const match of body.matchAll(/<img\b[^>]*>/gi)) {
    const altMatch = match[0].match(/\balt\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i);
    const alt = altMatch?.[1] ?? altMatch?.[2] ?? altMatch?.[3] ?? "";
    if (!isDescriptiveImageAlt(alt)) return false;
  }
  return true;
}
