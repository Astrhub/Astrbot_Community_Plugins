import type {
  ArtifactPublicationStatus,
  ArtifactReviewStatus,
  ArtifactRiskLevel,
} from "@/types/artifacts";

export const REVIEW_STATUS_LABELS: Record<ArtifactReviewStatus, string> = {
  quarantined: "隔离中",
  prechecking: "基础校验",
  scanning: "静态扫描",
  pending_review: "待人工审查",
  changes_requested: "需要修改",
  approved: "已批准",
  rejected: "已拒绝",
  withdrawn: "已撤回",
  processing_failed: "处理失败",
};

export const PUBLICATION_STATUS_LABELS: Record<ArtifactPublicationStatus, string> = {
  unpublished: "未发布",
  publishing: "发布中",
  published: "CDN 已发布",
  publish_failed: "发布失败",
  revoking: "撤回中",
  revoked: "已撤回",
  revoke_failed: "撤回失败",
};

export const RISK_LABELS: Record<ArtifactRiskLevel, string> = {
  none: "无命中",
  low: "低风险",
  medium: "中风险",
  high: "高风险",
  critical: "严重风险",
};

export function reviewTagType(
  status: ArtifactReviewStatus,
): "default" | "info" | "success" | "warning" | "error" {
  if (status === "approved") return "success";
  if (status === "rejected" || status === "processing_failed") return "error";
  if (status === "pending_review" || status === "changes_requested") return "warning";
  return "info";
}

export function riskTagType(
  risk: ArtifactRiskLevel,
): "default" | "info" | "success" | "warning" | "error" {
  if (risk === "critical" || risk === "high") return "error";
  if (risk === "medium") return "warning";
  if (risk === "low") return "info";
  return "success";
}

export function formatArtifactTime(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}
