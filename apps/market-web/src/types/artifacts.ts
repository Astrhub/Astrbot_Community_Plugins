export type ArtifactSourceType = "upload" | "github";

export type ArtifactReviewStatus =
  | "quarantined"
  | "prechecking"
  | "scanning"
  | "pending_review"
  | "changes_requested"
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
  suggested_category?: string;
  category_confidence?: number | null;
  category_reason?: string;
  policy_version_id?: string | null;
  review_coverage?: Record<string, unknown>;
  automated_review_completed_at?: string | null;
  reviewed_at?: string | null;
  published_at?: string | null;
  revoked_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ArtifactReviewRun {
  id: string;
  artifact_id: string;
  type:
    | "precheck"
    | "static"
    | "diff"
    | "import_graph"
    | "runtime"
    | "category"
    | "clamav"
    | "yara"
    | "dependency"
    | "llm_package"
    | "llm_file"
    | "llm_summary"
    | "routing";
  status: "queued" | "running" | "succeeded" | "failed" | "timed_out" | "cancelled";
  attempt: number;
  advisory: boolean;
  label: "自动审查建议" | "确定性检查";
  summary?: string;
  error_code?: string;
  model?: string;
  ruleset_version?: string;
  tool_name?: string;
  tool_version?: string;
  policy_version_id?: string | null;
  coverage: Record<string, unknown>;
  astrbot_version?: string;
  python_version?: string;
  platform?: string;
  created_at: string;
  completed_at?: string | null;
}

export interface ArtifactFinding {
  id: string;
  artifact_id: string;
  run_id: string;
  fingerprint: string;
  rule_id?: string;
  file_path?: string;
  line_start?: number | null;
  line_end?: number | null;
  severity: "info" | "low" | "medium" | "high" | "critical";
  category?: string;
  message: string;
  suggestion?: string;
  evidence_excerpt?: string;
  confidence?: number | null;
  status: "open" | "accepted" | "resolved" | "false_positive";
  source?: string;
  deterministic: boolean;
  advisory: boolean;
  label: "自动审查建议" | "确定性检查";
  affects_current_release?: boolean;
  created_at: string;
}

export interface ArtifactDecision {
  id: string;
  artifact_id: string;
  action: string;
  from_status: string;
  to_status: string;
  reason?: string;
  reviewer_nickname?: string;
  policy_version?: string;
  policy_version_id?: string | null;
  source: "admin" | "system" | "policy";
  input_run_ids: string[];
  input_fingerprints: string[];
  coverage_sha256?: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ArtifactDetail {
  artifact: PluginArtifact;
  runs: ArtifactReviewRun[];
  findings: ArtifactFinding[];
  decisions: ArtifactDecision[];
}
