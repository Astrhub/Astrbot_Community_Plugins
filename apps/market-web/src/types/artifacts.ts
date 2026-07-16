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
  base_artifact_id?: string | null;
  supersedes_artifact_id?: string | null;
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
  version: number;
  created_at: string;
  status_updated_at?: string | null;
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

export type ReviewWorkspaceView = "summary" | "files" | "diff" | "comments" | "history";
export type ReviewCommentSide = "base" | "current";

export interface ArtifactFile {
  id: string;
  artifact_id: string;
  path: string;
  language: string;
  mime_type: string;
  sha256: string;
  size_bytes: number;
  line_count: number | null;
  is_text: boolean;
  is_entrypoint: boolean;
  is_reachable: boolean;
  graph_status: "not_analyzed" | "complete" | "incomplete" | "not_applicable";
  content_available: boolean;
}

export interface ArtifactFileListResponse {
  artifact_id: string;
  tree_sha256: string;
  items: ArtifactFile[];
  total: number;
  limit: number;
  offset: number;
}

export interface ArtifactTextLine {
  number: number;
  text: string;
}

export interface ArtifactFileContentResponse {
  artifact_id: string;
  tree_sha256: string;
  file: ArtifactFile;
  encoding: "utf-8";
  start_line: number;
  end_line: number | null;
  total_lines: number;
  truncated: boolean;
  lines: ArtifactTextLine[];
}

export interface ArtifactDiffStats {
  base_size_bytes: number | null;
  current_size_bytes: number | null;
  base_line_count: number | null;
  current_line_count: number | null;
  forced_review: boolean;
  binary: boolean;
  added_lines: number;
  deleted_lines: number;
  hunk_count: number;
  hunks_complete: boolean;
  hunks_omitted: number;
  hunks_omitted_reason: string;
  hunks_truncated: boolean;
}

export interface ArtifactDiff {
  id: string;
  artifact_id: string;
  base_artifact_id: string | null;
  base_file_id: string | null;
  current_file_id: string | null;
  path: string;
  base_path: string;
  change_type: "added" | "deleted" | "modified" | "unchanged" | "renamed";
  base_sha256: string | null;
  current_sha256: string | null;
  base_tree_sha256: string | null;
  current_tree_sha256: string;
  stats: ArtifactDiffStats;
  has_hunks: boolean;
  created_at: string | null;
}

export interface ArtifactDiffListResponse {
  artifact_id: string;
  tree_sha256: string;
  items: ArtifactDiff[];
  total: number;
  limit: number;
  offset: number;
}

export interface ArtifactDiffLine {
  kind: "context" | "delete" | "add";
  prefix: " " | "-" | "+";
  text: string;
  newline: "none" | "lf" | "crlf" | "cr";
  old_line: number | null;
  new_line: number | null;
}

export interface ArtifactDiffHunk {
  id: string;
  header: string;
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  lines: ArtifactDiffLine[];
}

export interface ArtifactDiffContentResponse {
  artifact_id: string;
  tree_sha256: string;
  diff: ArtifactDiff;
  hunks_available: boolean;
  unavailable_reason: string;
  schema_version: string;
  tool_version: string;
  context_lines: number;
  truncated: boolean;
  omitted_hunks: number;
  hunks: ArtifactDiffHunk[];
}

export interface ReviewCommentEvent {
  id: string;
  thread_id: string;
  type: "create" | "edit" | "reply" | "resolve" | "reopen" | "author_addressed";
  body: string;
  actor_nickname: string;
  actor_role: "author" | "admin" | "core_admin" | "system";
  expected_version: number;
  resulting_version: number;
  created_at: string;
}

export interface ReviewComment {
  id: string;
  artifact_id: string;
  source_thread_id: string | null;
  file_id: string | null;
  file_path: string;
  file_sha256: string;
  side: ReviewCommentSide;
  line_start: number;
  line_end: number;
  body: string;
  reviewer_nickname: string;
  reviewer_role: "admin" | "core_admin";
  resolved: boolean;
  resolved_by_nickname: string;
  locked_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  event_count: number;
  events_truncated: boolean;
  events: ReviewCommentEvent[];
}

export interface ReviewCommentListResponse {
  artifact_id: string;
  items: ReviewComment[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReviewHistoryEvent {
  id: string;
  type:
    | "artifact_submitted"
    | "comment_event"
    | "decision"
    | "finding"
    | "finding_event"
    | "policy_event"
    | "publication_publish_failed"
    | "publication_published"
    | "publication_revoke_failed"
    | "publication_revoked"
    | "run";
  occurred_at: string;
  source: string;
  actor_nickname: string;
  actor_role: string;
  idempotency_key: string;
  policy_version_id: string | null;
  payload: Record<string, unknown>;
}

export interface ReviewHistoryResponse {
  artifact_id: string;
  items: ReviewHistoryEvent[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface ReviewAnchor {
  fileId: string;
  filePath: string;
  side: ReviewCommentSide;
  lineStart: number;
  lineEnd: number;
  diffId?: string;
  hunkId?: string;
}

export interface ReviewCommentCreateInput {
  file_id: string;
  side: ReviewCommentSide;
  line_start: number;
  line_end: number;
  body: string;
  diff_id?: string;
  hunk_id?: string;
  source_thread_id?: string;
}

export interface StableRiskEvidence {
  kind: "path_sha" | "dependency" | "fingerprint" | "admin_confirmation";
  deterministic: boolean;
  candidate_artifact_id: string;
  stable_artifact_id: string;
  finding_id: string;
  fingerprint?: string;
  path?: string;
  file_sha256?: string;
  package_name?: string;
  package_version?: string;
  advisory_id?: string;
  tool_name?: string;
  tool_version?: string;
  ruleset_version?: string;
  confirmed_by_nickname?: string;
  reason?: string;
}

export interface StableRiskResponse {
  candidate_artifact_id: string;
  finding_id: string;
  affects_current_release: true;
  correlation: StableRiskEvidence;
  stable_artifact: PluginArtifact;
}
