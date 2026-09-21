
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

-- W2 (MELD-PAY-002): webhook idempotency, keyed on the Stripe event id.
CREATE TABLE IF NOT EXISTS webhook_events (
  stripe_event_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  customer_id TEXT,
  subscription_id TEXT,
  received_at TEXT NOT NULL
);

-- R3 (MELD-PAY-002): opaque per-checkout correlation token minted
-- Worker-side at click time; no user data, no PII.
CREATE TABLE IF NOT EXISTS checkout_clicks (
  checkout_ref TEXT PRIMARY KEY,
  plan TEXT NOT NULL,
  clicked_at TEXT NOT NULL
);
