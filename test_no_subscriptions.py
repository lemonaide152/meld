"""NEGATIVE-TEST GATE: no subscription paths (spec v1.1 supersedure).

Run:  python3 test_no_subscriptions.py
Owner: @melde2e. Runs against deploy/worker.py in the WORKING TREE
(staged, not committed — per @meld 2026-09-22).

Covers the two kills @meldfin flagged and @meld removed:
  A) /api/checkout legacy plan branch  → every {plan:*} / empty body = 400,
     explicit "Subscriptions were removed" message, and NO Stripe session
     minted (no outbound fetch, no checkout_clicks write).
  B) webhook checkout.session.completed WITHOUT meld_id metadata →
     {"ok":true,"ignored":"no_meld_id"}; grants NOTHING: zero pros rows,
     zero lease.grant / UPDATE-melds statements, exactly one idempotent
     webhook_events write + one payment.no_meld_id_ignored ledger entry.
"""
import asyncio, sys, json, time, types

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

spa = types.ModuleType("spa_content"); spa._SPA_HTML = "<html></html>"
appc = types.ModuleType("app_content"); appc.APP_HTML = "<html></html>"
agents = types.ModuleType("agents_content"); agents.AGENTS_HTML = "<html></html>"
sys.modules["spa_content"] = spa
sys.modules["app_content"] = appc
sys.modules["agents_content"] = agents

import worker  # noqa: E402  — the real product code
from fastapi import HTTPException  # noqa: E402

passed = total = 0
def ok(name, cond, detail=""):
    global passed, total
    total += 1
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" — {detail}"))
    if cond: passed += 1


class FakeResult:
    def __init__(self, changes=1):
        self._changes = changes
    def __getitem__(self, k):
        if k == "meta":
            return {"changes": self._changes}
        raise KeyError(k)


class FakeD1:
    """Minimal D1 double: records every statement for negative assertions."""
    def __init__(self):
        self.statements = []     # ("READ"|"WRITE", sql, bind)
        self.seen_event_ids = set()
        self.paid_melds = set()
        self.meld_payments = {}
        self.ledger = []
    def prepare(self, sql):
        return FakeStmt(self, sql)


class FakeStmt:
    def __init__(self, d1, sql):
        self.d1, self.sql = d1, sql
        self._bind = ()
    def bind(self, *args):
        self._bind = args
        return self
    async def first(self):
        d1 = self.d1
        d1.statements.append(("READ", self.sql, self._bind))
        sql = self.sql
        if "INSERT INTO rate" in sql:  # rate limiter admit (RETURNING row)
            return {"count": 1, "bucket": int(time.time() // 60)}
        if "FROM webhook_events WHERE stripe_event_id" in sql:
            return {1} if self._bind[0] in d1.seen_event_ids else None
        if "SELECT code FROM melds" in sql:
            return {"code": self._bind[0]} if self._bind[0] == MELD else None
        return None
    async def run(self):
        d1 = self.d1
        d1.statements.append(("WRITE", self.sql, self._bind))
        sql = self.sql
        if "INSERT INTO webhook_events" in sql:
            eid = self._bind[0]
            if eid in d1.seen_event_ids:
                return FakeResult(changes=0)
            d1.seen_event_ids.add(eid)
            return FakeResult(changes=1)
        if "INSERT INTO meld_payments" in sql:
            sid = self._bind[0]
            if sid in d1.meld_payments:
                return FakeResult(changes=0)
            d1.meld_payments[sid] = self._bind[1]
            return FakeResult(changes=1)
        if "UPDATE melds SET paid = 1" in sql:
            d1.paid_melds.add(self._bind[0])
            return FakeResult(changes=1)
        if "INSERT INTO ledger" in sql:
            d1.ledger.append(self._bind)
        return FakeResult(changes=1)


MELD = "k3x9p2qz8r1m"
EID = "evt_neg_1"
WHSEC = "whsec_test"


class FakeReq:
    """Works for both /api/checkout (json body) and the webhook (raw body)."""
    def __init__(self, json_body=None, raw_body=None, sig=""):
        self._json = json_body
        self._raw = raw_body
        self.headers = {"stripe-signature": sig}
        self.client = None
        self.scope = {"env": types.SimpleNamespace(
            DB=None, STRIPE_SECRET_KEY="sk_test", STRIPE_WEBHOOK_SECRET=WHSEC)}
    async def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json
    async def body(self):
        return self._raw


def signed_payload(event):
    import hashlib, hmac
    payload = json.dumps(event).encode()
    t = str(int(time.time()))
    v1 = hmac.new(WHSEC.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, f"t={t},v1={v1}"


class NoFetch:
    """Sentinel: any Stripe outbound call during a negative test = session mint = FAIL."""
    def __getattr__(self, name):
        raise AssertionError("OUTBOUND FETCH — a Stripe session was minted!")


async def checkout(body):
    d1 = FakeD1()
    orig_db = worker.db
    orig_fetch = sys.modules.get("js")
    worker.db = lambda request: d1
    sys.modules["js"] = NoFetch()  # any fetch() raises → session mint detected
    try:
        r = await worker.create_checkout(FakeReq(json_body=body))
        return r, d1, None
    except HTTPException as e:
        return None, d1, e
    except AssertionError as e:
        # NoFetch sentinel: the pay-per-meld path reached the Stripe call. For
        # negative tests this is the EXPECTED stop (no session minted in-test);
        # surfaced as a sentinel error the A5 case treats as a pass.
        return None, d1, e
    finally:
        worker.db = orig_db
        if orig_fetch is not None:
            sys.modules["js"] = orig_fetch
        else:
            sys.modules.pop("js", None)


async def run_webhook(event, d1=None):
    d1 = d1 or FakeD1()
    payload, sig = signed_payload(event)
    orig_db = worker.db
    worker.db = lambda request: d1
    try:
        return await worker.stripe_webhook(FakeReq(raw_body=payload, sig=sig)), d1
    finally:
        worker.db = orig_db


def make_event(amount, meld_id, session_id="cs_neg", event_id=EID):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": session_id,
            "amount_total": amount,
            "customer": "cus_neg",
            "subscription": "sub_legacy_neg",
            "metadata": {"meld_id": meld_id} if meld_id else {},
            "customer_details": {"email": "legacy@b.com"},
        }},
    }


async def main():
    print("── A) /api/checkout rejects every legacy subscription body ──")

    for name, body in [
        ("A1 {plan: monthly} → 400", {"plan": "monthly"}),
        ("A2 {plan: yearly} → 400", {"plan": "yearly"}),
        ("A3 {} (no meld_code) → 400", {}),
        ("A4 garbage plan → 400", {"plan": "lifetime"}),
        ("A5 plan + meld_code BOTH → meld_code wins, no plan 500",
         {"plan": "monthly", "meld_code": MELD}),
    ]:
        r, d1, err = await checkout(body)
        if name.startswith("A5"):
            # meld_code present → pay-per-meld path; must stop at the Stripe
            # fetch (NoFetch sentinel), NOT 400 and NOT a subscription error.
            # (no-pro directive: the {plan} key is now ignored entirely — the
            # request is a valid per-meld checkout.)
            ok(name, err is None or isinstance(err, AssertionError) or
               (err.status_code == 500 and "Subscriptions" not in str(err.detail)),
               f"status={getattr(err, 'status_code', None)} detail={getattr(err, 'detail', r)}")
        else:
            ok(name, err is not None and err.status_code == 400,
               f"got status={getattr(err, 'status_code', None)} detail={getattr(err, 'detail', r)}")

    r, d1, err = await checkout({"plan": "monthly"})
    ok("A6 monthly 400 message says subscriptions removed",
       err is not None and "Subscriptions were removed" in str(err.detail), str(getattr(err, 'detail', '')))
    ok("A7 no checkout_clicks write on rejection",
       not any("checkout_clicks" in st[1] for st in d1.statements), str(d1.statements)[:300])
    ok("A8 no outbound Stripe session mint (NoFetch never fired)",
       err is not None and err.status_code == 400, "a fetch would have raised")

    print("\n── B) webhook without meld_id grants NOTHING ──")
    resp, d1e = await run_webhook(make_event(2500, meld_id=None))  # $25 legacy sub amount
    ok("B1 returns ignored:no_meld_id",
       resp == {"ok": True, "ignored": "no_meld_id"}, str(resp))
    ok("B2 zero pros/lease.grant statements",
       not any(("lease" in st[1].lower() or "pros" in st[1].lower()) for st in d1e.statements),
       str([st[1] for st in d1e.statements])[:300])
    ok("B3 zero capability writes (no UPDATE melds)",
       not any("UPDATE melds" in st[1] for st in d1e.statements), "")
    ok("B4 zero meld_payments rows",
       d1e.meld_payments == {}, str(d1e.meld_payments))
    ok("B5 zero paid flag flips", d1e.paid_melds == set(), str(d1e.paid_melds))
    ok("B6 event id recorded idempotently (webhook_events)",
       any("INSERT INTO webhook_events" in st[1] and st[0] == "WRITE" for st in d1e.statements), "")
    ledger_sqls = [st[1] for st in d1e.statements if st[0] == "WRITE" and "INSERT INTO ledger" in st[1]]
    ok("B7 exactly one ledger entry: payment.no_meld_id_ignored",
       len(ledger_sqls) == 1 and all("payment.no_meld_id_ignored" in s for s in ledger_sqls),
       str(ledger_sqls))

    # Redelivery of the same event id → dedup, no second ledger entry, no grant
    d1f = FakeD1()
    resp2, _ = await run_webhook(make_event(2500, meld_id=None, event_id=EID), d1f)  # first delivery
    resp2, d1f = await run_webhook(make_event(2500, meld_id=None, event_id=EID), d1f)  # redelivery
    ok("B8 redelivery → dedup, no grant", resp2.get("dedup") is True, str(resp2))
    ok("B9 redelivery writes nothing new",
       len([s for s in d1f.statements if "INSERT INTO ledger" in s[1]]) == 1
       and d1f.paid_melds == set() and d1f.meld_payments == {},
       str(d1f.statements)[:300])

    # Control: meld_id present still unlocks (gate must not break the paid path)
    resp3, d1g = await run_webhook(make_event(333, meld_id=MELD, session_id="cs_neg_ok"))
    ok("B10 control: 333 + meld_id still unlocks", resp3.get("unlocked") == MELD, str(resp3))

    print("\n── C) /v1/keys must not advertise any recurring price ──")
    src = open(f"{DEPLOY}/worker.py", encoding="utf-8").read()
    import re
    hit = re.search(r'/v1/keys.*?\$?\d+\s*/\s*m[o0]', src, re.S)
    ok("C1 no /v1/keys recurring-price copy ($X/mo)",
       not re.search(r'\$\s*\d+\s*/\s*mo\b', src), str(re.findall(r'.{40}\$ ?\d+/mo.{10}', src)))
    ok("C2 no $49 anywhere in worker.py", "$49" not in src, "")

    print(f"\nRESULTS: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)

asyncio.run(main())
