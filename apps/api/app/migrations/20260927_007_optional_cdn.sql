CREATE TABLE plugin_cdn_subscriptions (
    plugin_id text PRIMARY KEY REFERENCES market_plugins(id) ON DELETE CASCADE,
    "authorization" jsonb NOT NULL DEFAULT '{}'::jsonb,
    generation integer NOT NULL DEFAULT 0,
    requested_version text NOT NULL DEFAULT '',
    artifact_id text REFERENCES plugin_artifacts(id) ON DELETE SET NULL,
    state text NOT NULL DEFAULT 'idle'
        CHECK (state IN ('idle', 'queued', 'running', 'submitted', 'disabled', 'error', 'blocked')),
    lease_id text NOT NULL DEFAULT '',
    lease_until timestamptz,
    available_at timestamptz NOT NULL DEFAULT now(),
    last_error_code text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

UPDATE market_plugins p SET metadata = p.metadata || jsonb_build_object(
    'cdn_enabled', EXISTS (SELECT 1 FROM plugin_artifacts a WHERE a.plugin_id = p.id),
    'cdn_generation', 0
) WHERE NOT (p.metadata ? 'cdn_enabled');

INSERT INTO plugin_cdn_subscriptions (
    plugin_id, "authorization", requested_version, artifact_id, state
)
SELECT p.id,
       CASE WHEN jsonb_typeof(a.submitted_by_snapshot->'repository_authorization') = 'object'
            THEN (a.submitted_by_snapshot->'repository_authorization') - 'auth_reference'
            ELSE '{}'::jsonb END,
       p.repo_version, a.id, 'submitted'
FROM market_plugins p
JOIN LATERAL (
    SELECT * FROM plugin_artifacts a WHERE a.plugin_id = p.id
    ORDER BY a.created_at DESC LIMIT 1
) a ON true
WHERE p.metadata->>'cdn_enabled' = 'true';

CREATE INDEX plugin_cdn_subscriptions_due_idx
    ON plugin_cdn_subscriptions (available_at, lease_until)
    WHERE state IN ('queued', 'running', 'error');
