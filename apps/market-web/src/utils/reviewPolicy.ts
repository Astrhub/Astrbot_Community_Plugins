import type { ReviewPolicyDocument } from "@/types/artifacts";

export function createDefaultReviewPolicy(): ReviewPolicyDocument {
  return {
    schema_version: "1",
    required_stages: ["static"],
    runtime_targets: [],
    limits: {
      cpu: 1,
      memory_mb: 768,
      pids: 128,
      timeout_seconds: 120,
      disk_mb: 2048,
      tmpfs_mb: 512,
      max_log_bytes: 1_048_576,
    },
    network_profiles: {
      install: "pypi-only-v1",
      smoke: "none",
      on_unverified: "fail_closed",
    },
    llm: {
      enabled: false,
      provider_config_ref: "config:llm-default",
      model: "",
      prompt_version: "v1",
      max_tokens: 0,
      max_cost_microusd: 0,
      input_cost_microusd_per_million_tokens: 0,
      output_cost_microusd_per_million_tokens: 0,
      max_files: 20,
      max_file_bytes: 262_144,
      required_files: [],
      timeout_seconds: 90,
      max_retries: 2,
    },
    malware: {
      clamav: false,
      clamav_config_ref: "config:clamav-default",
      yara_ruleset: null,
      max_database_age_hours: 24,
      on_unknown: "fail_closed",
      max_files: 2000,
      max_file_bytes: 8 * 1024 * 1024,
      max_total_bytes: 128 * 1024 * 1024,
      timeout_seconds: 60,
      per_file_timeout_seconds: 10,
      max_matches: 200,
      max_offsets_per_match: 16,
      max_output_bytes: 256 * 1024,
      subprocess_memory_mb: 512,
    },
    dependency: {
      enabled: false,
      advisory_config_ref: "config:dependency-default",
      max_severity: "high",
      max_data_age_hours: 24,
      on_unavailable: "manual_review",
      allow_direct_urls: false,
      allow_vcs: false,
      denied_licenses: [],
      private_package_prefixes: [],
    },
    category: {
      enabled: false,
      provider_config_ref: "config:llm-default",
      model: "",
      minimum_confidence: 0.8,
      allowed_categories: [
        "ai_tools",
        "entertainment",
        "integrations",
        "productivity",
        "utilities",
        "other",
      ],
      default_category: "other",
      max_input_chars: 32_000,
      max_output_tokens: 512,
      prompt_version: "category-prompt-v1",
    },
    routing: {
      auto_approve: false,
      manual_review_at: "low",
      deterministic_reject_at: "critical",
      degraded_action: "manual_review",
      require_complete_coverage: true,
    },
  };
}

export function cloneReviewPolicy(policy: ReviewPolicyDocument): ReviewPolicyDocument {
  return JSON.parse(JSON.stringify(policy)) as ReviewPolicyDocument;
}
