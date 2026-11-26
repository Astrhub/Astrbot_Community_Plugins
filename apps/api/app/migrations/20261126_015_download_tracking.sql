ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS download_count bigint NOT NULL DEFAULT 0
    CHECK (download_count >= 0);

UPDATE plugin_artifacts a
   SET download_url = NULL
 WHERE a.download_url IS NOT NULL
   AND EXISTS (
       SELECT 1
         FROM market_storage_objects o
        WHERE o.owner_type = 'artifact'
          AND o.owner_id = a.id
          AND o.purpose = 'artifact-published'
          AND o.status = 'active'
   );
