-- Enforce global case-insensitive uniqueness of AstrBot plugin_id (author/name).
CREATE UNIQUE INDEX IF NOT EXISTS market_plugins_author_name_key
    ON market_plugins (lower(btrim(author)), lower(btrim(name)))
    WHERE btrim(author) <> '' AND btrim(name) <> '';
