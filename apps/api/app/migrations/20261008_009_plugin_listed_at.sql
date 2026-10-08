-- Record when a plugin was actually listed so the listed-time sort reflects listing, not submission.
ALTER TABLE market_plugins ADD COLUMN IF NOT EXISTS listed_at timestamptz;

UPDATE market_plugins
   SET listed_at = COALESCE(updated_at, created_at, now())
 WHERE status = 'listed'
   AND listed_at IS NULL;
