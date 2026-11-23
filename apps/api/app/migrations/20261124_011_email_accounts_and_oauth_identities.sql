-- Add email accounts, unified public usernames, session revocation versions,
-- generic multi-OAuth identity bindings, and plugin owner_username snapshots.

ALTER TABLE market_users
    ADD COLUMN IF NOT EXISTS email text NOT NULL DEFAULT '';
ALTER TABLE market_users
    ADD COLUMN IF NOT EXISTS email_verified_at timestamptz;
ALTER TABLE market_users
    ADD COLUMN IF NOT EXISTS public_username text NOT NULL DEFAULT '';
ALTER TABLE market_users
    ADD COLUMN IF NOT EXISTS session_version integer NOT NULL DEFAULT 1;

DO $$
DECLARE
    constraint_record record;
BEGIN
    FOR constraint_record IN
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'market_users'::regclass
           AND contype = 'c'
           AND pg_get_constraintdef(oid) ILIKE '%auth_source%'
    LOOP
        EXECUTE format('ALTER TABLE market_users DROP CONSTRAINT %I', constraint_record.conname);
    END LOOP;
END $$;

ALTER TABLE market_users
    ADD CONSTRAINT market_users_auth_source_check
    CHECK (auth_source IN ('github', 'internal', 'email', 'local', 'oauth'));

WITH candidates AS (
    SELECT
        u.id,
        COALESCE(
            NULLIF(btrim(u.public_username), ''),
            NULLIF(btrim(u.github_login), ''),
            NULLIF(btrim(u.internal_username), ''),
            u.id
        ) AS base_name,
        EXISTS (
            SELECT 1
              FROM market_plugins p
             WHERE p.owner_user_id = u.id
               AND p.status = 'listed'
        ) AS has_listed_plugin,
        EXISTS (
            SELECT 1
              FROM market_plugins p
             WHERE p.owner_user_id = u.id
        ) AS has_any_plugin,
        (btrim(u.github_login) <> '') AS has_github_login,
        u.created_at
      FROM market_users u
),
ranked AS (
    SELECT
        id,
        base_name,
        row_number() OVER (
            PARTITION BY lower(base_name)
            ORDER BY
                has_listed_plugin DESC,
                has_any_plugin DESC,
                has_github_login DESC,
                created_at ASC,
                id ASC
        ) AS rn
      FROM candidates
)
UPDATE market_users u
   SET public_username = CASE
           WHEN r.rn = 1 THEN r.base_name
           ELSE r.base_name || '_' || substr(md5(u.id), 1, 6)
       END
  FROM ranked r
 WHERE u.id = r.id
   AND btrim(u.public_username) = '';

CREATE UNIQUE INDEX IF NOT EXISTS market_users_email_lower_idx
    ON market_users (lower(btrim(email)))
    WHERE btrim(email) <> '';

CREATE UNIQUE INDEX IF NOT EXISTS market_users_public_username_lower_idx
    ON market_users (lower(btrim(public_username)))
    WHERE btrim(public_username) <> '';

CREATE TABLE IF NOT EXISTS market_oauth_identities (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    provider text NOT NULL,
    instance_url text NOT NULL DEFAULT '',
    provider_user_id text NOT NULL,
    provider_login text NOT NULL DEFAULT '',
    provider_email text NOT NULL DEFAULT '',
    avatar_url text NOT NULL DEFAULT '',
    scopes text NOT NULL DEFAULT '',
    credential_ref text NOT NULL DEFAULT '',
    expires_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS market_oauth_identities_provider_user_active_idx
    ON market_oauth_identities (lower(provider), lower(instance_url), provider_user_id)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS market_oauth_identities_user_active_idx
    ON market_oauth_identities (user_id)
    WHERE revoked_at IS NULL;

INSERT INTO market_oauth_identities (
    id, user_id, provider, instance_url, provider_user_id,
    provider_login, provider_email, avatar_url, scopes
)
SELECT
    'oauth_github_' || u.id,
    u.id,
    'github',
    'https://github.com',
    COALESCE(NULLIF(btrim(u.github_id), ''), u.id),
    u.github_login,
    u.github_email,
    u.avatar_url,
    ''
  FROM market_users u
 WHERE COALESCE(NULLIF(btrim(u.github_id), ''), '') <> ''
    OR btrim(u.github_login) <> ''
ON CONFLICT DO NOTHING;

ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS owner_username text NOT NULL DEFAULT '';

UPDATE market_plugins p
   SET owner_username = COALESCE(
       NULLIF(btrim(p.owner_github_login), ''),
       (SELECT NULLIF(btrim(u.public_username), '') FROM market_users u WHERE u.id = p.owner_user_id),
       ''
   )
 WHERE btrim(p.owner_username) = '';

CREATE UNIQUE INDEX IF NOT EXISTS market_plugins_owner_username_name_key
    ON market_plugins (lower(owner_username), lower(name))
    WHERE owner_username <> '';
