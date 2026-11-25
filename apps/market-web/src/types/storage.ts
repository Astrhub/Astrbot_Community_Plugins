export type StorageBackend = "local" | "s3" | "webdav" | "imgbed";

export type StorageProfileStatus = "active" | "draining" | "retired" | "removed";

export type StoragePurpose = "artifact-quarantine" | "artifact-published" | "avatar";

export type LocalStorageMode = "filesystem" | "sqlite" | "postgresql";

export interface StorageCapabilities {
  conditional_create?: boolean;
  range_read?: boolean;
  public_read?: boolean;
  image_variants?: boolean;
  list_objects?: boolean;
}

export interface StorageProfileItem {
  id: string;
  name: string;
  backend: StorageBackend;
  status: StorageProfileStatus;
  config: Record<string, unknown>;
  has_secret: boolean;
  legacy: boolean;
  health_status: string;
  health_message: string;
  last_checked_at: string;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
  bound_purposes: StoragePurpose[];
  active_object_count: number;
  pending_object_count: number;
  has_active_migrations: boolean;
  capabilities: StorageCapabilities;
}

export interface StorageBindingItem {
  purpose: StoragePurpose;
  profile_id: string;
  generation: number;
  updated_by: string;
  updated_at: string;
}

export interface StorageUnknownObject {
  namespace: string;
  key: string;
  size_bytes: number;
}

export interface StorageMigrationItem {
  id: string;
  purpose: StoragePurpose;
  source_profile_id: string;
  target_profile_id: string;
  binding_generation: number;
  status:
    | "queued"
    | "running"
    | "paused"
    | "completed"
    | "completed_with_errors"
    | "failed"
    | "cancelled";
  total_count: number;
  completed_count: number;
  failed_count: number;
  skipped_count: number;
  unknown_objects: StorageUnknownObject[];
  last_error: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  completed_at: string;
}

export interface StorageMigrationEntry {
  id: string;
  migration_id: string;
  object_id: string;
  source_profile_id: string;
  source_namespace: string;
  source_key: string;
  source_public_url: string;
  target_profile_id: string;
  target_namespace: string;
  target_key: string;
  target_public_url: string;
  expected_sha256: string;
  status: "pending" | "running" | "completed" | "failed" | "skipped";
  attempts: number;
  last_error: string;
  created_at: string;
  updated_at: string;
}

export interface StorageMigrationDetail extends StorageMigrationItem {
  items: StorageMigrationEntry[];
}

export interface StorageProfileTestResult {
  ok: boolean;
  profile_id: string;
  health_status: string;
  health_message: string;
  checked_at: string;
  checks: Record<string, boolean>;
}

export interface StorageProfileCreatePayload {
  id: string;
  name: string;
  backend: StorageBackend;
  config: Record<string, unknown>;
  secret: Record<string, string>;
}

export interface StorageProfileUpdatePayload {
  name?: string;
  config?: Record<string, unknown>;
  secret?: Record<string, string>;
}
