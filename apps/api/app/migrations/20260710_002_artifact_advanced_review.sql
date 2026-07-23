ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS suggested_category text NOT NULL DEFAULT '';
ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS category_confidence numeric(5, 4);
ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS category_reason text NOT NULL DEFAULT '';

ALTER TABLE market_plugins
    DROP CONSTRAINT IF EXISTS market_plugins_category_confidence_check;
ALTER TABLE market_plugins
    ADD CONSTRAINT market_plugins_category_confidence_check
    CHECK (
        category_confidence IS NULL
        OR (category_confidence >= 0 AND category_confidence <= 1)
    );

CREATE TABLE IF NOT EXISTS review_policies (
    id text PRIMARY KEY,
    version text NOT NULL UNIQUE,
    schema_version text NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft', 'active', 'retired')
    ),
    is_default boolean NOT NULL DEFAULT true,
    policy jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(policy) = 'object'
    ),
    policy_sha256 text NOT NULL CHECK (length(policy_sha256) = 64),
    base_policy_id text REFERENCES review_policies(id) ON DELETE RESTRICT,
    created_by_user_id text REFERENCES market_users(id) ON DELETE SET NULL,
    created_by_nickname text NOT NULL DEFAULT '',
    validation_summary jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(validation_summary) = 'object'
    ),
    validated_at timestamptz,
    activated_at timestamptz,
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status <> 'active' OR activated_at IS NOT NULL),
    CHECK (status <> 'retired' OR retired_at IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS review_policies_active_default_idx
    ON review_policies(is_default)
    WHERE status = 'active' AND is_default;
CREATE INDEX IF NOT EXISTS review_policies_status_created_idx
    ON review_policies(status, created_at DESC);

CREATE TABLE IF NOT EXISTS review_policy_events (
    id text PRIMARY KEY,
    policy_id text NOT NULL REFERENCES review_policies(id) ON DELETE RESTRICT,
    action text NOT NULL CHECK (
        action IN ('create', 'validate', 'activate', 'retire', 'rollback')
    ),
    actor_user_id text REFERENCES market_users(id) ON DELETE SET NULL,
    actor_nickname text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    request_id text NOT NULL,
    base_version text NOT NULL DEFAULT '',
    diff jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(diff) = 'object'
    ),
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_policy_events_policy_idx
    ON review_policy_events(policy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS review_policy_events_request_idx
    ON review_policy_events(request_id, created_at DESC);

ALTER TABLE plugin_artifacts
    ADD COLUMN IF NOT EXISTS policy_version_id text
        REFERENCES review_policies(id) ON DELETE RESTRICT;
ALTER TABLE plugin_artifacts
    ADD COLUMN IF NOT EXISTS supersedes_artifact_id text
        REFERENCES plugin_artifacts(id) ON DELETE SET NULL;
ALTER TABLE plugin_artifacts
    ADD COLUMN IF NOT EXISTS review_coverage jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE plugin_artifacts
    ADD COLUMN IF NOT EXISTS automated_review_completed_at timestamptz;

ALTER TABLE plugin_artifacts
    DROP CONSTRAINT IF EXISTS plugin_artifacts_review_status_check;
ALTER TABLE plugin_artifacts
    ADD CONSTRAINT plugin_artifacts_review_status_check
    CHECK (
        review_status IN (
            'quarantined', 'prechecking', 'scanning', 'pending_review',
            'changes_requested', 'approved', 'rejected', 'withdrawn',
            'processing_failed'
        )
    );
ALTER TABLE plugin_artifacts
    DROP CONSTRAINT IF EXISTS plugin_artifacts_review_coverage_object_check;
ALTER TABLE plugin_artifacts
    ADD CONSTRAINT plugin_artifacts_review_coverage_object_check
    CHECK (jsonb_typeof(review_coverage) = 'object');
ALTER TABLE plugin_artifacts
    DROP CONSTRAINT IF EXISTS plugin_artifacts_lineage_not_self_check;
ALTER TABLE plugin_artifacts
    ADD CONSTRAINT plugin_artifacts_lineage_not_self_check
    CHECK (
        (base_artifact_id IS NULL OR base_artifact_id <> id)
        AND (supersedes_artifact_id IS NULL OR supersedes_artifact_id <> id)
    );

CREATE INDEX IF NOT EXISTS plugin_artifacts_policy_idx
    ON plugin_artifacts(policy_version_id, review_status, created_at DESC)
    WHERE policy_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS plugin_artifacts_supersedes_idx
    ON plugin_artifacts(supersedes_artifact_id, created_at DESC)
    WHERE supersedes_artifact_id IS NOT NULL;

CREATE OR REPLACE FUNCTION enforce_plugin_artifact_lineage_same_plugin()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    related_plugin_id text;
BEGIN
    IF NEW.base_artifact_id IS NOT NULL THEN
        SELECT plugin_id
          INTO related_plugin_id
          FROM plugin_artifacts
         WHERE id = NEW.base_artifact_id;
        IF related_plugin_id IS NOT NULL AND related_plugin_id <> NEW.plugin_id THEN
            RAISE EXCEPTION 'base artifact must belong to the same plugin'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    IF NEW.supersedes_artifact_id IS NOT NULL THEN
        SELECT plugin_id
          INTO related_plugin_id
          FROM plugin_artifacts
         WHERE id = NEW.supersedes_artifact_id;
        IF related_plugin_id IS NOT NULL AND related_plugin_id <> NEW.plugin_id THEN
            RAISE EXCEPTION 'superseded artifact must belong to the same plugin'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' AND NEW.plugin_id IS DISTINCT FROM OLD.plugin_id THEN
        IF EXISTS (
            SELECT 1
              FROM plugin_artifacts child
             WHERE (child.base_artifact_id = NEW.id OR child.supersedes_artifact_id = NEW.id)
               AND child.plugin_id <> NEW.plugin_id
        ) THEN
            RAISE EXCEPTION 'artifact plugin cannot break existing lineage'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS plugin_artifacts_lineage_same_plugin_trigger ON plugin_artifacts;
DO $$
BEGIN
    EXECUTE format(
        'CREATE TRIGGER plugin_artifacts_lineage_same_plugin_trigger '
        'BEFORE INSERT OR UPDATE OF plugin_id, base_artifact_id, supersedes_artifact_id '
        'ON plugin_artifacts FOR EACH ROW EXECUTE FUNCTION %I.'
        'enforce_plugin_artifact_lineage_same_plugin()',
        current_schema()
    );
END
$$;

ALTER TABLE artifact_files
    ADD COLUMN IF NOT EXISTS is_entrypoint boolean NOT NULL DEFAULT false;
ALTER TABLE artifact_files
    ADD COLUMN IF NOT EXISTS is_reachable boolean NOT NULL DEFAULT false;
ALTER TABLE artifact_files
    ADD COLUMN IF NOT EXISTS graph_status text NOT NULL DEFAULT 'not_analyzed';
ALTER TABLE artifact_files
    ADD COLUMN IF NOT EXISTS scan_summary jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE artifact_files
    DROP CONSTRAINT IF EXISTS artifact_files_graph_status_check;
ALTER TABLE artifact_files
    ADD CONSTRAINT artifact_files_graph_status_check
    CHECK (
        graph_status IN ('not_analyzed', 'complete', 'incomplete', 'not_applicable')
    );
ALTER TABLE artifact_files
    DROP CONSTRAINT IF EXISTS artifact_files_scan_summary_object_check;
ALTER TABLE artifact_files
    ADD CONSTRAINT artifact_files_scan_summary_object_check
    CHECK (jsonb_typeof(scan_summary) = 'object');

CREATE INDEX IF NOT EXISTS artifact_files_entrypoint_idx
    ON artifact_files(artifact_id, is_entrypoint, path)
    WHERE is_entrypoint OR is_reachable;

CREATE TABLE IF NOT EXISTS artifact_file_diffs (
    id text PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    base_artifact_id text REFERENCES plugin_artifacts(id) ON DELETE SET NULL,
    base_file_id text REFERENCES artifact_files(id) ON DELETE SET NULL,
    current_file_id text REFERENCES artifact_files(id) ON DELETE SET NULL,
    path text NOT NULL CHECK (length(path) > 0),
    base_path text NOT NULL DEFAULT '',
    change_type text NOT NULL CHECK (
        change_type IN ('added', 'deleted', 'modified', 'unchanged', 'renamed')
    ),
    base_sha256 text CHECK (base_sha256 IS NULL OR length(base_sha256) = 64),
    current_sha256 text CHECK (current_sha256 IS NULL OR length(current_sha256) = 64),
    base_tree_sha256 text CHECK (
        base_tree_sha256 IS NULL OR length(base_tree_sha256) = 64
    ),
    current_tree_sha256 text NOT NULL CHECK (length(current_tree_sha256) = 64),
    hunks_key text,
    stats jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(stats) = 'object'
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (base_file_id IS NULL OR base_sha256 IS NOT NULL),
    CHECK (current_file_id IS NULL OR current_sha256 IS NOT NULL),
    CHECK (base_artifact_id IS NULL OR base_tree_sha256 IS NOT NULL),
    CHECK (
        (change_type = 'added' AND base_sha256 IS NULL AND current_sha256 IS NOT NULL)
        OR (
            change_type = 'deleted'
            AND base_sha256 IS NOT NULL
            AND current_sha256 IS NULL
        )
        OR (
            change_type IN ('modified', 'unchanged', 'renamed')
            AND base_sha256 IS NOT NULL
            AND current_sha256 IS NOT NULL
        )
    ),
    CHECK (change_type = 'added' OR length(base_path) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS artifact_file_diffs_identity_idx
    ON artifact_file_diffs(artifact_id, COALESCE(base_artifact_id, ''), path);
CREATE INDEX IF NOT EXISTS artifact_file_diffs_query_idx
    ON artifact_file_diffs(artifact_id, change_type, path);
CREATE INDEX IF NOT EXISTS artifact_file_diffs_base_idx
    ON artifact_file_diffs(base_artifact_id, path)
    WHERE base_artifact_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS artifact_dependency_edges (
    id text PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    source_file_id text NOT NULL REFERENCES artifact_files(id) ON DELETE CASCADE,
    target_file_id text REFERENCES artifact_files(id) ON DELETE SET NULL,
    target_name text NOT NULL DEFAULT '',
    edge_type text NOT NULL CHECK (
        edge_type IN ('import', 'from', 'dynamic', 'unknown')
    ),
    confidence numeric(5, 4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    line_start integer CHECK (line_start IS NULL OR line_start >= 1),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(metadata) = 'object'
    ),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS artifact_dependency_edges_identity_idx
    ON artifact_dependency_edges(
        artifact_id,
        source_file_id,
        COALESCE(target_file_id, ''),
        target_name,
        edge_type,
        COALESCE(line_start, 0)
    );
CREATE INDEX IF NOT EXISTS artifact_dependency_edges_source_idx
    ON artifact_dependency_edges(artifact_id, source_file_id);
CREATE INDEX IF NOT EXISTS artifact_dependency_edges_target_idx
    ON artifact_dependency_edges(artifact_id, target_file_id)
    WHERE target_file_id IS NOT NULL;

ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS tool_name text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS tool_version text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS policy_version_id text
        REFERENCES review_policies(id) ON DELETE RESTRICT;
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS input_sha256 text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS output_sha256 text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS coverage jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS prompt_version text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS result_schema_version text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS container_image_digest text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS astrbot_version text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS python_version text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS dependency_snapshot_sha256 text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS worker_id text NOT NULL DEFAULT '';
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS idempotency_key text;
ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS queued_at timestamptz;

UPDATE review_runs
   SET queued_at = created_at
 WHERE queued_at IS NULL;

ALTER TABLE review_runs
    ALTER COLUMN queued_at SET DEFAULT now();
ALTER TABLE review_runs
    ALTER COLUMN queued_at SET NOT NULL;
ALTER TABLE review_runs
    DROP CONSTRAINT IF EXISTS review_runs_type_check;
ALTER TABLE review_runs
    ADD CONSTRAINT review_runs_type_check
    CHECK (
        type IN (
            'precheck', 'static', 'diff', 'import_graph', 'runtime',
            'category', 'clamav', 'yara', 'dependency',
            'llm_package', 'llm_file', 'llm_summary', 'routing'
        )
    );
ALTER TABLE review_runs
    DROP CONSTRAINT IF EXISTS review_runs_input_sha256_check;
ALTER TABLE review_runs
    ADD CONSTRAINT review_runs_input_sha256_check
    CHECK (input_sha256 = '' OR length(input_sha256) = 64);
ALTER TABLE review_runs
    DROP CONSTRAINT IF EXISTS review_runs_output_sha256_check;
ALTER TABLE review_runs
    ADD CONSTRAINT review_runs_output_sha256_check
    CHECK (output_sha256 = '' OR length(output_sha256) = 64);
ALTER TABLE review_runs
    DROP CONSTRAINT IF EXISTS review_runs_dependency_snapshot_sha256_check;
ALTER TABLE review_runs
    ADD CONSTRAINT review_runs_dependency_snapshot_sha256_check
    CHECK (
        dependency_snapshot_sha256 = '' OR length(dependency_snapshot_sha256) = 64
    );
ALTER TABLE review_runs
    DROP CONSTRAINT IF EXISTS review_runs_coverage_object_check;
ALTER TABLE review_runs
    ADD CONSTRAINT review_runs_coverage_object_check
    CHECK (jsonb_typeof(coverage) = 'object');
ALTER TABLE review_runs
    DROP CONSTRAINT IF EXISTS review_runs_raw_result_object_check;
ALTER TABLE review_runs
    ADD CONSTRAINT review_runs_raw_result_object_check
    CHECK (jsonb_typeof(raw_result) = 'object');

CREATE UNIQUE INDEX IF NOT EXISTS review_runs_idempotency_idx
    ON review_runs(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS review_runs_policy_idx
    ON review_runs(policy_version_id, artifact_id, queued_at DESC)
    WHERE policy_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS review_runs_tool_idx
    ON review_runs(type, tool_name, tool_version, created_at DESC);

CREATE TABLE IF NOT EXISTS runtime_dispatches (
    id text PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    run_id text NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled')
    ),
    request jsonb NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    request_sha256 text NOT NULL CHECK (length(request_sha256) = 64),
    result_key text,
    result_sha256 text CHECK (result_sha256 IS NULL OR length(result_sha256) = 64),
    runner_id text NOT NULL DEFAULT '',
    image_digest text NOT NULL DEFAULT '',
    lease_owner text,
    lease_expires_at timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    collected_at timestamptz,
    error_code text NOT NULL DEFAULT '',
    error_message text NOT NULL DEFAULT '',
    queued_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((result_key IS NULL) = (result_sha256 IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS runtime_dispatches_active_run_idx
    ON runtime_dispatches(run_id)
    WHERE status <> 'cancelled';
CREATE INDEX IF NOT EXISTS runtime_dispatches_claim_idx
    ON runtime_dispatches(status, lease_expires_at, queued_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS runtime_dispatches_artifact_idx
    ON runtime_dispatches(artifact_id, queued_at DESC);
CREATE INDEX IF NOT EXISTS runtime_dispatches_collect_idx
    ON runtime_dispatches(status, completed_at)
    WHERE status IN ('succeeded', 'failed', 'timed_out') AND collected_at IS NULL;

CREATE OR REPLACE FUNCTION enforce_runtime_dispatch_run_artifact()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM review_runs
         WHERE id = NEW.run_id
           AND artifact_id = NEW.artifact_id
           AND type = 'runtime'
    ) THEN
        RAISE EXCEPTION 'runtime dispatch must reference a runtime run for the same artifact'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS runtime_dispatches_run_artifact_trigger ON runtime_dispatches;
DO $$
BEGIN
    EXECUTE format(
        'CREATE TRIGGER runtime_dispatches_run_artifact_trigger '
        'BEFORE INSERT OR UPDATE OF artifact_id, run_id '
        'ON runtime_dispatches FOR EACH ROW EXECUTE FUNCTION %I.'
        'enforce_runtime_dispatch_run_artifact()',
        current_schema()
    );
END
$$;

ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'static';
ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS deterministic boolean NOT NULL DEFAULT true;
ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS file_id text REFERENCES artifact_files(id) ON DELETE SET NULL;
ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS file_sha256 text;
ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS affects_current_release boolean NOT NULL DEFAULT false;
ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS correlation jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS status_actor_user_id text
        REFERENCES market_users(id) ON DELETE SET NULL;
ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS status_actor_nickname text NOT NULL DEFAULT '';
ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS status_updated_at timestamptz;
ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1;

UPDATE review_findings finding
   SET source = CASE
           WHEN run.type = 'precheck' THEN 'precheck'
           WHEN run.type = 'runtime' THEN 'runtime'
           WHEN run.type LIKE 'llm_%' THEN 'llm'
           ELSE 'static'
       END,
       deterministic = run.type NOT LIKE 'llm_%'
  FROM review_runs run
 WHERE run.id = finding.run_id;

ALTER TABLE review_findings
    DROP CONSTRAINT IF EXISTS review_findings_source_check;
ALTER TABLE review_findings
    ADD CONSTRAINT review_findings_source_check
    CHECK (
        source IN (
            'precheck', 'static', 'runtime', 'llm', 'clamav',
            'yara', 'dependency', 'reviewer', 'system'
        )
    );
ALTER TABLE review_findings
    DROP CONSTRAINT IF EXISTS review_findings_file_sha256_check;
ALTER TABLE review_findings
    ADD CONSTRAINT review_findings_file_sha256_check
    CHECK (file_sha256 IS NULL OR length(file_sha256) = 64);
ALTER TABLE review_findings
    DROP CONSTRAINT IF EXISTS review_findings_correlation_object_check;
ALTER TABLE review_findings
    ADD CONSTRAINT review_findings_correlation_object_check
    CHECK (jsonb_typeof(correlation) = 'object');
ALTER TABLE review_findings
    DROP CONSTRAINT IF EXISTS review_findings_version_check;
ALTER TABLE review_findings
    ADD CONSTRAINT review_findings_version_check
    CHECK (version >= 1);

CREATE INDEX IF NOT EXISTS review_findings_source_status_idx
    ON review_findings(artifact_id, source, status, severity, created_at DESC);
CREATE INDEX IF NOT EXISTS review_findings_current_release_idx
    ON review_findings(artifact_id, affects_current_release, severity)
    WHERE affects_current_release;
CREATE INDEX IF NOT EXISTS review_findings_file_idx
    ON review_findings(file_id, created_at DESC)
    WHERE file_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS review_finding_events (
    id text PRIMARY KEY,
    finding_id text NOT NULL REFERENCES review_findings(id) ON DELETE CASCADE,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    type text NOT NULL CHECK (
        type IN ('status_changed', 'correlation_changed', 'current_release_linked')
    ),
    from_status text CHECK (
        from_status IS NULL
        OR from_status IN ('open', 'accepted', 'resolved', 'false_positive')
    ),
    to_status text CHECK (
        to_status IS NULL
        OR to_status IN ('open', 'accepted', 'resolved', 'false_positive')
    ),
    actor_user_id text REFERENCES market_users(id) ON DELETE SET NULL,
    actor_nickname text NOT NULL DEFAULT '',
    actor_source text NOT NULL CHECK (
        actor_source IN ('user', 'system', 'policy')
    ),
    reason text NOT NULL DEFAULT '',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(metadata) = 'object'
    ),
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_finding_events_finding_idx
    ON review_finding_events(finding_id, created_at DESC);
CREATE INDEX IF NOT EXISTS review_finding_events_artifact_idx
    ON review_finding_events(artifact_id, created_at DESC);

CREATE TABLE IF NOT EXISTS review_comments (
    id text PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    source_thread_id text REFERENCES review_comments(id) ON DELETE SET NULL,
    file_id text REFERENCES artifact_files(id) ON DELETE SET NULL,
    file_path text NOT NULL CHECK (length(file_path) > 0),
    file_sha256 text NOT NULL CHECK (length(file_sha256) = 64),
    side text NOT NULL CHECK (side IN ('base', 'current')),
    line_start integer NOT NULL CHECK (line_start >= 1),
    line_end integer NOT NULL CHECK (line_end >= line_start),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 10000),
    reviewer_user_id text REFERENCES market_users(id) ON DELETE SET NULL,
    reviewer_nickname text NOT NULL DEFAULT '',
    reviewer_role text NOT NULL CHECK (reviewer_role IN ('admin', 'core_admin')),
    resolved boolean NOT NULL DEFAULT false,
    resolved_by_user_id text REFERENCES market_users(id) ON DELETE SET NULL,
    resolved_by_nickname text NOT NULL DEFAULT '',
    locked_at timestamptz,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    CHECK (
        (resolved AND resolved_at IS NOT NULL)
        OR (NOT resolved AND resolved_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS review_comments_artifact_idx
    ON review_comments(artifact_id, file_path, created_at);
CREATE INDEX IF NOT EXISTS review_comments_open_idx
    ON review_comments(artifact_id, created_at)
    WHERE NOT resolved;

CREATE TABLE IF NOT EXISTS review_comment_events (
    id text PRIMARY KEY,
    thread_id text NOT NULL REFERENCES review_comments(id) ON DELETE CASCADE,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    type text NOT NULL CHECK (
        type IN ('create', 'edit', 'reply', 'resolve', 'reopen', 'author_addressed')
    ),
    body text NOT NULL DEFAULT '' CHECK (length(body) <= 10000),
    actor_user_id text REFERENCES market_users(id) ON DELETE SET NULL,
    actor_nickname text NOT NULL DEFAULT '',
    actor_role text NOT NULL CHECK (
        actor_role IN ('author', 'admin', 'core_admin', 'system')
    ),
    expected_version integer NOT NULL CHECK (expected_version >= 0),
    resulting_version integer NOT NULL CHECK (resulting_version >= 1),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(metadata) = 'object'
    ),
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (type NOT IN ('create', 'edit', 'reply') OR length(body) > 0)
);

CREATE INDEX IF NOT EXISTS review_comment_events_thread_idx
    ON review_comment_events(thread_id, created_at);
CREATE INDEX IF NOT EXISTS review_comment_events_artifact_idx
    ON review_comment_events(artifact_id, created_at DESC);

ALTER TABLE review_decisions
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'admin';
ALTER TABLE review_decisions
    ADD COLUMN IF NOT EXISTS policy_version_id text
        REFERENCES review_policies(id) ON DELETE RESTRICT;
ALTER TABLE review_decisions
    ADD COLUMN IF NOT EXISTS input_run_ids text[] NOT NULL DEFAULT ARRAY[]::text[];
ALTER TABLE review_decisions
    ADD COLUMN IF NOT EXISTS input_fingerprints text[] NOT NULL DEFAULT ARRAY[]::text[];
ALTER TABLE review_decisions
    ADD COLUMN IF NOT EXISTS coverage_sha256 text NOT NULL DEFAULT '';
ALTER TABLE review_decisions
    ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

UPDATE review_decisions
   SET source = 'system'
 WHERE action IN ('auto_reject', 'retry_publish');

ALTER TABLE review_decisions
    DROP CONSTRAINT IF EXISTS review_decisions_action_check;
ALTER TABLE review_decisions
    ADD CONSTRAINT review_decisions_action_check
    CHECK (
        action IN (
            'auto_reject', 'auto_approve', 'approve', 'reject',
            'request_changes', 'retry_publish', 'revoke', 'emergency_override'
        )
    );
ALTER TABLE review_decisions
    DROP CONSTRAINT IF EXISTS review_decisions_source_check;
ALTER TABLE review_decisions
    ADD CONSTRAINT review_decisions_source_check
    CHECK (source IN ('admin', 'system', 'policy'));
ALTER TABLE review_decisions
    DROP CONSTRAINT IF EXISTS review_decisions_coverage_sha256_check;
ALTER TABLE review_decisions
    ADD CONSTRAINT review_decisions_coverage_sha256_check
    CHECK (coverage_sha256 = '' OR length(coverage_sha256) = 64);
ALTER TABLE review_decisions
    DROP CONSTRAINT IF EXISTS review_decisions_metadata_object_check;
ALTER TABLE review_decisions
    ADD CONSTRAINT review_decisions_metadata_object_check
    CHECK (jsonb_typeof(metadata) = 'object');
ALTER TABLE review_decisions
    DROP CONSTRAINT IF EXISTS review_decisions_request_changes_reason_check;
ALTER TABLE review_decisions
    ADD CONSTRAINT review_decisions_request_changes_reason_check
    CHECK (action <> 'request_changes' OR length(btrim(reason)) > 0);

CREATE INDEX IF NOT EXISTS review_decisions_policy_idx
    ON review_decisions(policy_version_id, created_at DESC)
    WHERE policy_version_id IS NOT NULL;

ALTER TABLE artifact_jobs
    ADD COLUMN IF NOT EXISTS policy_version_id text
        REFERENCES review_policies(id) ON DELETE RESTRICT;
ALTER TABLE artifact_jobs
    ADD COLUMN IF NOT EXISTS run_id text REFERENCES review_runs(id) ON DELETE SET NULL;
ALTER TABLE artifact_jobs
    ADD COLUMN IF NOT EXISTS stage_name text NOT NULL DEFAULT '';

ALTER TABLE artifact_jobs
    DROP CONSTRAINT IF EXISTS artifact_jobs_type_check;
ALTER TABLE artifact_jobs
    ADD CONSTRAINT artifact_jobs_type_check
    CHECK (
        type IN (
            'precheck', 'static_scan', 'publish', 'revoke', 'outbox',
            'cleanup_orphan', 'diff_graph', 'clamav_scan', 'yara_scan',
            'runtime_dispatch', 'runtime_collect', 'dependency_scan',
            'category', 'llm_package', 'llm_file', 'llm_summary',
            'route_review'
        )
    );

CREATE INDEX IF NOT EXISTS artifact_jobs_policy_idx
    ON artifact_jobs(policy_version_id, status, available_at)
    WHERE policy_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS artifact_jobs_run_idx
    ON artifact_jobs(run_id, created_at DESC)
    WHERE run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS artifact_sboms (
    id text PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES plugin_artifacts(id) ON DELETE CASCADE,
    run_id text NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    format text NOT NULL CHECK (
        format IN ('cyclonedx-json', 'spdx-json', 'pip-report')
    ),
    document_sha256 text NOT NULL CHECK (length(document_sha256) = 64),
    object_key text NOT NULL UNIQUE,
    package_count integer NOT NULL DEFAULT 0 CHECK (package_count >= 0),
    generator text NOT NULL,
    tool_version text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (artifact_id, run_id, format, document_sha256)
);

CREATE INDEX IF NOT EXISTS artifact_sboms_artifact_idx
    ON artifact_sboms(artifact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS artifact_sboms_run_idx
    ON artifact_sboms(run_id, created_at DESC);
