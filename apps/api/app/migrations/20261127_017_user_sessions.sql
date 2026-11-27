-- Persist user sessions with hashed tokens, trusted-device flags, and device metadata.

CREATE TABLE IF NOT EXISTS market_user_sessions (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    trusted boolean NOT NULL DEFAULT false,
    session_version integer NOT NULL DEFAULT 1,
    device_label text NOT NULL DEFAULT '',
    user_agent text NOT NULL DEFAULT '',
    ip_address text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS market_user_sessions_user_active_idx
    ON market_user_sessions (user_id, last_seen_at DESC)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS market_user_sessions_expires_idx
    ON market_user_sessions (expires_at)
    WHERE revoked_at IS NULL;
