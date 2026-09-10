-- Preserve stable IDs and every artifact/comment reference while allowing names per owner.
ALTER TABLE market_plugins DROP CONSTRAINT IF EXISTS market_plugins_name_key;
CREATE UNIQUE INDEX market_plugins_owner_name_key
    ON market_plugins (owner_user_id, lower(name));
CREATE UNIQUE INDEX market_plugins_namespace_key
    ON market_plugins (lower(owner_github_login), lower(name))
    WHERE owner_github_login <> '';
