export const NEW_PLUGIN_DAYS = 14;

export function isNewPlugin(createdAt: unknown, now = Date.now()): boolean {
  if (typeof createdAt !== "string" || !createdAt.trim()) return false;
  const createdTime = new Date(createdAt).getTime();
  if (!Number.isFinite(createdTime) || createdTime > now) return false;
  return now - createdTime <= NEW_PLUGIN_DAYS * 24 * 60 * 60 * 1000;
}
