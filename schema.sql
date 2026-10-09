-- meld D1 schema. SPEC.md section 4. No owner token, email, or learn column.
-- Timestamps are UTC ISO 8601 text: 2026-10-09T16:48:51.313Z
CREATE TABLE IF NOT EXISTS melds (
  code        TEXT PRIMARY KEY,
  note        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  reply_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_melds_expires ON melds(expires_at);

CREATE TABLE IF NOT EXISTS replies (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  code       TEXT NOT NULL,
  content    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replies_code ON replies(code, id);
