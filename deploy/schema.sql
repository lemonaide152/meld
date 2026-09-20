
CREATE TABLE IF NOT EXISTS melds (
  code TEXT PRIMARY KEY,
  context_a TEXT NOT NULL,
  context_b TEXT,
  resolved INTEGER NOT NULL DEFAULT 0,
  resolved_at TEXT,
  owner_token TEXT NOT NULL,
  owner_email TEXT,
  creator_ip TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  pin TEXT,
  responder_pubkey TEXT,
  signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_melds_ip_created ON melds(creator_ip, created_at);
CREATE INDEX IF NOT EXISTS idx_melds_expiry ON melds(expires_at);

CREATE TABLE IF NOT EXISTS pros (
  email_key TEXT PRIMARY KEY,
  customer_id TEXT,
  pro_until TEXT NOT NULL,
  since TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
  at TEXT NOT NULL,
  event TEXT NOT NULL,
  customer_id TEXT,
  email_key TEXT,
  days INTEGER
);

CREATE TABLE IF NOT EXISTS rate (
  key TEXT PRIMARY KEY,
  count INTEGER NOT NULL,
  bucket INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_bucket ON rate(bucket);
