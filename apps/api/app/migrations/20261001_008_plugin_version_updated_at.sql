-- Track the last time repo_version actually changed so update-time sorting ignores metadata-only syncs.
ALTER TABLE market_plugins ADD COLUMN IF NOT EXISTS version_updated_at timestamptz;

UPDATE market_plugins
   SET version_updated_at = COALESCE(updated_at, created_at, now())
 WHERE version_updated_at IS NULL;

ALTER TABLE market_plugins ALTER COLUMN version_updated_at SET NOT NULL;
ALTER TABLE market_plugins ALTER COLUMN version_updated_at SET DEFAULT now();
