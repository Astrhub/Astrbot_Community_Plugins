ALTER TABLE market_announcements
ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

UPDATE market_announcements
SET updated_at = created_at;
