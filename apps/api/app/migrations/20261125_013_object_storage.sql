-- Generic multi-profile object storage metadata, purpose bindings, object registry,
-- public URL aliases, migration queue/items, and unified PostgreSQL blob chunk tables.

CREATE TABLE IF NOT EXISTS market_storage_secrets (
    secret_ref text PRIMARY KEY,
    profile_id text NOT NULL,
    ciphertext text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS market_storage_secrets_profile_idx
    ON market_storage_secrets (profile_id);

CREATE TABLE IF NOT EXISTS market_storage_profiles (
    id text PRIMARY KEY,
    name text NOT NULL,
    backend text NOT NULL CHECK (backend IN ('local', 's3', 'webdav', 'imgbed')),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'draining', 'retired', 'removed')),
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    secret_ref text NOT NULL DEFAULT '',
    legacy boolean NOT NULL DEFAULT false,
    health_status text NOT NULL DEFAULT 'unknown',
    health_message text NOT NULL DEFAULT '',
    last_checked_at timestamptz,
    created_by text NOT NULL DEFAULT '',
    updated_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market_storage_bindings (
    purpose text PRIMARY KEY
        CHECK (purpose IN ('artifact-quarantine', 'artifact-published', 'avatar')),
    profile_id text NOT NULL REFERENCES market_storage_profiles(id) ON DELETE RESTRICT,
    generation integer NOT NULL DEFAULT 1 CHECK (generation >= 1),
    updated_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market_storage_objects (
    id text PRIMARY KEY,
    purpose text NOT NULL
        CHECK (purpose IN ('artifact-quarantine', 'artifact-published', 'avatar')),
    profile_id text NOT NULL REFERENCES market_storage_profiles(id) ON DELETE RESTRICT,
    backend text NOT NULL,
    namespace text NOT NULL DEFAULT '',
    object_key text NOT NULL,
    size_bytes bigint NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    sha256 text NOT NULL DEFAULT '',
    content_type text NOT NULL DEFAULT 'application/octet-stream',
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'deleted')),
    public_url text NOT NULL DEFAULT '',
    owner_type text NOT NULL DEFAULT '',
    owner_id text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS market_storage_objects_active_locator_idx
    ON market_storage_objects (profile_id, namespace, object_key)
    WHERE status <> 'deleted';

CREATE INDEX IF NOT EXISTS market_storage_objects_purpose_profile_idx
    ON market_storage_objects (purpose, profile_id, status);

CREATE TABLE IF NOT EXISTS market_storage_url_aliases (
    id text PRIMARY KEY,
    alias_path text NOT NULL UNIQUE,
    object_id text NOT NULL REFERENCES market_storage_objects(id) ON DELETE CASCADE,
    purpose text NOT NULL CHECK (purpose IN ('artifact-published', 'avatar')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS market_storage_url_aliases_object_idx
    ON market_storage_url_aliases (object_id);

CREATE TABLE IF NOT EXISTS market_storage_migrations (
    id text PRIMARY KEY,
    purpose text NOT NULL
        CHECK (purpose IN ('artifact-quarantine', 'artifact-published', 'avatar')),
    source_profile_id text NOT NULL REFERENCES market_storage_profiles(id) ON DELETE RESTRICT,
    target_profile_id text NOT NULL REFERENCES market_storage_profiles(id) ON DELETE RESTRICT,
    binding_generation integer NOT NULL DEFAULT 1 CHECK (binding_generation >= 1),
    status text NOT NULL DEFAULT 'queued'
        CHECK (
            status IN (
                'queued', 'running', 'paused', 'completed',
                'completed_with_errors', 'failed', 'cancelled'
            )
        ),
    total_count integer NOT NULL DEFAULT 0 CHECK (total_count >= 0),
    completed_count integer NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
    failed_count integer NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    skipped_count integer NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    unknown_objects jsonb NOT NULL DEFAULT '[]'::jsonb,
    last_error text NOT NULL DEFAULT '',
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS market_storage_migrations_status_idx
    ON market_storage_migrations (status, created_at ASC);

CREATE TABLE IF NOT EXISTS market_storage_migration_items (
    id text PRIMARY KEY,
    migration_id text NOT NULL REFERENCES market_storage_migrations(id) ON DELETE CASCADE,
    object_id text NOT NULL REFERENCES market_storage_objects(id) ON DELETE CASCADE,
    source_profile_id text NOT NULL,
    source_namespace text NOT NULL DEFAULT '',
    source_key text NOT NULL,
    source_public_url text NOT NULL DEFAULT '',
    target_profile_id text NOT NULL,
    target_namespace text NOT NULL DEFAULT '',
    target_key text NOT NULL,
    target_public_url text NOT NULL DEFAULT '',
    expected_sha256 text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS market_storage_migration_items_migration_status_idx
    ON market_storage_migration_items (migration_id, status, created_at ASC);

CREATE TABLE IF NOT EXISTS market_storage_blob_objects (
    id text PRIMARY KEY,
    profile_id text NOT NULL,
    namespace text NOT NULL DEFAULT '',
    object_key text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    content_type text NOT NULL DEFAULT 'application/octet-stream',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    chunk_size integer NOT NULL CHECK (chunk_size > 0),
    chunk_count integer NOT NULL CHECK (chunk_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (profile_id, namespace, object_key)
);

CREATE TABLE IF NOT EXISTS market_storage_blob_chunks (
    object_id text NOT NULL REFERENCES market_storage_blob_objects(id) ON DELETE CASCADE,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    data bytea NOT NULL,
    size_bytes integer NOT NULL CHECK (size_bytes >= 0),
    PRIMARY KEY (object_id, chunk_index)
);
