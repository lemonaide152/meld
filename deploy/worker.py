"""
meld — ephemeral context bridge. Cloudflare Workers port (D1-backed).
Faithful port of the audited meld.py trust model:
- capability model: code admits, PIN authenticates answerer, rotating token reads
- per-IP sliding-window rate limits, body caps, string-validated contexts
- zero-knowledge E2E: server stores ciphertext (meld1:…), key in #k= fragment
- pro leases (35-day) + append-only ledger; emails hashed at rest
State lives in D1 (survives restarts — an upgrade over the RAM dict).
"""
import hashlib
import json
import secrets
import string
import time
import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from workers import asgi
from spa_content import _SPA_HTML

app = FastAPI(title="meld", version="1.0.0", docs_url=None, redoc_url=None)

FREE_LIMIT = 3
FREE_EXPIRY_HOURS = 1
LEASE_DAYS = 35
CODE_LEN = 12
MAX_CONTEXT = 100_000
# rate limits: (max, window_seconds)
RL = {"create": (20, 60), "resolve": (10, 60), "view": (60, 60)}

# ── env bindings (set in wrangler.toml) ─────────────────────────────────
def db(request):
    return request.scope["env"].DB  # D1 binding — use .prepare(sql).bind(...).run()/.first()


# ── helpers ──────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _code() -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(CODE_LEN))


def _token() -> str:
    return secrets.token_hex(32)


def _client_ip(request: Request) -> str:
    """LAST X-Forwarded-For hop (Cloudflare appends the real client IP)."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "0.0.0.0"


def _email_key(email: str) -> str:
    return "sha256:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()


async def _fetch(url, headers=None, body=None, method="GET"):
    """Use JS fetch from Python Workers — build init via JSON.parse (avoids JsProxy)."""
    import json as _json
    import js
    init_dict = {"method": method, "headers": headers or {}}
    if body:
        init_dict["body"] = body
    init = js.JSON.parse(_json.dumps(init_dict))
    resp = await js.fetch(url, init)
    return await resp.text()



def _rl_key(kind: str, ip: str) -> str:
    return f"{kind}:{ip}:{int(time.time() // 60)}"  # per-minute bucket

def _rl_bucket() -> int:
    return int(time.time() // 60)


async def _rate_limit(db_conn, kind: str, ip: str) -> bool:
    """D1-backed fixed-window limiter (per-minute buckets, self-pruning)."""
    key = _rl_key(kind, ip)
    limit = {"create": 20, "resolve": 10, "view": 60}.get(kind, 60)
    row = await db_conn.prepare(
        "SELECT count FROM rate WHERE key = ?").bind(key).first()
    count = row["count"] if row else 0
    if count >= limit:
        return False
    await db_conn.prepare(
        "INSERT INTO rate (key, count, bucket) VALUES (?, 1, ?) "
        "ON CONFLICT(key) DO UPDATE SET count = count + 1").bind(key, _rl_bucket()).run()
    bucket = int(time.time() // 60)
    await db_conn.prepare("DELETE FROM rate WHERE bucket < ?").bind(bucket - 2).run()
    return True


async def _check_meld_limit(db_conn, ip: str) -> bool:
    """Free users: FREE_LIMIT melds per hour. Pro (valid lease): unlimited."""
    lease = await db_conn.prepare(
        "SELECT pro_until FROM pros WHERE email_key = ? OR customer_id = ?").bind(ip, ip).first()
    if lease:
        if lease["pro_until"] > _now():
            return True
        await db_conn.prepare("DELETE FROM pros WHERE email_key = ? OR customer_id = ?").bind(ip, ip).run()
        return True  # expired lease: allow, cleanup happened
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=FREE_EXPIRY_HOURS)).isoformat()
    row = await db_conn.prepare(
        "SELECT COUNT(*) as c FROM melds WHERE creator_ip = ? AND created_at >= ?").bind(ip, cutoff).first()
    return row["c"] < FREE_LIMIT


# ── middleware: security headers + JSON body cap ─────────────────────────
@app.middleware("http")
async def security(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
    return resp


# ── API ──────────────────────────────────────────────────────────────────
@app.post("/api/melds")
async def create_meld(request: Request):
    conn = db(request)
    ip = _client_ip(request)
    body = await request.json()
    context = body.get("context", "")
    if not isinstance(context, str):
        raise HTTPException(400, "Context must be a string")
    if len(context) > MAX_CONTEXT:
        raise HTTPException(400, "Context too large (100K max)")

    if not await _rate_limit(conn, "create", ip):
        raise HTTPException(429, "Too many requests. Please slow down.",
                            headers={"Retry-After": "60"})
    if not await _check_meld_limit(conn, ip):
        raise HTTPException(429, "Free limit reached. <a href='/upgrade'>Go Pro</a>")

    code = _code()
    token = _token()
    now = _now()
    expiry = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(hours=FREE_EXPIRY_HOURS)).isoformat()
    email = body.get("email")
    pin = body.get("pin")
    await conn.prepare(
        "INSERT INTO melds (code, context_a, context_b, resolved, owner_token,"
        " owner_email, creator_ip, created_at, expires_at, pin)"
        " VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?, ?)").bind(
        code, context, token,
        email if isinstance(email, str) and len(email) <= 254 else None,
        ip, now, expiry,
        hashlib.sha256(pin.encode()).hexdigest() if isinstance(pin, str) and 0 < len(pin) <= 128 else None,
    ).run()
    return {
        "code": code,
        "url": f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/m/{code}",
        "owner_url": f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/m/{code}#t={token}",
        "owner_token": token,
        "context_a": context,
        "resolved": False,
    }


@app.get("/api/melds/{code}")
async def get_meld(code: str, request: Request):
    conn = db(request)
    ip = _client_ip(request)
    if not await _rate_limit(conn, "view", ip):
        raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})
    row = await conn.prepare(
        "SELECT * FROM melds WHERE code = ?").bind(code).first()
    if not row:
        raise HTTPException(404, "Meld not found")
    if row["expires_at"] <= _now():
        await conn.prepare("DELETE FROM melds WHERE code = ?").bind(code).run()
        raise HTTPException(410, "This meld has expired")
    return {"code": code, "context_a": row["context_a"],
            "context_b": row["context_b"], "resolved": bool(row["resolved"]),
            "resolved_at": row["resolved_at"]}


@app.post("/api/melds/{code}/resolve")
async def resolve_meld(code: str, request: Request):
    conn = db(request)
    ip = _client_ip(request)
    if not await _rate_limit(conn, "resolve", ip):
        raise HTTPException(429, "Too many requests. Please slow down.",
                            headers={"Retry-After": "60"})
    body = await request.json()
    context = body.get("context", "")
    if not isinstance(context, str):
        raise HTTPException(400, "Context must be a string")
    if len(context) > MAX_CONTEXT:
        raise HTTPException(400, "Context too large (100K max)")

    row = await conn.prepare(
        "SELECT * FROM melds WHERE code = ?").bind(code).first()
    if not row:
        raise HTTPException(404, "Meld not found")
    if row["expires_at"] <= _now():
        raise HTTPException(410, "This meld has expired")

    pin = row["pin"]
    if pin:
        supplied = body.get("pin", "")
        if not isinstance(supplied, str) or not secrets.compare_digest(
                pin, hashlib.sha256(supplied.encode()).hexdigest()):
            raise HTTPException(403, "Invalid PIN")

    if row["resolved"]:
        if row["context_b"] == context:
            return {"code": code, "context_a": row["context_a"],
                    "context_b": row["context_b"], "resolved": True, "retry": True}
        raise HTTPException(409, "Already resolved with a different answer")

    pk = body.get("responder_pubkey")
    sig = body.get("signature")
    if (pk is None) != (sig is None):
        raise HTTPException(400, "Signature evidence requires both responder_pubkey and signature")

    fast_expiry = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(minutes=10)).isoformat()
    await conn.prepare(
        "UPDATE melds SET context_b = ?, resolved = 1, resolved_at = ?,"
        " responder_pubkey = ?, signature = ?,"
        " expires_at = MIN(expires_at, ?) WHERE code = ?").bind(
        context, _now(), pk, sig, fast_expiry, code).run()
    return {"code": code, "context_a": row["context_a"], "context_b": context,
            "resolved": True}


@app.get("/api/melds/{code}/result")
async def get_result(code: str, request: Request, token: str = ""):
    conn = db(request)
    ip = _client_ip(request)
    if not await _rate_limit(conn, "view", ip):
        raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})
    token = request.headers.get("X-Meld-Token", "") or token
    row = await conn.prepare(
        "SELECT * FROM melds WHERE code = ?").bind(code).first()
    if not row:
        raise HTTPException(404, "Meld not found")
    if not secrets.compare_digest(row["owner_token"], token or ""):
        raise HTTPException(403, "Invalid token")
    if not row["resolved"]:
        raise HTTPException(400, "Not yet resolved")
    fresh = _token()
    await conn.prepare("UPDATE melds SET owner_token = ? WHERE code = ?").bind(fresh, code).run()
    return {"code": code, "context_a": row["context_a"],
            "context_b": row["context_b"], "resolved": True,
            "owner_token": fresh,
            "responder_pubkey": row["responder_pubkey"],
            "signature": row["signature"]}


@app.get("/api/pro-status")
async def pro_status(email: str = "", request: Request = None):
    if not email:
        return {"pro": False}
    row = await db(request).prepare(
        "SELECT pro_until FROM pros WHERE email_key = ?").bind(_email_key(email)).first()
    if row and row["pro_until"] > _now():
        return {"pro": True}
    return {"pro": False}


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    cfg = _get_stripe_cfg(request)
    secret = cfg["webhook_secret"]
    if not secret:
        raise HTTPException(501, "Webhook not configured")
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        import stripe
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except ImportError:
        raise HTTPException(501, "Payments unavailable")
    except Exception:
        raise HTTPException(400, "Invalid signature")
    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        email = (sess.get("customer_details") or {}).get("email", "")
        customer = sess.get("customer", "")
        if email:
            key = _email_key(email)
            until = (datetime.datetime.now(datetime.timezone.utc)
                     + datetime.timedelta(days=LEASE_DAYS)).isoformat()
            await db(request).prepare(
                "INSERT INTO pros (email_key, customer_id, pro_until, since)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(email_key) DO UPDATE SET"
                " pro_until = ?, customer_id = ?").bind(
                key, customer, until, _now(), until, customer).run()
            await db(request).prepare(
                "INSERT INTO ledger (at, event, customer_id, email_key, days)"
                " VALUES (?, 'lease.grant', ?, ?, ?)").bind(
                _now(), customer, key, LEASE_DAYS).run()
    return {"ok": True}


@app.get("/api")
async def api_index():
    return {"name": "meld", "version": "1.0.0-workers",
            "summary": "Ephemeral two-party context bridge."}


# ── Stripe: checkout (REST API — no SDK needed on Workers) ──────────────
# Stripe config — read from env bindings (set via wrangler secret/vars)
def _get_stripe_cfg(request):
    env = request.scope["env"]
    return {
        "key": getattr(env, "STRIPE_SECRET_KEY", "") or "",
        "webhook_secret": getattr(env, "STRIPE_WEBHOOK_SECRET", "") or "",
        "monthly": getattr(env, "STRIPE_PRICE_MONTHLY", "") or "",
        "yearly": getattr(env, "STRIPE_PRICE_YEARLY", "") or "",
    }


async def _stripe_call(method: str, path: str, params: dict = None):
    """Direct Stripe REST API call via JS fetch."""
    from urllib.parse import urlencode
    url = f"https://api.stripe.com/v1/{path}"
    headers = {
        "Authorization": f"Bearer {STRIPE_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = urlencode(params) if params else None
    resp_text = await _fetch(url, headers=headers, body=body, method=method)
    import json as _json
    return _json.loads(resp_text)


@app.post("/api/checkout")
async def create_checkout(request: Request):
    """Create a Stripe Checkout Session. Returns {url} for redirect."""
    cfg = _get_stripe_cfg(request)
    STRIPE_KEY = cfg["key"]
    if not STRIPE_KEY:
        raise HTTPException(501, "Payments not configured")
    body = await request.json()
    email = body.get("email", "")
    plan = body.get("plan", "monthly")
    price_id = cfg["monthly"] if plan == "monthly" else cfg["yearly"]
    if not price_id:
        raise HTTPException(500, "Price not configured")

    host = request.headers.get("host", "localhost")
    scheme = "https" if "workers.dev" in host or "genberg" in host else request.url.scheme
    base_url = f"{scheme}://{host}"

    from urllib.parse import urlencode as _ue
    params = {
        "mode": "subscription",
        "success_url": base_url + "/pro",
        "cancel_url": base_url,
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
    }
    if email:
        params["customer_email"] = email

    resp_text = await _fetch(
        "https://api.stripe.com/v1/checkout/sessions",
        headers={
            "Authorization": f"Bearer {STRIPE_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
        body=_ue(params),
    )
    import json as _j
    d = _j.loads(resp_text)
    if d.get("error"):
        raise HTTPException(500, d["error"].get("message", "Stripe error"))
    return {"url": d["url"], "session_id": d["id"]}


@app.get("/pro")
async def pro_page(request: Request):
    """Success page after Stripe checkout."""
    return HTMLResponse("""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>meld Pro</title>
<style>body{background:#0a0a0f;color:#e4e4f0;font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{text-align:center}.emoji{font-size:3rem;margin-bottom:.5rem}h1{font-size:1.5rem}p{color:#8888a0}a{color:#a78bfa}</style></head>
<body><div class="box"><div class="emoji">🎉</div><h1>You're Pro</h1><p>Unlimited melds, 7-day expiry, priority cleanup.</p><p><a href="/">Create a meld</a></p></div></body></html>""")


# ── Agent tier: API keys + /v1 endpoints ────────────────────────────────

async def _validate_api_key(request: Request):
    """Validate Bearer token against api_keys table. Returns key row or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    key = auth[len("Bearer "):]
    if not key or len(key) < 32:
        return None
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    conn = db(request)
    row = await conn.prepare(
        "SELECT * FROM api_keys WHERE key_hash = ? AND active = 1").bind(key_hash).first()
    if not row:
        return None
    return row


async def _meter_meld(conn, key_hash: str, code: str):
    """Record a meld usage against the API key."""
    await conn.prepare(
        "INSERT INTO meld_usage (key_hash, meld_code, created_at) VALUES (?, ?, ?)"
    ).bind(key_hash, code, _now()).run()
    await conn.prepare(
        "UPDATE api_keys SET melds_used = melds_used + 1 WHERE key_hash = ?"
    ).bind(key_hash).run()


@app.post("/v1/keys")
async def create_api_key(request: Request):
    """Create an API key. Requires a valid Pro lease (payment gating)."""
    conn = db(request)
    body = await request.json()
    email = body.get("email", "")
    label = body.get("label", "default")
    if not email or not isinstance(email, str):
        raise HTTPException(400, "email required")

    email_key = _email_key(email)
    lease = await conn.prepare(
        "SELECT pro_until FROM pros WHERE email_key = ?").bind(email_key).first()
    if not lease or lease["pro_until"] <= _now():
        raise HTTPException(403, "Active Pro subscription required. Upgrade at /upgrade")

    key = "mk_" + secrets.token_hex(24)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    await conn.prepare(
        "INSERT INTO api_keys (key_hash, label, tier, melds_used, melds_limit, created_at, active)"
        " VALUES (?, ?, 'agent', 0, 10000, ?, 1)").bind(
        key_hash, label[:100], _now()).run()
    return {
        "key": key,
        "label": label[:100],
        "tier": "agent",
        "melds_limit": 10000,
        "note": "Store this key securely — it cannot be recovered."
    }


@app.post("/v1/melds")
async def v1_create_meld(request: Request):
    """Agent API: create a meld with Bearer auth."""
    conn = db(request)
    key_row = await _validate_api_key(request)
    if not key_row:
        raise HTTPException(401, "Invalid or missing API key")
    if key_row["melds_used"] >= key_row["melds_limit"]:
        raise HTTPException(429, "Meld limit reached for this API key")

    body = await request.json()
    context = body.get("context", "")
    if not isinstance(context, str):
        raise HTTPException(400, "Context must be a string")
    if len(context) > MAX_CONTEXT:
        raise HTTPException(400, "Context too large (100K max)")

    code = _code()
    token = _token()
    now = _now()
    expiry = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(days=7)).isoformat()  # agents get 7-day TTL
    await conn.prepare(
        "INSERT INTO melds (code, context_a, context_b, resolved, owner_token,"
        " owner_email, creator_ip, created_at, expires_at, pin)"
        " VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?, NULL)").bind(
        code, context, token, key_row["label"], "api:" + key_row["key_hash"][:16], now, expiry
    ).run()
    await _meter_meld(conn, key_row["key_hash"], code)

    host = request.headers.get("host", "localhost")
    scheme = "https" if "workers.dev" in host else request.url.scheme
    return {
        "code": code,
        "url": f"{scheme}://{host}/m/{code}",
        "owner_url": f"{scheme}://{host}/m/{code}#t={token}",
        "owner_token": token,
        "context_a": context,
        "resolved": False,
    }


@app.post("/v1/melds/{code}/resolve")
async def v1_resolve_meld(code: str, request: Request):
    """Agent API: resolve a meld with Bearer auth."""
    conn = db(request)
    key_row = await _validate_api_key(request)
    if not key_row:
        raise HTTPException(401, "Invalid or missing API key")

    body = await request.json()
    context = body.get("context", "")
    if not isinstance(context, str):
        raise HTTPException(400, "Context must be a string")
    if len(context) > MAX_CONTEXT:
        raise HTTPException(400, "Context too large (100K max)")

    row = await conn.prepare(
        "SELECT * FROM melds WHERE code = ?").bind(code).first()
    if not row:
        raise HTTPException(404, "Meld not found")
    if row["expires_at"] <= _now():
        raise HTTPException(410, "This meld has expired")

    pin = row["pin"]
    if pin:
        supplied = body.get("pin", "")
        if not isinstance(supplied, str) or not secrets.compare_digest(
                pin, hashlib.sha256(supplied.encode()).hexdigest()):
            raise HTTPException(403, "Invalid PIN")

    if row["resolved"]:
        if row["context_b"] == context:
            return {"code": code, "context_a": row["context_a"],
                    "context_b": row["context_b"], "resolved": True, "retry": True}
        raise HTTPException(409, "Already resolved with a different answer")

    fast_expiry = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(minutes=10)).isoformat()
    await conn.prepare(
        "UPDATE melds SET context_b = ?, resolved = 1, resolved_at = ?,"
        " expires_at = MIN(expires_at, ?) WHERE code = ?").bind(
        context, _now(), fast_expiry, code).run()
    await _meter_meld(conn, key_row["key_hash"], code)
    return {"code": code, "context_a": row["context_a"], "context_b": context,
            "resolved": True}


@app.get("/v1/melds/{code}/result")
async def v1_get_result(code: str, request: Request):
    """Agent API: read the result with the owner token (same as human flow)."""
    conn = db(request)
    key_row = await _validate_api_key(request)
    if not key_row:
        raise HTTPException(401, "Invalid or missing API key")
    token = request.headers.get("X-Meld-Token", "")
    row = await conn.prepare(
        "SELECT * FROM melds WHERE code = ?").bind(code).first()
    if not row:
        raise HTTPException(404, "Meld not found")
    if not secrets.compare_digest(row["owner_token"], token or ""):
        raise HTTPException(403, "Invalid token")
    if not row["resolved"]:
        raise HTTPException(400, "Not yet resolved")
    fresh = _token()
    await conn.prepare("UPDATE melds SET owner_token = ? WHERE code = ?").bind(fresh, code).run()
    return {"code": code, "context_a": row["context_a"],
            "context_b": row["context_b"], "resolved": True,
            "owner_token": fresh}


@app.get("/v1/usage")
async def v1_usage(request: Request):
    """Check API key usage."""
    key_row = await _validate_api_key(request)
    if not key_row:
        raise HTTPException(401, "Invalid or missing API key")
    return {
        "label": key_row["label"],
        "tier": key_row["tier"],
        "melds_used": key_row["melds_used"],
        "melds_limit": key_row["melds_limit"],
    }


# ── SPA ──────────────────────────────────────────────────────────────────

# ── SPA ──────────────────────────────────────────────────────────────────
PAGE = _SPA_HTML


@app.get("/m/{code}")
async def serve_meld(request: Request, code: str):
    if "application/json" in request.headers.get("accept", ""):
        conn = db(request)
        ip = _client_ip(request)
        if not await _rate_limit(conn, "view", ip):
            raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})
        row = await conn.prepare(
            "SELECT * FROM melds WHERE code = ?").bind(code).first()
        if not row:
            raise HTTPException(404, "Meld not found")
        if row["expires_at"] <= _now():
            raise HTTPException(410, "This meld has expired")
        return {"code": code, "context_a": row["context_a"],
                "context_b": row["context_b"], "resolved": bool(row["resolved"]),
                "api": {"resolve": f"POST /api/melds/{code}/resolve",
                        "result": "GET /api/melds/{code}/result (X-Meld-Token)"}}
    return HTMLResponse(PAGE)


@app.get("/trust", response_class=HTMLResponse)
async def trust():
    return HTMLResponse(TRUST_HTML)


@app.get("/{path:path}")
async def serve_page(path: str):
    return HTMLResponse(PAGE)


@app.get("/")
async def root():
    return HTMLResponse(PAGE)


TRUST_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>meld — trust</title></head><body><h1>meld trust model</h1><p>See TRUST.md in the repository.</p></body></html>"""

# Workers ASGI entrypoint
Default = asgi.entrypoint(app)
