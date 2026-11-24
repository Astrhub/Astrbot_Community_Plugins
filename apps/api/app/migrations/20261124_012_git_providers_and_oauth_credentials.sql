-- Multi-platform OAuth credential store, Git repository bindings, normalized plugin repo uniqueness,
-- and artifact source_type='git' support for known OAuth Git providers.

ALTER TABLE market_oauth_identities
    ADD COLUMN IF NOT EXISTS rotation_status text NOT NULL DEFAULT '';
ALTER TABLE market_oauth_identities
    ADD COLUMN IF NOT EXISTS token_refreshed_at timestamptz;

CREATE TABLE IF NOT EXISTS market_oauth_credentials (
    credential_ref text PRIMARY KEY,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
    provider text NOT NULL,
    ciphertext text NOT NULL,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS market_oauth_credentials_user_provider_idx
    ON market_oauth_credentials (user_id, lower(provider));

CREATE TABLE IF NOT EXISTS market_git_repository_bindings (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES market_users(id) ON DELETE RESTRICT,
    normalized_repo text NOT NULL,
    repo_url text NOT NULL,
    provider text NOT NULL,
    instance_url text NOT NULL DEFAULT '',
    repo_external_id text NOT NULL DEFAULT '',
    owner_id text NOT NULL DEFAULT '',
    permission_level text NOT NULL DEFAULT '',
    verification_method text NOT NULL DEFAULT 'oauth'
        CHECK (verification_method IN ('oauth')),
    credential_ref text NOT NULL DEFAULT '',
    binding_generation integer NOT NULL DEFAULT 1,
    verified_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    revoked_by text REFERENCES market_users(id) ON DELETE SET NULL,
    revoke_reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS market_git_repository_bindings_active_repo_idx
    ON market_git_repository_bindings (lower(normalized_repo))
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS market_git_repository_bindings_user_idx
    ON market_git_repository_bindings (user_id)
    WHERE revoked_at IS NULL;

ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS repo_provider text NOT NULL DEFAULT '';
ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS repo_external_id text NOT NULL DEFAULT '';
ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS normalized_repo text NOT NULL DEFAULT '';

UPDATE market_plugins
   SET repo_provider = CASE
           WHEN lower(btrim(repo)) LIKE 'https://github.com/%'
                OR lower(btrim(repo)) LIKE 'https://www.github.com/%' THEN 'github'
           WHEN lower(btrim(repo)) LIKE 'https://gitlab.com/%'
                OR lower(btrim(repo)) LIKE 'https://www.gitlab.com/%' THEN 'gitlab'
           WHEN lower(btrim(repo)) LIKE 'https://gitee.com/%'
                OR lower(btrim(repo)) LIKE 'https://www.gitee.com/%' THEN 'gitee'
           WHEN lower(btrim(repo)) LIKE 'https://atomgit.com/%'
                OR lower(btrim(repo)) LIKE 'https://www.atomgit.com/%'
                OR lower(btrim(repo)) LIKE 'https://gitcode.com/%'
                OR lower(btrim(repo)) LIKE 'https://www.gitcode.com/%' THEN 'atomgit'
           WHEN lower(btrim(repo)) LIKE 'https://gitea.com/%'
                OR lower(btrim(repo)) LIKE 'https://www.gitea.com/%' THEN 'gitea'
           ELSE repo_provider
       END,
       normalized_repo = CASE
           WHEN btrim(normalized_repo) <> '' THEN lower(btrim(normalized_repo))
           ELSE lower(
               regexp_replace(
                   regexp_replace(
                       regexp_replace(
                           regexp_replace(btrim(repo), '/+$', ''),
                           '\.git$',
                           '',
                           'i'
                       ),
                       '^https://www\.',
                       'https://',
                       'i'
                   ),
                   '^https://gitcode\.com/',
                   'https://atomgit.com/',
                   'i'
               )
           )
       END
 WHERE btrim(repo) <> ''
   AND (btrim(repo_provider) = '' OR btrim(normalized_repo) = '');

CREATE UNIQUE INDEX IF NOT EXISTS market_plugins_normalized_repo_key
    ON market_plugins (lower(normalized_repo))
    WHERE btrim(normalized_repo) <> '';

DO $$
DECLARE
    constraint_record record;
BEGIN
    IF to_regclass('plugin_artifacts') IS NOT NULL THEN
        FOR constraint_record IN
            SELECT conname
              FROM pg_constraint
             WHERE conrelid = 'plugin_artifacts'::regclass
               AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%source_type%'
        LOOP
            EXECUTE format('ALTER TABLE plugin_artifacts DROP CONSTRAINT %I', constraint_record.conname);
        END LOOP;
        ALTER TABLE plugin_artifacts
            ADD CONSTRAINT plugin_artifacts_source_type_check
            CHECK (source_type IN ('upload', 'github', 'git'));
    END IF;
END $$;
