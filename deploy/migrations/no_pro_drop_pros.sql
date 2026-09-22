-- MIGRATION: no-pro directive (spec v1.2, operator 2026-09-22)
-- R2: drop the `pros` table — no reader remains in worker.py.
-- Run against prod D1 AND the meld-test D1 before/with the next deploy.
-- Safe: all leases are moot; pay-per-meld uses melds.paid + meld_payments.
-- Rollback: none meaningful (no re-grant path exists).

DROP TABLE IF EXISTS pros;

-- Optional hygiene: purge stale pro-lease ledger rows? NO — the ledger is
-- append-only by design (TRUST.md). Historical rows stay.
