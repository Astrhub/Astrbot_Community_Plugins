ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS repo_version text NOT NULL DEFAULT '';
ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS current_artifact_id text;
ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS category text NOT NULL DEFAULT 'other';
ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS category_source text NOT NULL DEFAULT 'user';

UPDATE market_plugins
   SET repo_version = COALESCE(NULLIF(metadata->>'version', ''), repo_version)
 WHERE repo_version = '';

UPDATE market_plugins
   SET category = CASE
       WHEN metadata->>'category' IN (
           'ai_tools', 'entertainment', 'integrations', 'productivity', 'utilities', 'other'
       ) THEN metadata->>'category'
       ELSE 'other'
   END
 WHERE category = 'other';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'market_plugins_category_check'
           AND conrelid = 'market_plugins'::regclass
    ) THEN
        ALTER TABLE market_plugins
            ADD CONSTRAINT market_plugins_category_check
            CHECK (category IN (
                'ai_tools', 'entertainment', 'integrations',
                'productivity', 'utilities', 'other'
            ));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'market_plugins_category_source_check'
           AND conrelid = 'market_plugins'::regclass
    ) THEN
        ALTER TABLE market_plugins
            ADD CONSTRAINT market_plugins_category_source_check
            CHECK (category_source IN ('user', 'ai', 'reviewer'));
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS plugin_artifacts (
    id text PRIMARY KEY,
    plugin_id text NOT NULL REFERENCES market_plugins(id) ON DELETE CASCADE,
    version text NOT NULL DEFAULT '',
    normalized_version text NOT NULL DEFAULT '',
    source_type text NOT NULL CHECK (source_type IN ('upload', 'github')),
    source_repo text NOT NULL,
    source_ref text NOT NULL DEFAULT '',
    source_commit_sha text NOT NULL DEFAULT '',
    archive_sha256 text NOT NULL CHECK (length(archive_sha256) = 64),
    tree_sha256 text NOT NULL DEFAULT '',
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    quarantine_key text NOT NULL UNIQUE,
    published_key text UNIQUE,
    path_suffix text NOT NULL CHECK (path_suffix ~ '^[a-f0-9]{8,12}$'),
    download_url text UNIQUE,
    review_status text NOT NULL DEFAULT 'quarantined' CHECK (
        review_status IN (
            'quarantined', 'prechecking', 'scanning', 'pending_review',
            'approved', 'rejected', 'withdrawn', 'processing_failed'
        )
    ),
    publication_status text NOT NULL DEFAULT 'unpublished' CHECK (
        publication_status IN (
            'unpublished', 'publishing', 'published', 'publish_failed',
            'revoking', 'revoked', 'revoke_failed'
        )
    ),
    risk_level text NOT NULL DEFAULT 'none' CHECK (
        risk_level IN ('none', 'low', 'medium', 'high', 'critical')
    ),
    base_artifact_id text REFERENCES plugin_artifacts(id) ON DELETE SET NULL,
    submitted_by text REFERENCES market_users(id) ON DELETE SET NULL,
    submitted_by_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    suggested_category text NOT NULL DEFAULT '',
    category_confidence numeric(5, 4),
    category_reason text NOT NULL DEFAULT '',
    rejection_code text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz,
    published_at timestamptz,
    revoked_at timestamptz,
    UNIQUE (plugin_id, archive_sha256)
);

CREATE INDEX IF NOT EXISTS plugin_artifacts_plugin_created_idx
    ON plugin_artifacts(plugin_id, created_at DESC);
CREATE INDEX IF NOT EXISTS plugin_artifacts_review_queue_idx
    ON plugin_artifacts(review_status, risk_level, created_at DESC);
CREATE INDEX IF NOT EXISTS plugin_artifacts_publication_idx
    ON plugin_artifacts(publication_status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS plugin_artifacts_published_version_idx
    ON plugin_artifacts(plugin_id, normalized_version)
    WHERE publication_status = 'published';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'market_plugins_current_artifact_fk'
           AND conrelid = 'market_plugins'::regclass
    ) THEN
        ALTER TABLE market_plugins
            ADD CONSTRAINT market_plugins_current_artifact_fk
            FOREIGN KEY (current_artifact_id)
            REFERENCES plugin_artifacts(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS market_plugins_current_artifact_idx
    ON market_plugins(current_artifact_id)
    WHERE current_artifact_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS artifact_files (
    id text PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    path text NOT NULL,
    language text NOT NULL DEFAULT '',
    mime_type text NOT NULL DEFAULT 'application/octet-stream',
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    line_count integer CHECK (line_count IS NULL OR line_count >= 0),
    is_text boolean NOT NULL DEFAULT false,
    content_key text,
    flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (artifact_id, path)
);

CREATE INDEX IF NOT EXISTS artifact_files_artifact_idx
    ON artifact_files(artifact_id, path);

CREATE TABLE IF NOT EXISTS review_runs (
    id text PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    type text NOT NULL CHECK (
        type IN (
            'precheck', 'static', 'runtime',
            'llm_package', 'llm_file', 'llm_summary'
        )
    ),
    status text NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled')
    ),
    attempt integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    ruleset_version text NOT NULL DEFAULT '',
    model text NOT NULL DEFAULT '',
    summary text NOT NULL DEFAULT '',
    raw_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_result_key text,
    error_code text NOT NULL DEFAULT '',
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_runs_artifact_idx
    ON review_runs(artifact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS review_runs_status_idx
    ON review_runs(status, created_at);

CREATE TABLE IF NOT EXISTS review_findings (
    id text PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    run_id text NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    fingerprint text NOT NULL,
    rule_id text NOT NULL DEFAULT '',
    file_path text NOT NULL DEFAULT '',
    line_start integer CHECK (line_start IS NULL OR line_start >= 1),
    line_end integer CHECK (line_end IS NULL OR line_end >= 1),
    severity text NOT NULL CHECK (
        severity IN ('info', 'low', 'medium', 'high', 'critical')
    ),
    category text NOT NULL DEFAULT '',
    message text NOT NULL,
    suggestion text NOT NULL DEFAULT '',
    evidence_excerpt text NOT NULL DEFAULT '',
    confidence numeric(5, 4),
    status text NOT NULL DEFAULT 'open' CHECK (
        status IN ('open', 'accepted', 'resolved', 'false_positive')
    ),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (artifact_id, fingerprint),
    CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start)
);

CREATE INDEX IF NOT EXISTS review_findings_artifact_idx
    ON review_findings(artifact_id, severity, file_path);
CREATE INDEX IF NOT EXISTS review_findings_run_idx
    ON review_findings(run_id);

CREATE TABLE IF NOT EXISTS review_decisions (
    id text PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    action text NOT NULL CHECK (
        action IN (
            'auto_reject', 'approve', 'reject', 'retry_publish',
            'revoke', 'emergency_override'
        )
    ),
    from_status text NOT NULL,
    to_status text NOT NULL,
    reason text NOT NULL DEFAULT '',
    reviewer_user_id text REFERENCES market_users(id) ON DELETE SET NULL,
    reviewer_nickname text NOT NULL DEFAULT '',
    policy_version text NOT NULL DEFAULT 'p1',
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_decisions_artifact_idx
    ON review_decisions(artifact_id, created_at DESC);

CREATE TABLE IF NOT EXISTS artifact_jobs (
    id text PRIMARY KEY,
    artifact_id text REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    type text NOT NULL CHECK (
        type IN ('precheck', 'static_scan', 'publish', 'revoke', 'outbox', 'cleanup_orphan')
    ),
    status text NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    idempotency_key text NOT NULL UNIQUE,
    last_error_code text NOT NULL DEFAULT '',
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS artifact_jobs_claim_idx
    ON artifact_jobs(status, available_at, created_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS artifact_jobs_artifact_idx
    ON artifact_jobs(artifact_id, created_at DESC)
    WHERE artifact_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS outbox_events (
    id text PRIMARY KEY,
    event_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    recipient_user_id text REFERENCES market_users(id) ON DELETE SET NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'delivered', 'failed', 'cancelled')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    delivered_at timestamptz,
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS outbox_events_delivery_idx
    ON outbox_events(status, available_at, created_at)
    WHERE status IN ('queued', 'failed');

ALTER TABLE market_notifications
    ADD COLUMN IF NOT EXISTS dedupe_key text;
CREATE UNIQUE INDEX IF NOT EXISTS market_notifications_dedupe_idx
    ON market_notifications(dedupe_key)
    WHERE dedupe_key IS NOT NULL;
