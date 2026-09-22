
CREATE TABLE IF NOT EXISTS melds (
  code TEXT PRIMARY KEY,
  context_a TEXT NOT NULL,
  context_b TEXT,
  resolved INTEGER NOT NULL DEFAULT 0,
  paid INTEGER NOT NULL DEFAULT 0,
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
-- KPI: B→A conversion is measured by resolver_ip creating within 7d.
-- Missing column broke live resolves (SQLITE no-such-column) until ALTERed.
ALTER TABLE melds ADD COLUMN resolver_ip TEXT;

-- MELD-FREELIMIT-002: per-IP created-this-window counter (count creations,
-- not live meld rows — melds are deleted on resolve/sweep, so a live-row
-- COUNT is "3 concurrent", never "3 created/hour", and the paywall never fires).
CREATE TABLE IF NOT EXISTS free_counts (
  ip TEXT PRIMARY KEY,
  window_key TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0
);

-- MELD-FREELIMIT-002: escalating IP throttle (operator ladder
-- 1m → 10m → 1h → 24h → permanent). One offense per window with 2+ wall hits
-- (NAT guard); hit_window = hour-window of the last wall hit.
CREATE TABLE IF NOT EXISTS ip_throttle (
  ip TEXT PRIMARY KEY,
  offense_count INTEGER NOT NULL DEFAULT 0,
  wall_hits INTEGER NOT NULL DEFAULT 0,
  hit_window TEXT,
  offense_window TEXT,
  banned_until TEXT,
  permanent INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

-- No-pro directive (R2): the `pros` table is DROPPED — no reader remains.
-- Migration: DROP TABLE IF EXISTS pros; (run against prod + test D1).

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
-- No-pro (R5): subscription_id is no longer written (kept in schema so old
-- rows still read back).
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

-- MELD-FUNNEL (meldmktg ask, meldsec-endorsed): per-click IP for
-- click→session→grant attribution. Nullable — pre-migration rows keep
-- NULL (no backfill possible; the data never existed). Recorded via
-- _client_ip(): CF-Connecting-IP on Workers, XFF last-hop fallback.
ALTER TABLE checkout_clicks ADD COLUMN clicker_ip TEXT;

-- Pay-per-meld (MELD-PPM-001): one row per paid checkout session.
-- Idempotency on stripe_session_id (spec §Security req 3): a redelivered
-- webhook re-INSERTs to changes==0 → dedup, never a double-unlock.
CREATE TABLE IF NOT EXISTS meld_payments (
  stripe_session_id TEXT PRIMARY KEY,
  meld_code TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  paid_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meld_payments_code ON meld_payments(meld_code);
