CREATE TABLE IF NOT EXISTS api_keys (
  key_hash TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  tier TEXT NOT NULL DEFAULT 'agent',
  melds_used INTEGER NOT NULL DEFAULT 0,
  melds_limit INTEGER NOT NULL DEFAULT 10000,
  created_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS meld_usage (
  key_hash TEXT NOT NULL,
  meld_code TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meld_usage_key ON meld_usage(key_hash);
