"""Pay-per-meld webhook logic tests — no network, fake D1, real worker code.
Verifies spec v1.1 §Security:
  req 3  idempotent on checkout.session.id (replay = no-op, not double-unlock)
  req 5  wrong amount_total rejected even with valid signature
  req 4  exactly one capability paid flag flips on correct 333
"""
import asyncio, sys, json, os

DEPLOY = "/opt/data/profiles/meld/workspace/deploy"
sys.path.insert(0, DEPLOY)

# ── shim the Workers runtime imports before importing worker.py ──
import types
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

passed = total = 0
def ok(name, cond, detail=""):
    global passed, total
    total += 1
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" — {detail}"))
    if cond: passed += 1


class FakeResult:
    def __init__(self, rows=None, changes=1):
        self._rows = rows or []
        self._changes = changes
    def first(self):
        return self._rows[0] if self._rows else None
    def __getitem__(self, k):
        if k == "meta":
            return {"changes": self._changes}
        raise KeyError(k)


class FakeD1:
    """Minimal D1 double: records every statement for assertions."""
    def __init__(self, seen_event_ids=(), paid_melds=()):
        self.statements = []
        self.seen_event_ids = set(seen_event_ids)
        self.paid_melds = set(paid_melds)   # codes with paid=1
        self.meld_payments = {}             # stripe_session_id -> meld_code
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
        d1.statements.append(("SELECT1", self.sql, self._bind))
        sql = self.sql
        if "FROM webhook_events WHERE stripe_event_id" in sql:
            return {1} if self._bind[0] in d1.seen_event_ids else None
        if "SELECT code FROM melds" in sql:
            return {"code": self._bind[0]} if self._bind[0] in d1.paid_melds or self._bind[0] == MELD else None
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
SID = "cs_test_333"
EID = "evt_test_1"
WHSEC = "whsec_test"


def make_event(amount, meld_id=MELD, session_id=SID, event_id=EID):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": session_id,
            "amount_total": amount,
            "customer": "cus_test",
            "subscription": None,
            "metadata": {"meld_id": meld_id} if meld_id else {},
            "customer_details": {"email": "a@b.com"},
        }},
    }


class FakeReq:
    def __init__(self, payload, sig):
        self._payload = payload
        self.headers = {"stripe-signature": sig}
        self.scope = {"env": types.SimpleNamespace(
            DB=None, STRIPE_SECRET_KEY="sk_test", STRIPE_WEBHOOK_SECRET=WHSEC)}
    async def body(self):
        return self._payload


def signed_headers(event, secret=WHSEC, ts=None):
    payload = json.dumps(event).encode()
    t = str(ts or int(time.time()))
    v1 = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, f"t={t},v1={v1}"


import time, hashlib, hmac  # noqa: E402

async def run_webhook(event, d1, sig_override=None):
    payload, sig = signed_headers(event)
    req = FakeReq(payload, sig_override or sig)
    # monkeypatch db() to return our fake
    orig_db = worker.db
    worker.db = lambda request: d1
    try:
        return await worker.stripe_webhook(req)
    finally:
        worker.db = orig_db


async def main():
    # T1: correct amount, correct metadata → unlock, paid flag flipped
    d1 = FakeD1()
    r = await run_webhook(make_event(333), d1)
    ok("T1 correct-333 unlocks", r.get("unlocked") == MELD, str(r))
    ok("T1 paid flag flipped", d1.paid_melds == {MELD}, str(d1.paid_melds))
    ok("T1 payment row recorded", d1.meld_payments.get(SID) == MELD, str(d1.meld_payments))
    ok("T1 ledger meld.unlock", any("meld.unlock" in st[1] for st in d1.statements), str(d1.statements)[:200])

    # T2: replay same session (new event id, same session id) → dedup, no second unlock
    d1b = FakeD1(seen_event_ids=set(), paid_melds={MELD})  # already unlocked
    d1b.meld_payments = {SID: MELD}  # session already processed
    r2 = await run_webhook(make_event(333, event_id="evt_new_1"), d1b)
    ok("T2 replayed session → dedup", r2.get("dedup") is True, str(r2))
    ok("T2 no re-UPDATE", not any("UPDATE melds" in st[1] for st in d1b.statements), str(d1b.statements)[:200])
    ok("T2 no double ledger entry", not any("meld.unlock" in str(b) for b in d1b.ledger), str(d1b.ledger)[:120])

    # T3: WRONG amount (even signed) → rejected, no unlock
    d1c = FakeD1()
    r3 = await run_webhook(make_event(100), d1c)  # $1.00 ≠ 333
    ok("T3 wrong-amount rejected", r3.get("rejected") == "wrong_amount", str(r3))
    ok("T3 no paid flag flip", d1c.paid_melds == set(), str(d1c.paid_melds))
    ok("T3 rejection logged", any("wrong_amount_rejected" in st[1] for st in d1c.statements), str(d1c.statements)[:200])

    # T4: bad signature → 400 before anything
    d1d = FakeD1()
    from fastapi import HTTPException
    try:
        await run_webhook(make_event(333), d1d, sig_override="t=123,v1=deadbeef")
        ok("T4 bad sig rejected", False, "no exception raised")
    except HTTPException as e:
        ok("T4 bad sig → 400", e.status_code == 400, str(e.status_code))
    ok("T4 nothing written", d1d.paid_melds == set() and not any("INSERT INTO meld_payments" in st[1] for st in d1d.statements), "")

    # T5: legacy event (no meld_id) still hits the subscription branch
    d1e = FakeD1()
    legacy = make_event(2500, meld_id=None)
    legacy["data"]["object"]["customer_details"] = {"email": "sub@b.com"}
    legacy["data"]["object"]["subscription"] = "sub_legacy"
    r5 = await run_webhook(legacy, d1e)
    ok("T5 legacy lease.grant path intact", any("lease.grant" in st[1] for st in d1e.statements), str(d1e.statements)[:200])

    print(f"\nRESULTS: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)

asyncio.run(main())
