CREATE TABLE IF NOT EXISTS review_worker_heartbeats (
    worker_kind text NOT NULL CHECK (worker_kind IN ('artifact_worker', 'runtime_runner')),
    worker_id text NOT NULL CHECK (worker_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'),
    components jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(components) = 'object'
    ),
    capacity integer NOT NULL DEFAULT 0 CHECK (capacity >= 0 AND capacity <= 1024),
    active_count integer NOT NULL DEFAULT 0 CHECK (
        active_count >= 0 AND active_count <= capacity
    ),
    observed_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (worker_kind, worker_id),
    CHECK (expires_at > observed_at)
);

CREATE INDEX IF NOT EXISTS review_worker_heartbeats_fresh_idx
    ON review_worker_heartbeats(worker_kind, expires_at DESC, observed_at DESC);
