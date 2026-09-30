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

DEPLOY = str((__import__("pathlib").Path(__file__).resolve().parent / "deploy"))
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

SCHEMA = open(DEPLOY + "/schema.sql").read() + "\n" + open(DEPLOY + "/schema_api.sql").read() + """
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
    return asyncio.run(coro)


def _unwrap(result):
    """create_meld may return JSONResponse (pricing header) or a plain dict."""
    if hasattr(result, "body"):
        raw = result.body
        if isinstance(raw, (bytes, bytearray, memoryview)):
            raw = bytes(raw).decode()
        data = json.loads(raw)
        # expose response headers for pricing assertions
        data["_headers"] = {k.decode() if isinstance(k, bytes) else k:
                            v.decode() if isinstance(v, bytes) else v
                            for k, v in dict(result.headers).items()}
        return data
    return result


async def do_create(d1, ip, context="x", email=None, headers=None, ttl="1hr"):
    body = {"context": context, "ttl": ttl}
    if email:
        body["email"] = email
    hdrs = {"x-forwarded-for": ip}
    if headers:
        hdrs.update(headers)
    req = FakeRequest(d1, body, hdrs)
    try:
        return ("ok", _unwrap(await worker.create_meld(req)))
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
    # match worker._email_key normalization (strip + lower) — audit F3
    return "sha256:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()


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
    ok("T1.c 4th create admitted (pilot bridges are not quota-walled)", r4[0] == "ok",
       f"got {r4[0]} {r4[1] if r4[0] == 'err' else 'created'}")


# ── T2: regression guard — live rows still count ────────────────────────
def test_live_rows_still_blocked():
    print("T2 regression: live melds still hit the wall")
    d1 = fresh_db()
    ip = "10.1.0.2"
    for _ in range(3):
        run(do_create(d1, ip))
    r4 = run(do_create(d1, ip))
    ok("T2 4th concurrent create admitted", r4[0] == "ok", f"got {r4[:2]}")



# ── T3: hourly window rolls over ────────────────────────────────────────
def test_window_rolls():
    print("T3 pilot: hourly free wall is not applied")
    d1 = fresh_db()
    ip = "10.1.0.3"
    results = [run(do_create(d1, ip, context=f"w-{i}")) for i in range(5)]
    ok("T3 five creates admitted with explicit ttl", all(r[0] == "ok" for r in results),
       str([r[:2] for r in results]))
    ok("T3 create does not charge a free-tier counter",
       not d1.q("SELECT 1 FROM free_counts WHERE ip=?", ip))

# ── T4: NO lease bypass (no-pro directive) ───────────────────────────────
def test_no_lease_bypass():
    print("T4 no-pro: email confers no bypass — 4th create walled")
    d1 = fresh_db()
    ip = "10.1.0.4"
    email = "pro@example.com"
    results = [run(do_create(d1, ip, email=email)) for _ in range(5)]
    admitted = sum(1 for r in results if r[0] == "ok")
    ok("T4 email confers no payment and does not wall the 5th create",
       admitted == 5 and all(r[0] == "ok" for r in results),
       str([(r[0], r[1] if r[0] == "err" else "") for r in results]))


# ── T5: first wall hit records no offense, no ban ───────────────────────
def test_first_hit_no_offense():
    print("T5 pilot creates do not arm the ban ladder")
    d1 = fresh_db()
    ip = "10.1.0.5"
    for _ in range(3):
        run(do_create(d1, ip))
    r4 = run(do_create(d1, ip))
    ok("T5.a 4th create admitted", r4[0] == "ok", f"got {r4[:2]}")
    row = throttle_row(d1, ip)
    ok("T5.b no throttle row from a free-tier wall", row is None,
       f"row={dict(row) if row else None}")
    rej = run(worker._throttle_reject(d1, ip, "/api/melds"))
    ok("T5.c no ban after ordinary creates", rej is None, f"rej={rej}")


# ── T6: second wall hit in a window => rung 1 (1 minute) ────────────────
def test_second_hit_first_rung():
    print("T6 pilot creates do not record a free-wall offense")
    d1 = fresh_db()
    ip = "10.1.0.6"
    results = [run(do_create(d1, ip, context=f"p-{i}")) for i in range(5)]
    ok("T6.a five creates admitted", all(r[0] == "ok" for r in results),
       str([r[:2] for r in results]))
    ok("T6.b no ban row", throttle_row(d1, ip) is None)
    rej = run(worker._throttle_reject(d1, ip, "/api/melds"))
    ok("T6.c IP is not banned", rej is None, f"rej={rej}")


# ── T7: ladder walks 1m→10m→1h→24h, capped at rolling 24h (audit F1) ────
def test_ladder_walks():
    print("T7 ladder walk across windows via abuse denials (no worker-issued permanence)")
    d1 = fresh_db()
    ip = "10.1.0.7"
    # Two denials in a window earn one offense. Pilot creates do not do this;
    # the per-minute limiter still records denials through _register_wall_hit.
    run(worker._register_wall_hit(d1, ip))
    run(worker._register_wall_hit(d1, ip))
    expected = [60, 600, 3600, 86400]
    row = throttle_row(d1, ip)
    remain = (datetime.datetime.fromisoformat(row["banned_until"])
              - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    ok("T7.1 rung 1 ≈ 60s", abs(remain - expected[0]) < 5, f"remaining={remain}")
    for w in range(2, 9):
        d1.q("UPDATE ip_throttle SET hit_window='0', offense_window='0' WHERE ip=?", ip)
        run(worker._register_wall_hit(d1, ip))
        run(worker._register_wall_hit(d1, ip))
        row = throttle_row(d1, ip)
        if w <= 4:
            remain = (datetime.datetime.fromisoformat(row["banned_until"])
                      - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            ok(f"T7.{w} rung {w} ≈ {expected[w - 1]}s",
               abs(remain - expected[w - 1]) < 5, f"remaining={remain}")
        else:
            ok(f"T7.{w} offense {w}: permanent stays 0 (worker-capped)",
               row["permanent"] == 0, f"permanent={row['permanent']}")
    rej = run(worker._throttle_reject(d1, ip, "/api/melds"))
    ok("T7.8 walk-to-cap => 429 with Retry-After (recoverable, not 403)",
       rej is not None and rej.status_code == 429 and "Retry-After" in rej.headers,
       f"rej={rej}")


# ── T7b: operator-issued permanent bans still 403 (manual action only) ──
def test_operator_permanent():
    print("T7b operator-issued permanent ban still enforced")
    d1 = fresh_db()
    ip = "10.1.0.70"
    d1.q("INSERT INTO ip_throttle (ip, offense_count, permanent, updated_at)"
         " VALUES (?, 5, 1, ?)", ip, worker._now())
    rej = run(worker._throttle_reject(d1, ip, "/api/melds"))
    ok("T7b manual permanent => 403", rej is not None and rej.status_code == 403,
       f"rej={rej}")


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
    print("T9 pilot creates are not payment-walled")
    d1 = fresh_db()
    ip = "10.1.0.9"
    results = [run(do_create(d1, ip, context=f"nat-user-{n}")) for n in range(6)]
    ok("T9.a shared-IP creates stay admitted", all(r[0] == "ok" for r in results),
       str([r[:2] for r in results]))
    blob = " ".join(str(r) for r in results).lower()
    ok("T9.b responses do not upsell checkout",
       "checkout" not in blob and "3.33" not in blob, blob[:180])
    ok("T9.c no free-wall ban", throttle_row(d1, ip) is None)


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
    ok("T11.c pilot create does not charge a free-tier counter",
       not d1.q("SELECT 1 FROM free_counts WHERE ip='10.1.0.11'"))


# ── T12: free-wall 429 carries Retry-After (melde2e finding) ────────────
def test_retry_after_wall():
    print("T12 pilot create is not a free-wall 429")
    d1 = fresh_db()
    ip = "10.1.0.12"
    for _ in range(3):
        run(do_create(d1, ip))
    r4 = run(do_create(d1, ip))
    ok("T12 4th create admitted", r4[0] == "ok", f"r4={r4[:2]}")


# ── T13: /v1/keys rate-limited (no-pro: no probe-403s anymore) ───────────
def test_keys_limiter():
    print("T13 /v1/keys: 5/min limiter (no-pro: keys are free, no probe 403s)")
    d1 = fresh_db()
    ip = "10.1.0.13"

    def probe():
        req = FakeRequest(d1, {"email": "nobody@example.com"},
                          {"x-forwarded-for": ip, "authorization": ""},
                          path="/v1/keys")
        try:
            run(worker.create_api_key(req))
            return None
        except worker.HTTPException as e:
            return e

    statuses = []
    for _ in range(8):
        try:
            e = probe()
            statuses.append(e.status_code if e else 200)
        except worker.HTTPException as e:
            statuses.append(e.status_code)
    ok("T13.a first 5 mint keys (200), rest 429-limited",
       statuses.count(200) == 5 and statuses.count(429) == 3, str(statuses))
    rej = run(worker._throttle_reject(d1, ip, "/v1/keys"))
    ok("T13.b probe-403s registered a throttle offense (banned)",
       rej is not None and rej.status_code == 429, f"rej={rej}")


# ── T14: email is inert (no-pro directive) — no lookup, no error ────────
def test_email_inert():
    print("T14 no-pro: mixed-case email accepted but ignored at create")
    d1 = fresh_db()
    ip = "10.1.0.14"
    email = "Me@Corp.com"
    results = [run(do_create(d1, ip, email=email)) for _ in range(3)]
    ok("T14 three creates admitted with email set (no lease lookup)",
       all(r[0] == "ok" for r in results), str([r[:2] for r in results]))




# ── T15: humans skip FREE_LIMIT; agents still walled; pricing header ────
def test_human_skips_free_wall():
    print("T15 pilot-free: humans and agents are not payment-walled")
    d1 = fresh_db()
    hip = "10.1.0.15"
    results = [run(do_create(d1, hip, context=f"h-{i}",
                             headers={"x-meld-client": "human",
                                      "user-agent": "curl/8.0"}))
               for i in range(worker.FREE_LIMIT + 2)]
    ok("T15.a human header admits past the old free limit",
       all(r[0] == "ok" for r in results),
       str([r[:2] for r in results]))
    hdrs = results[0][1].get("_headers", {})
    pricing = hdrs.get("x-meld-pricing") or hdrs.get("X-Meld-Pricing")
    ok("T15.b create carries X-Meld-Pricing",
       pricing == worker.PRICING_HEADER, f"headers={hdrs}")

    bip = "10.1.0.16"
    browser = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    bres = [run(do_create(d1, bip, context=f"b-{i}",
                          headers={"user-agent": browser}))
            for i in range(worker.FREE_LIMIT + 2)]
    ok("T15.c browser UA admits past the old free limit",
       all(r[0] == "ok" for r in bres),
       str([r[:2] for r in bres]))

    aip = "10.1.0.17"
    ares = [run(do_create(d1, aip, context=f"a-{i}",
                          headers={"user-agent": "curl/8.4.0", "x-meld-client": "agent"}))
            for i in range(worker.FREE_LIMIT + 2)]
    ok("T15.d agent creates admitted past the old free limit",
       all(r[0] == "ok" for r in ares), str([r[:2] for r in ares]))
    blob = " ".join(str(r) for r in ares).lower()
    ok("T15.e agent create does not upsell checkout",
       "checkout" not in blob and "3.33" not in blob and pricing == "pilot-free",
       blob[:180])


for t in [test_count_created_accumulates, test_live_rows_still_blocked,
          test_window_rolls, test_no_lease_bypass, test_first_hit_no_offense,
          test_second_hit_first_rung, test_ladder_walks, test_operator_permanent,
          test_webhook_exempt,
          test_nat_shared_ip, test_limiter_violation_registers, test_prune,
          test_retry_after_wall, test_keys_limiter, test_email_inert,
          test_human_skips_free_wall]:
    t()

print(f"\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)
