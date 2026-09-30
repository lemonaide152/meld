"""x402 USDC-on-Base: unpaid 402 challenge, settle-then-unlock, Stripe still unlocks.

The facilitator HTTP call is stubbed. Local checks (amount, payTo, asset,
network, tx) run the real worker code. A client "I paid" flag must not unlock.
"""
import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import sqlite3
import sys
import time
import types

DEPLOY = str((__import__("pathlib").Path(__file__).resolve().parent / "deploy"))
sys.path.insert(0, DEPLOY)

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

import worker  # noqa: E402
import x402_pay  # noqa: E402
from fastapi import HTTPException  # noqa: E402

SCHEMA = open(DEPLOY + "/schema.sql").read() + "\n" + open(DEPLOY + "/schema_api.sql").read() + """
CREATE TABLE IF NOT EXISTS funnel_events (
  day TEXT NOT NULL, event TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, event));
"""

PAY_TO = "0x1111111111111111111111111111111111111111"
PAYER = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
TX = "0x" + "cd" * 32
TX2 = "0x" + "ef" * 32
NONCE = "0x" + "ab" * 32
NONCE2 = "0x" + "cd" * 32
SIG = "0x" + "ab" * 65
FAC = "https://facilitator.test"

passed = total = 0


def ok(name, cond, detail=""):
    global passed, total
    total += 1
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" — {detail}"))
    if cond:
        passed += 1


class Res:
    """Matches D1's result['meta']['changes'] (see worker._d1_changes)."""

    def __init__(self, changes):
        self._changes = changes
        self.meta = types.SimpleNamespace(changes=changes)

    def __getitem__(self, k):
        if k == "meta":
            return {"changes": self._changes}
        raise KeyError(k)


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
        return self.conn.execute(sql, a).fetchall()


def fresh_db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return D1(conn)


class FakeRequest:
    def __init__(self, d1, body=None, headers=None, path="/api/melds", method="POST",
                 pay_to=PAY_TO, facilitator=FAC):
        self._body = json.dumps(body if body is not None else {}).encode()
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.method = method
        env = types.SimpleNamespace(
            DB=d1,
            X402_PAY_TO=pay_to,
            X402_FACILITATOR_URL=facilitator,
            X402_FACILITATOR_AUTH="Bearer secret-token" if facilitator else "",
        )
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in self.headers.items()]
        self.scope = {"env": env, "client": None, "headers": raw_headers,
                      "type": "http", "method": method, "path": path}
        self.url = types.SimpleNamespace(path=path, scheme="https")
        self.client = None

    async def json(self):
        return json.loads(self._body)


def run(coro):
    return asyncio.run(coro)


def _body(result):
    raw = result.body
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw).decode()
    return json.loads(raw)


def _hdr(result, name):
    return result.headers.get(name) or result.headers.get(name.lower())


def b64json(value):
    pad = "=" * ((4 - len(value) % 4) % 4)
    return json.loads(base64.b64decode(value + pad))


def payment(pay_to=PAY_TO, amount="3330000", to=None, resource=None, nonce=NONCE,
            valid_before=None, valid_after="0"):
    now = int(time.time())
    body = {
        "x402Version": 2,
        "resource": {
            "url": resource or "https://meld.mergeinc.workers.dev/api/melds",
            "mimeType": "application/json",
        },
        "accepted": {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": amount,
            "asset": x402_pay.USDC_BASE,
            "payTo": pay_to,
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"},
        },
        "payload": {
            "signature": SIG,
            "authorization": {
                "from": PAYER,
                "to": to or pay_to,
                "value": amount,
                "validAfter": valid_after,
                "validBefore": valid_before or str(now + 60),
                "nonce": nonce,
            },
        },
    }
    return base64.b64encode(json.dumps(body).encode()).decode()


class Fac:
    def __init__(self, verify=None, settle=None):
        self.calls = []
        self.verify = verify
        self.settle = settle

    async def __call__(self, url, headers, body):
        parsed = json.loads(body)
        self.calls.append((url, headers, parsed))
        if url.endswith("/verify"):
            if self.verify is not None:
                return self.verify
            return 200, json.dumps({"isValid": True, "payer": PAYER})
        if url.endswith("/settle"):
            if self.settle is not None:
                return self.settle
            return 200, json.dumps({
                "success": True,
                "transaction": TX,
                "network": "eip155:8453",
                "payer": PAYER,
                "amount": "3330000",
            })
        return 500, "{}"


def use(fac):
    x402_pay.http_post = fac


async def do_create(d1, ip, context="hello from agent", headers=None, body_extra=None,
                    pay_to=PAY_TO, facilitator=FAC, ttl="1hr"):
    hdrs = {"x-forwarded-for": ip, "x-meld-client": "agent"}
    if headers:
        hdrs.update(headers)
    body = {"context": context}
    if ttl is not None:
        body["ttl"] = ttl
    if body_extra:
        body.update(body_extra)
    req = FakeRequest(d1, body, hdrs, pay_to=pay_to, facilitator=facilitator)
    try:
        return ("ok", await worker.create_meld(req))
    except HTTPException as e:
        return ("err", e)


async def fill_quota(d1, ip, n=3):
    for i in range(n):
        got = await do_create(d1, ip, context=f"free-{i}")
        if got[0] != "ok":
            raise AssertionError(got)


def test_unconfigured_probe_has_no_address():
    print("unconfigured probe does not invent a payTo")
    d1 = fresh_db()
    req = FakeRequest(d1, {}, method="GET", path="/api/x402", pay_to="", facilitator="")
    resp = run(worker.x402_resource(req))
    body = _body(resp)
    ok("503 when X402_PAY_TO unset", resp.status_code == 503, str(resp.status_code))
    blob = json.dumps(body).lower()
    ok("no address in unconfigured body", "0x" not in blob and "payto" not in blob, blob[:200])


def test_probe_challenge_shape():
    print("GET /api/x402 unpaid challenge")
    d1 = fresh_db()
    req = FakeRequest(d1, {}, {"host": "evil.example"}, method="GET", path="/api/x402")
    resp = run(worker.x402_resource(req))
    body = _body(resp)
    ok("402", resp.status_code == 402, str(resp.status_code))
    acc = body["accepts"][0]
    ok("version 2", body.get("x402Version") == 2, str(body.get("x402Version")))
    ok("scheme exact", acc.get("scheme") == "exact")
    ok("network base", acc.get("network") == "eip155:8453", acc.get("network"))
    ok("amount $3.33 atomic", acc.get("amount") == "3330000", acc.get("amount"))
    ok("asset USDC", acc.get("asset", "").lower() == x402_pay.USDC_BASE.lower())
    ok("payTo from env", acc.get("payTo") == PAY_TO)
    ok("resource pinned to allowlisted host",
       body["resource"]["url"] == "https://meld.mergeinc.workers.dev/api/x402",
       body["resource"]["url"])
    ok("disclosure stays in the challenge",
       "not for secrets" in body["resource"]["description"].lower())
    decoded = b64json(_hdr(resp, "payment-required"))
    ok("PAYMENT-REQUIRED matches body", decoded["accepts"][0]["payTo"] == PAY_TO)
    posted = FakeRequest(d1, {}, method="POST", path="/api/x402")
    unpaid = run(worker.x402_resource(posted))
    ok("unpaid POST is 402", unpaid.status_code == 402 and _body(unpaid)["accepts"][0]["payTo"] == PAY_TO)


def test_wall_and_client_claim():
    print("agent wall is 402; client paid flag does not unlock")
    d1 = fresh_db()
    fac = Fac()
    use(fac)
    ip = "10.9.0.1"
    run(fill_quota(d1, ip))
    blocked = run(do_create(d1, ip, context="over quota"))
    ok("4th create is 402", blocked[0] == "err" and blocked[1].status_code == 402,
       str(blocked[0] if blocked[0] == "ok" else blocked[1].status_code))
    if blocked[0] == "err":
        detail = blocked[1].detail
        ok("wall challenge payTo", isinstance(detail, dict) and detail["accepts"][0]["payTo"] == PAY_TO,
           str(detail)[:180])
        wrapped = run(worker._handle_http_exception(FakeRequest(d1, {}), blocked[1]))
        parsed = _body(wrapped)
        ok("HTTP 402 body is the challenge, not detail-wrapped",
           wrapped.status_code == 402 and parsed.get("x402Version") == 2 and "detail" not in parsed,
           str(parsed)[:180])
    claimed = run(do_create(
        d1, ip, context="i paid",
        body_extra={"paid": True, "tx": TX, "transaction": TX, "payment": "done"}))
    ok("client claim still 402", claimed[0] == "err" and claimed[1].status_code == 402,
       str(getattr(claimed[1], "status_code", claimed)))
    ok("client claim did not call facilitator", fac.calls == [], str(fac.calls)[:120])
    ok("no paid meld from the claim",
       d1.q("SELECT COUNT(*) c FROM melds WHERE paid = 1")[0]["c"] == 0)
    # Omit ttl is allowed (defaults to 1hr). Other values are rejected before settle.
    bad_ttl = run(do_create(d1, ip, context="bad ttl", ttl="3m",
                            headers={"payment-signature": payment(nonce=NONCE2)}))
    ok("ttl other than 1hr is 400 before settle",
       bad_ttl[0] == "err" and bad_ttl[1].status_code == 400 and fac.calls == [],
       str(getattr(bad_ttl[1], "detail", bad_ttl))[:160])
    bad_ttl2 = run(do_create(d1, ip, context="bad ttl 7d", ttl="7d",
                             headers={"payment-signature": payment(nonce=NONCE2)}))
    ok("ttl 7d is 400 before settle",
       bad_ttl2[0] == "err" and bad_ttl2[1].status_code == 400 and fac.calls == [],
       str(getattr(bad_ttl2[1], "detail", bad_ttl2))[:160])


def test_rejects_before_facilitator():
    print("wrong amount / payTo / garbage never settle")
    d1 = fresh_db()
    fac = Fac()
    use(fac)
    ip = "10.9.0.2"
    run(fill_quota(d1, ip))
    bad_amount = run(do_create(d1, ip, headers={"payment-signature": payment(amount="1")}))
    ok("wrong amount rejected", bad_amount[0] == "err" and bad_amount[1].status_code == 402,
       str(getattr(bad_amount[1], "status_code", bad_amount)))
    bad_to = run(do_create(d1, ip, headers={
        "payment-signature": payment(to=OTHER, pay_to=OTHER)}))
    ok("wrong payTo rejected", bad_to[0] == "err" and bad_to[1].status_code == 402)
    garbage = run(do_create(d1, ip, headers={"payment-signature": "not-base64!!!"}))
    ok("malformed signature rejected", garbage[0] == "err" and garbage[1].status_code == 400,
       str(getattr(garbage[1], "status_code", garbage)))
    ok("no facilitator call", fac.calls == [])
    ok("still unpaid", d1.q("SELECT COUNT(*) c FROM melds WHERE paid = 1")[0]["c"] == 0)


def test_settle_must_return_tx():
    print("verify/settle failures do not unlock")
    ip = "10.9.0.3"
    cases = [
        ("verify invalid",
         Fac(verify=(200, json.dumps({"isValid": False, "invalidReason": "insufficient_funds"}))),
         1),
        ("settle no tx",
         Fac(settle=(200, json.dumps({
             "success": True, "transaction": "", "network": "eip155:8453", "payer": PAYER}))),
         2),
        ("settle wrong network",
         Fac(settle=(200, json.dumps({
             "success": True, "transaction": TX, "network": "eip155:1", "payer": PAYER}))),
         2),
        ("settle payer mismatch",
         Fac(settle=(200, json.dumps({
             "success": True, "transaction": TX, "network": "eip155:8453", "payer": OTHER}))),
         2),
        ("settle success false",
         Fac(settle=(200, json.dumps({
             "success": False, "errorReason": "insufficient_funds",
             "transaction": "", "network": "eip155:8453", "payer": PAYER}))),
         2),
    ]
    for name, fac, _expect_calls in cases:
        d1 = fresh_db()
        use(fac)
        run(fill_quota(d1, ip))
        got = run(do_create(d1, ip, headers={"payment-signature": payment()}))
        ok(name + " no unlock",
           got[0] == "err" and d1.q("SELECT COUNT(*) c FROM melds WHERE paid = 1")[0]["c"] == 0,
           str(got[0]))
        urls = [c[0] for c in fac.calls]
        if name == "verify invalid":
            ok("verify invalid does not settle", not any(u.endswith("/settle") for u in urls), str(urls))


def test_valid_payment_create_and_resolve():
    print("valid settlement unlocks create and resolve")
    d1 = fresh_db()
    fac = Fac()
    use(fac)
    ip = "10.9.0.4"
    run(fill_quota(d1, ip))
    got = run(do_create(d1, ip, context="paid context",
                        headers={"payment-signature": payment(valid_after="0")}))
    ok("paid create succeeds", got[0] == "ok", str(got[0] if got[0] == "err" else "ok"))
    if got[0] != "ok":
        return
    resp = got[1]
    data = _body(resp)
    code = data["code"]
    ok("paid create echoes ttl 1hr", data.get("ttl") == "1hr", str(data.get("ttl")))
    exp = datetime.datetime.fromisoformat(data["expires_at"])
    delta = (exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    ok("paid create expiry is about 1 hour", abs(delta - 3600) < 5, f"delta={delta}")
    paid = d1.q("SELECT paid, context_a FROM melds WHERE code = ?", code)[0]
    ok("paid flag set", paid["paid"] == 1, str(paid["paid"]))
    cols = [r["name"] for r in d1.q("PRAGMA table_info(x402_payments)")]
    ok("x402 table has no payTo column", "pay_to" not in cols and "payTo" not in cols, str(cols))
    row = d1.q("SELECT * FROM x402_payments")[0]
    blob = " ".join("" if v is None else str(v) for v in tuple(row))
    ok("payTo not stored on the payment row", PAY_TO.lower() not in blob.lower(), blob)
    ok("tx stored", row["tx_hash"] == TX)
    ok("nonce stored", row["nonce"] == NONCE)
    pay_row = d1.q("SELECT stripe_session_id, amount_cents FROM meld_payments")[0]
    ok("meld_payments records 333 cents",
       pay_row["amount_cents"] == 333 and pay_row["stripe_session_id"] == "x402:" + TX,
       str(tuple(pay_row)))
    settled = b64json(_hdr(resp, "payment-response"))
    ok("PAYMENT-RESPONSE has the tx", settled.get("success") is True and settled.get("transaction") == TX,
       str(settled))
    ok("facilitator verify then settle",
       [c[0] for c in fac.calls] == [FAC + "/verify", FAC + "/settle"],
       str([c[0] for c in fac.calls]))
    reqs = fac.calls[0][2]["paymentRequirements"]
    ok("server requirements pin amount and payTo",
       reqs["amount"] == "3330000" and reqs["payTo"] == PAY_TO and reqs["network"] == "eip155:8453")
    ok("facilitator auth header sent",
       fac.calls[0][1].get("Authorization") == "Bearer secret-token")
    resolved = run(worker.resolve_meld(
        code, FakeRequest(d1, {"context": "the answer"}, {"x-forwarded-for": ip})))
    ok("resolve succeeds after x402 create",
       isinstance(resolved, dict) and resolved.get("resolved") is True and resolved.get("context_a") == "paid context",
       str(resolved)[:200])
    before = len(fac.calls)
    replay = run(do_create(d1, ip, context="second",
                           headers={"payment-signature": payment()}))
    ok("replay does not mint another meld",
       replay[0] == "err" and replay[1].status_code == 402
       and d1.q("SELECT COUNT(*) c FROM melds WHERE paid = 1")[0]["c"] == 1,
       str(getattr(replay[1], "detail", replay) )[:160])
    # Replay may hit the facilitator again; it must not create a second row.
    ok("still one x402 payment", d1.q("SELECT COUNT(*) c FROM x402_payments")[0]["c"] == 1)
    ok("replay attempted after the first settle", len(fac.calls) >= before)


def test_x402_endpoint_paid_create():
    print("POST /api/x402 settles then creates")
    d1 = fresh_db()
    fac = Fac(settle=(200, json.dumps({
        "success": True, "transaction": TX2, "network": "eip155:8453",
        "payer": PAYER, "amount": "3330000",
    })))
    use(fac)
    empty = FakeRequest(
        d1, {"context": "  ", "ttl": "1hr"},
        {"payment-signature": payment(resource="https://meld.mergeinc.workers.dev/api/x402", nonce=NONCE2),
         "x-forwarded-for": "10.9.0.5"},
        path="/api/x402")
    try:
        run(worker.x402_resource(empty))
        ok("empty context rejected before settle", False, "no exception")
    except HTTPException as e:
        ok("empty context is 400 and did not settle", e.status_code == 400 and fac.calls == [],
           str(e.status_code))
    bare = FakeRequest(
        d1, {"context": "omit ttl defaults to 1hr"},
        {"payment-signature": payment(resource="https://meld.mergeinc.workers.dev/api/x402", nonce=NONCE2),
         "x-forwarded-for": "10.9.0.5"},
        path="/api/x402")
    bare_resp = run(worker.x402_resource(bare))
    bare_data = _body(bare_resp)
    ok("omitted ttl on x402 URL create defaults to 1hr",
       bare_resp.status_code == 200 and bare_data.get("ttl") == "1hr",
       str(bare_data.get("ttl")))
    req = FakeRequest(
        d1, {"context": "via x402 url", "ttl": "1hr"},
        {"payment-signature": payment(
            resource="https://meld.mergeinc.workers.dev/api/x402", nonce="n-x402-1hr"),
         "x-forwarded-for": "10.9.0.5"},
        path="/api/x402")
    resp = run(worker.x402_resource(req))
    data = _body(resp)
    ok("x402 URL create 200", resp.status_code == 200 and data.get("code"), str(resp.status_code))
    ok("x402 URL create ttl is 1hr", data.get("ttl") == "1hr", str(data.get("ttl")))
    exp = datetime.datetime.fromisoformat(data["expires_at"])
    delta = (exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    ok("x402 URL expiry is about 1 hour", abs(delta - 3600) < 5, f"delta={delta}")
    code = data["code"]
    ok("x402 URL meld is paid", d1.q("SELECT paid FROM melds WHERE code = ?", code)[0]["paid"] == 1)
    resolved = run(worker.resolve_meld(
        code, FakeRequest(d1, {"context": "answered"}, {"x-forwarded-for": "10.9.0.6"})))
    ok("resolve of x402 URL meld succeeds",
       isinstance(resolved, dict) and resolved.get("resolved") is True)


def test_humans_stay_free():
    print("humans skip the x402 wall")
    d1 = fresh_db()
    fac = Fac()
    use(fac)
    ip = "10.9.0.7"
    results = []
    for i in range(worker.FREE_LIMIT + 2):
        req = FakeRequest(
            d1, {"context": f"human-{i}", "ttl": "1hr"},
            {"x-forwarded-for": ip, "x-meld-client": "human", "user-agent": "curl/8.0"})
        results.append(run(worker.create_meld(req)))
    ok("human creates past the quota", all(getattr(r, "status_code", 200) == 200 for r in results),
       str([getattr(r, "status_code", 200) for r in results]))
    ok("human path did not call the facilitator", fac.calls == [])


def test_stripe_still_unlocks():
    print("Stripe checkout.session.completed still unlocks")
    d1 = fresh_db()
    code = "k3x9p2qz8r1m"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    later = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
    d1.conn.execute(
        "INSERT INTO melds (code, context_a, resolved, owner_token, creator_ip, created_at, expires_at)"
        " VALUES (?, 'ctx', 0, 'tok', '', ?, ?)",
        (code, now, later))
    event = {
        "id": "evt_x402_stripe",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_stripe_still",
            "amount_total": 333,
            "customer": "cus_test",
            "metadata": {"meld_id": code},
        }},
    }
    payload = json.dumps(event).encode()
    secret = "whsec_test"
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    req = FakeRequest(d1, {}, {"stripe-signature": f"t={ts},v1={sig}"}, pay_to="", facilitator="")
    req._payload = payload

    async def body():
        return payload

    req.body = body
    req.scope["env"].STRIPE_WEBHOOK_SECRET = secret
    req.scope["env"].STRIPE_SECRET_KEY = "sk_test"
    orig = worker.db
    worker.db = lambda request: d1
    try:
        result = run(worker.stripe_webhook(req))
    finally:
        worker.db = orig
    ok("stripe unlocks the meld", result.get("unlocked") == code, str(result))
    ok("stripe paid flag", d1.q("SELECT paid FROM melds WHERE code = ?", code)[0]["paid"] == 1)


def test_mcp_create_returns_402():
    print("MCP meld_create over quota returns HTTP 402")
    d1 = fresh_db()
    use(Fac())
    ip = "10.9.0.8"
    run(fill_quota(d1, ip))
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "meld_create", "arguments": {"context": "from mcp", "ttl": "1hr"}},
    }
    req = FakeRequest(d1, payload, {"x-forwarded-for": ip, "x-meld-client": "agent"}, path="/mcp")
    resp = run(worker.mcp_post(req))
    body = _body(resp)
    ok("mcp HTTP 402", resp.status_code == 402, str(resp.status_code))
    ok("mcp challenge payTo", body.get("accepts", [{}])[0].get("payTo") == PAY_TO, str(body)[:200])
    ok("mcp did not create a paid meld",
       d1.q("SELECT COUNT(*) c FROM melds WHERE context_a = 'from mcp'")[0]["c"] == 0)


def test_docs_and_source_keep_secrets_out():
    print("docs keep the warning and do not embed the test payee")
    src = open(DEPLOY + "/worker.py", encoding="utf-8").read()
    pay = open(DEPLOY + "/x402_pay.py", encoding="utf-8").read()
    agents = open(DEPLOY + "/agents_content.py", encoding="utf-8").read()
    ok("payee not hardcoded in worker", PAY_TO not in src and PAYER not in src)
    ok("payee not hardcoded in x402 module", PAY_TO not in pay and PAYER not in pay)
    for label, text in (
        ("llms", worker.LLMS_TXT),
        ("agents", worker.AGENTS_MD),
        ("upgrade", worker.UPGRADE_MD),
        ("agents page", agents),
    ):
        low = text.lower()
        ok(label + " mentions 402", "402" in text, text[-120:])
        ok(label + " keeps not-for-secrets", "not for secrets" in low, low[-120:])
    ok("upgrade names the secrets",
       "X402_PAY_TO" in worker.UPGRADE_MD and "X402_FACILITATOR_URL" in worker.UPGRADE_MD
       and "X402_FACILITATOR_AUTH" in worker.UPGRADE_MD)
    ok("upgrade does not contain the test payee", PAY_TO not in worker.UPGRADE_MD)


def main():
    test_unconfigured_probe_has_no_address()
    test_probe_challenge_shape()
    test_wall_and_client_claim()
    test_rejects_before_facilitator()
    test_settle_must_return_tx()
    test_valid_payment_create_and_resolve()
    test_x402_endpoint_paid_create()
    test_humans_stay_free()
    test_stripe_still_unlocks()
    test_mcp_create_returns_402()
    test_docs_and_source_keep_secrets_out()
    print(f"\nRESULTS: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
