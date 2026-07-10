export type ArtifactSourceType = "upload" | "github";

export type ArtifactReviewStatus =
  | "quarantined"
  | "prechecking"
  | "scanning"
  | "pending_review"
  | "approved"
  | "rejected"
  | "withdrawn"
  | "processing_failed";

export type ArtifactPublicationStatus =
  | "unpublished"
  | "publishing"
  | "published"
  | "publish_failed"
  | "revoking"
  | "revoked"
  | "revoke_failed";

export type ArtifactRiskLevel = "none" | "low" | "medium" | "high" | "critical";

export interface PluginArtifact {
  id: string;
  plugin_id: string;
  plugin_name?: string;
  plugin_repo?: string;
  version: string;
  normalized_version: string;
  repo_version?: string;
  published_version?: string;
  source_type: ArtifactSourceType;
  source_ref?: string;
  source_commit_sha?: string;
  archive_sha256: string;
  size_bytes: number;
  review_status: ArtifactReviewStatus;
  publication_status: ArtifactPublicationStatus;
  risk_level: ArtifactRiskLevel;
  rejection_code?: string;
  download_url?: string | null;
  submitted_by?: string | null;
  owner_user_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ArtifactReviewRun {
  id: string;
  artifact_id: string;
  type: "precheck" | "static" | "runtime" | "llm_package" | "llm_file" | "llm_summary";
  status: "queued" | "running" | "succeeded" | "failed" | "timed_out" | "cancelled";
  summary?: string;
  error_code?: string;
  raw_result?: Record<string, unknown>;
  created_at: string;
  completed_at?: string | null;
}

export interface ArtifactFinding {
  id: string;
  rule_id?: string;
  file_path?: string;
  line_start?: number | null;
  line_end?: number | null;
  severity: "info" | "low" | "medium" | "high" | "critical";
  category?: string;
  message: string;
  suggestion?: string;
  evidence_excerpt?: string;
}

export interface ArtifactDecision {
  id: string;
  action: string;
  reason?: string;
  reviewer_nickname?: string;
  created_at: string;
}

export interface ArtifactDetail {
  artifact: PluginArtifact;
  runs: ArtifactReviewRun[];
  findings: ArtifactFinding[];
  decisions: ArtifactDecision[];
}
