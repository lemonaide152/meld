-- Pilot instrumentation only (pilot.py). Aggregate daily counts. No codes, notes, IPs.
CREATE TABLE IF NOT EXISTS funnel_events (
  day   TEXT NOT NULL,
  event TEXT NOT NULL,
  n     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, event)
);
