-- x402 USDC-on-Base payments. Apply to prod D1 before relying on settlement.
-- The worker also runs CREATE TABLE IF NOT EXISTS on the first claim.
-- Do not add a payTo column. The payee is the X402_PAY_TO Worker secret.

CREATE TABLE IF NOT EXISTS x402_payments (
  tx_hash TEXT PRIMARY KEY,
  nonce TEXT NOT NULL UNIQUE,
  meld_code TEXT NOT NULL,
  amount_atomic TEXT NOT NULL,
  payer TEXT,
  network TEXT NOT NULL,
  paid_at TEXT NOT NULL
);
