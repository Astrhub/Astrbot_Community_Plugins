ALTER TABLE market_plugins
    ADD COLUMN IF NOT EXISTS download_count bigint NOT NULL DEFAULT 0
    CHECK (download_count >= 0);

CREATE TABLE IF NOT EXISTS market_plugin_download_windows (
    plugin_id text NOT NULL REFERENCES market_plugins(id) ON DELETE CASCADE,
    requester_hash text NOT NULL,
    window_started_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plugin_id, requester_hash)
);

UPDATE plugin_artifacts a
   SET download_url = NULL
 WHERE a.download_url IS NOT NULL
   AND EXISTS (
       SELECT 1
         FROM market_storage_objects o
        WHERE o.purpose = 'artifact-published'
          AND o.status = 'active'
          AND (
              (o.owner_type = 'artifact' AND o.owner_id = a.id)
              OR o.object_key = a.published_key
          )
   );
