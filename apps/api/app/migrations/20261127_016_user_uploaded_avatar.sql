ALTER TABLE market_users
    ADD COLUMN IF NOT EXISTS uploaded_avatar_url text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS uploaded_avatar_profile_id text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS uploaded_avatar_object_id text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS uploaded_avatar_sha256 text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS uploaded_avatar_updated_at timestamptz;
