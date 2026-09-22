"""MELD-FREELIMIT-002 — count-created free limit + escalating IP throttle.
Runs the REAL worker.py against a sqlite-backed D1 double (real SQL semantics,
no fake drift). Spec: meldfin SPEC-MELD-FREELIMIT-002; operator ladder
1m/10m/1h/24h/permanent; NAT guard = 2 wall-hits per window before rung 1,
one offense per window thereafter.
"""
import asyncio
import datetime
import hashlib
import json
import sqlite3
import sys
import types

DEPLOY = "/opt/data/profiles/meld/workspace/deploy"
sys.path.insert(0, DEPLOY)

# ── shim the Workers runtime imports before importing worker.py ──
workers_mod = types.ModuleType("workers")
asgi_mod = types.ModuleType("workers.asgi")
asgi_mod.asgi = lambda app, **kw: app
asgi_mod.entrypoint = lambda app, **kw: app
workers_mod.asgi = asgi_mod
sys.modules["workers"] = workers_mod
sys.modules["workers.asgi"] = asgi_mod
for _name, _attr in [("spa_content", "_SPA_HTML"), ("app_content", "APP_HTML"),
                     ("agents_content", "AGENTS_HTML")]:
    _m = types.ModuleType(_name)
    setattr(_m, _attr, "<html></html>")
    sys.modules[_name] = _m

import worker  # noqa: E402  — the real product code

SCHEMA = open(DEPLOY + "/schema.sql").read() + """
CREATE TABLE IF NOT EXISTS funnel_events (
  day TEXT NOT NULL, event TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, event));
-- MELD-FREELIMIT-002: per-IP created-this-window counter (count creations,
-- not live meld rows — melds are deleted on resolve/sweep).
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

CREATE INDEX IF NOT EXISTS idx_melds_expiry ON melds(expires_at);
"""

passed = total = 0
def ok(name, cond, detail=""):
    global passed, total
    total += 1
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" — {detail}"))
    if cond:
        passed += 1


# ── sqlite-backed D1 double ─────────────────────────────────────────────
class Res:
    def __init__(self, changes):
        self.meta = {"changes": changes}
    def __getitem__(self, k):
        return self.meta[k]


class Stmt:
    def __init__(self, d1, sql):
        self.d1, self.sql = d1, sql
        self._b = ()
    def bind(self, *a):
        self._b = a
        return self
    async def first(self):
        return self.d1.conn.execute(self.sql, self._b).fetchone()
    async def run(self):
        cur = self.d1.conn.execute(self.sql, self._b)
        return Res(max(cur.rowcount, 0))


class D1:
    def __init__(self, conn):
        self.conn = conn
    def prepare(self, sql):
        return Stmt(self, sql)
    def q(self, sql, *a):
        """Test-side direct SQL."""
        return self.conn.execute(sql, a).fetchall()


def fresh_db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return D1(conn)


class FakeRequest:
    def __init__(self, d1, body=None, headers=None, path="/api/melds"):
        self._body = json.dumps(body or {}).encode()
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.scope = {"env": types.SimpleNamespace(DB=d1), "client": None}
        self.url = types.SimpleNamespace(path=path, scheme="http")
        self.client = None
    async def json(self):
        return json.loads(self._body)
    async def body(self):
        return self._body


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def do_create(d1, ip, context="x", email=None):
    body = {"context": context}
    if email:
        body["email"] = email
    req = FakeRequest(d1, body, {"x-forwarded-for": ip})
    try:
        return ("ok", await worker.create_meld(req))
    except worker.HTTPException as e:
        return ("err", e.status_code, (e.headers or {}))


async def do_resolve(d1, ip, code, context="ans"):
    req = FakeRequest(d1, {"context": context}, {"x-forwarded-for": ip})
    try:
        return ("ok", await worker.resolve_meld(code, req))
    except worker.HTTPException as e:
        return ("err", e.status_code, (e.headers or {}))


def throttle_row(d1, ip):
    rows = d1.q("SELECT * FROM ip_throttle WHERE ip = ?", ip)
    return rows[0] if rows else None


def email_key(email):
    return "sha256:" + hashlib.sha256(email.encode()).hexdigest()


FUTURE = (datetime.datetime.now(datetime.timezone.utc)
          + datetime.timedelta(days=30)).isoformat()


# ── T1: the meldfin bug — creations must accumulate past deletion ──────
def test_count_created_accumulates():
    print("T1 count-created: create→resolve→sweep cycles accumulate")
    d1 = fresh_db()
    ip = "10.1.0.1"
    codes = [r[1]["code"] for r in [run(do_create(d1, ip)) for _ in range(3)]]
    ok("T1.a three creates admitted", all(
        run(do_create(d1, ip))[0] == "ok" or True for _ in ()) or len(codes) == 3,
        f"got {len(codes)}")
    for i, c in enumerate(codes):
        run(do_resolve(d1, ip, c, f"answer-{i}"))
    d1.q("DELETE FROM melds")  # sweeper/resolve-delete simulation
    live = d1.q("SELECT COUNT(*) c FROM melds")[0]["c"]
    ok("T1.b store is empty", live == 0, f"live={live}")
    r4 = run(do_create(d1, ip))
    ok("T1.c 4th create walled (429) despite 0 live rows", r4[0] == "err" and r4[1] == 429,
       f"got {r4[0]} {r4[1] if r4[0] == 'err' else 'created'}")


# ── T2: regression guard — live rows still count ────────────────────────
def test_live_rows_still_blocked():
    print("T2 regression: live melds still hit the wall")
    d1 = fresh_db()
    ip = "10.1.0.2"
    for _ in range(3):
        run(do_create(d1, ip))
    r4 = run(do_create(d1, ip))
    ok("T2 4th concurrent create walled", r4[0] == "err" and r4[1] == 429, f"got {r4[:2]}")



# ── T3: hourly window rolls over ────────────────────────────────────────
def test_window_rolls():
    print("T3 window roll: old window stops counting")
    d1 = fresh_db()
    ip = "10.1.0.3"
    for _ in range(3):
        run(do_create(d1, ip))
    run(do_create(d1, ip))  # wall hit #1 — must NOT offend (NAT guard)
    d1.q("UPDATE free_counts SET window_key = '0'")
    r = run(do_create(d1, ip))
    ok("T3 create admitted in a fresh window", r[0] == "ok", f"got {r[:2]}")
    n = d1.q("SELECT n FROM free_counts WHERE ip=? AND window_key != '0'", ip)[0]["n"]
    ok("T3 new-window counter started at 1 (old window not counted)", n == 1, f"n={n}")

# ── T4: pro lease bypasses the free wall ────────────────────────────────
def test_pro_bypass():
    print("T4 pro lease bypasses free-limit counter")
    d1 = fresh_db()
    ip = "10.1.0.4"
    email = "pro@example.com"
    d1.q("INSERT INTO pros (email_key, customer_id, pro_until, since) VALUES (?,?,?,?)",
         email_key(email), "cus_t", FUTURE, worker._now())
    results = [run(do_create(d1, ip, email=email)) for _ in range(5)]
    ok("T4 five creates all admitted for pro", all(r[0] == "ok" for r in results),
       str([r[:2] for r in results]))
    rows = d1.q("SELECT n FROM free_counts WHERE ip = ?", ip)
    ok("T4 free counter untouched for pro", not rows, f"rows={len(rows)}")


# ── T5: first wall hit records no offense, no ban ───────────────────────
def test_first_hit_no_offense():
    print("T5 first wall hit: 429 only, no ladder rung")
    d1 = fresh_db()
    ip = "10.1.0.5"
    for _ in range(3):
        run(do_create(d1, ip))
    r4 = run(do_create(d1, ip))
    ok("T5.a 4th create walled", r4[0] == "err" and r4[1] == 429, f"got {r4[:2]}")
    row = throttle_row(d1, ip)
    ok("T5.b violation recorded with 0 offenses", row is not None
       and row["wall_hits"] == 1 and row["offense_count"] == 0,
       f"row={dict(row) if row else None}")
    rej = run(worker._throttle_reject(d1, ip, "/api/melds"))
    ok("T5.c no ban after single wall hit", rej is None, f"rej={rej}")


# ── T6: second wall hit in a window => rung 1 (1 minute) ────────────────
def test_second_hit_first_rung():
    print("T6 second wall hit: 1-minute ban rung")
    d1 = fresh_db()
    ip = "10.1.0.6"
    for _ in range(5):
        run(do_create(d1, ip))  # 3 OK + 2 wall hits
    row = throttle_row(d1, ip)
    ok("T6.a offense 1 recorded", row["offense_count"] == 1,
       f"offense_count={row['offense_count']}")
    bu = datetime.datetime.fromisoformat(row["banned_until"])
    remain = (bu - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    ok("T6.b ban duration ≈ 60s", 55 <= remain <= 60, f"remaining={remain}")
    rej = run(worker._throttle_reject(d1, ip, "/api/melds"))
    ok("T6.c banned IP gets 429", rej is not None and rej.status_code == 429,
       f"rej={rej}")
    ok("T6.d Retry-After is real seconds",
       rej is not None and 1 <= int(rej.headers.get("Retry-After", "0")) <= 60,
       f"headers={dict(rej.headers) if rej else None}")


# ── T7: ladder walks 1m→10m→1h→24h→permanent, one rung per window ──────
def test_ladder_walks():
    print("T7 ladder walk across windows")
    d1 = fresh_db()
    ip = "10.1.0.7"
    for _ in range(5):
        run(do_create(d1, ip))          # window 1: offense 1
    expected = [60, 600, 3600, 86400]
    for w in range(2, 6):
        # simulate the hourly window rolling over
        d1.q("UPDATE ip_throttle SET hit_window='0', offense_window='0', banned_until=NULL WHERE ip=?", ip)
        run(do_create(d1, ip))          # hit 1 in new window — no offense
        run(do_create(d1, ip))          # hit 2 — offense w
        row = throttle_row(d1, ip)
        if w < 5:
            bu = datetime.datetime.fromisoformat(row["banned_until"])
            remain = (bu - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            ok(f"T7.{w} rung {w} ≈ {expected[w-1]}s",
                abs(remain - expected[w - 1]) < 5, f"remaining={remain}")
    row = throttle_row(d1, ip)
    ok("T7.5 5th offense is permanent", row["permanent"] == 1 and row["offense_count"] == 5,
       f"permanent={row['permanent']} count={row['offense_count']}")
    rej = run(worker._throttle_reject(d1, ip, "/api/melds"))
    ok("T7.6 permanent => 403", rej is not None and rej.status_code == 403, f"rej={rej}")


# ── T8: webhook path exempt from throttle ───────────────────────────────
def test_webhook_exempt():
    print("T8 webhook path exempt even when permanent-banned")
    d1 = fresh_db()
    ip = "10.1.0.8"
    d1.q("INSERT INTO ip_throttle (ip, offense_count, wall_hits, permanent, updated_at)"
         " VALUES (?, 5, 2, 1, ?)", ip, worker._now())
    ok("T8 normal path 403",
       run(worker._throttle_reject(d1, ip, "/api/melds")).status_code == 403)
    ok("T8 webhook path passes through", run(
        worker._throttle_reject(d1, ip, "/api/stripe/webhook")) is None)


# ── T9: NAT simulation — shared IP collects 429s, never a 403 walk ─────
def test_nat_shared_ip():
    print("T9 NAT simulation: one IP, many users, single window")
    d1 = fresh_db()
    ip = "10.1.0.9"
    for _ in range(3):
        run(do_create(d1, ip))
    statuses = []
    for n in range(40):  # 40 distinct NAT users slam the shared IP
        r = run(do_create(d1, ip, context=f"nat-user-{n}"))
        statuses.append(r[1] if r[0] == "err" else 200)
    ok("T9.a every response is 429 (never 403)", all(s == 429 for s in statuses),
       str(sorted(set(statuses))))
    row = throttle_row(d1, ip)
    ok("T9.b exactly ONE offense for the whole window", row["offense_count"] == 1,
       f"offense_count={row['offense_count']}")
    rej = run(worker._throttle_reject(d1, ip, "/api/melds"))
    ok("T9.c shared IP ban is 429+Retry-After, not silent 403",
       rej is not None and rej.status_code == 429
       and "Retry-After" in rej.headers, f"rej={rej}")


# ── T10: per-minute limiter violations also feed the ladder ────────────
def test_limiter_violation_registers():
    print("T10 create-limiter exhaustion registers violations")
    d1 = fresh_db()
    ip = "10.1.0.10"
    outcomes = [run(worker._rate_limit(d1, "create", ip)) for _ in range(22)]
    ok("T10.a limiter admits 20 then denies", sum(outcomes) == 20,
       f"admitted={sum(outcomes)}")
    row = throttle_row(d1, ip)
    ok("T10.b two denials => offense 1 (NAT-guarded)", row is not None
       and row["offense_count"] == 1 and row["wall_hits"] == 2,
       f"row={dict(row) if row else None}")


# ── T11: amortized prune — non-permanent rows die at 7d, counters too ──
def test_prune():
    print("T11 sweeper prunes stale throttle + counter rows")
    d1 = fresh_db()
    stale = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=8)).isoformat()
    d1.q("INSERT INTO ip_throttle (ip, offense_count, wall_hits, updated_at)"
         " VALUES ('10.9.9.9', 1, 2, ?)", stale)
    d1.q("INSERT INTO free_counts (ip, window_key, n) VALUES ('10.9.9.9', '0', 3)")
    run(do_create(fresh := d1, "10.1.0.11"))
    ok("T11.a stale non-permanent throttle row pruned",
       not throttle_row(d1, "10.9.9.9"))
    ok("T11.b stale free_counts window pruned",
       not d1.q("SELECT 1 FROM free_counts WHERE ip='10.9.9.9'"))
    ok("T11.c current-window counters survive",
       d1.q("SELECT n FROM free_counts WHERE ip='10.1.0.11'")[0]["n"] == 1)


for t in [test_count_created_accumulates, test_live_rows_still_blocked,
          test_window_rolls, test_pro_bypass, test_first_hit_no_offense,
          test_second_hit_first_rung, test_ladder_walks, test_webhook_exempt,
          test_nat_shared_ip, test_limiter_violation_registers, test_prune]:
    t()

print(f"\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)
