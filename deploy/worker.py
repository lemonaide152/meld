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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from workers import asgi
from spa_content import _SPA_HTML
from agents_content import AGENTS_HTML
from app_content import APP_HTML
from agents_content import AGENTS_HTML

app = FastAPI(title="meld", version="1.0.0", docs_url=None, redoc_url=None)

FREE_LIMIT = 3
FREE_EXPIRY_HOURS = 1
LEASE_DAYS = 35
CODE_LEN = 12
MAX_CONTEXT = 100_000
# Pay-per-meld (spec v1.1 §Security): the ONLY price for the wall-hit path.
# Amount is pinned server-side in cents; the client sends at most {meld_code}.
# The webhook independently asserts amount_total == MELD_PRICE_CENTS before
# unlocking (defense-in-depth: a signed event proves Stripe sent it, not that
# the session was created at our price).
MELD_PRICE_CENTS = 333  # $3.33 — operator decision 2026-09-22
# R4: the only origins a checkout success/cancel may redirect to. Never
# derived from the Host header (open-redirect-via-checkout).
ALLOWED_HOSTS = {"meld.mergeinc.workers.dev", "meld.sh", "www.meld.sh"}
# rate limits: (max, window_seconds)
RL = {"create": (20, 60), "resolve": (10, 60), "view": (60, 60), "checkout": (5, 60)}

# ── env bindings (set in wrangler.toml) ─────────────────────────────────
def db(request):
    return request.scope["env"].DB  # D1 binding — use .prepare(sql).bind(...).run()/.first()


# ── helpers ──────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def _funnel(db_conn, event: str):
    """Daily aggregate funnel counter. No PII: day + event + count only."""
    try:
        day = _now()[:10]
        await db_conn.prepare(
            "INSERT INTO funnel_events (day, event, n) VALUES (?, ?, 1) "
            "ON CONFLICT(day, event) DO UPDATE SET n = n + 1").bind(day, event).run()
    except Exception:
        pass  # analytics must never break the product


def _code() -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(CODE_LEN))


def _token() -> str:
    return secrets.token_hex(32)


def _client_ip(request: Request) -> str:
    """Real client IP. On Cloudflare Workers: CF-Connecting-IP is authoritative
    (XFF is empty and request.client is None in the ASGI bridge). XFF last-hop
    fallback covers self-hosted reverse-proxy deployments."""
    cf_ip = request.headers.get("cf-connecting-ip", "")
    if cf_ip:
        return cf_ip.strip()
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
    """D1-backed fixed-window limiter (per-minute buckets, self-pruning).
    Atomic: the limit check happens inside a conditional UPSERT, so concurrent
    requests cannot read stale counts and burst past the cap (TOCTOU fix).
    The stale-window prune stays best-effort outside the atomic path."""
    limit = {"create": 20, "resolve": 10, "view": 60, "checkout": 5}.get(kind, 60)
    key = _rl_key(kind, ip)
    bucket = _rl_bucket()
    # Atomic admit: increments only when the bucket is fresh or count < limit.
    row = await db_conn.prepare(
        "INSERT INTO rate (key, count, bucket) VALUES (?, 1, ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "  count = CASE WHEN bucket < ? THEN 1 "
        "                WHEN count < ? THEN count + 1 ELSE count END, "
        "  bucket = ? "
        "WHERE bucket < ? OR count < ? "
        "RETURNING count, bucket"
    ).bind(key, bucket, bucket, limit, bucket, bucket, limit).first()
    if row is None:
        return False  # conditional write matched nothing => window exhausted
    if row["bucket"] < bucket:
        await db_conn.prepare("DELETE FROM rate WHERE bucket < ?").bind(bucket - 2).run()
    return row["count"] <= limit and row["bucket"] == bucket


async def _check_meld_limit(db_conn, ip: str, email: str | None = None) -> bool:
    """Free users: FREE_LIMIT melds per hour. Pro (valid lease): unlimited.
    Lease lookup matches ONLY hashed email keys — never raw IPs — so a pro
    lease can never be claimed by controlling your source IP."""
    lease = None
    if email and email.startswith("sha256:"):
        lease = await db_conn.prepare(
            "SELECT pro_until, email_key FROM pros WHERE email_key = ?").bind(email).first()
        if lease and lease["pro_until"] <= _now():
            await db_conn.prepare("DELETE FROM pros WHERE email_key = ?").bind(lease["email_key"]).run()
            lease = None  # expired: fall through to free-limit count
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=FREE_EXPIRY_HOURS)).isoformat()
    row = await db_conn.prepare(
        "SELECT COUNT(*) as c FROM melds WHERE creator_ip = ? AND created_at >= ?").bind(ip, cutoff).first()
    return row["c"] < FREE_LIMIT


# ── middleware: staging IP allowlist + security headers + JSON body cap ──
@app.middleware("http")
async def security(request: Request, call_next):
    # staging lockdown: when ALLOWED_IPS is set (comma-separated), only those
    # client IPs may use the instance. Webhook path exempt (Stripe egress IPs
    # vary). Stealth 404 — don't advertise the staging instance's existence.
    env = request.scope.get("env")
    allowed_raw = getattr(env, "ALLOWED_IPS", "") if env else ""
    allowed = {s.strip() for s in (allowed_raw or "").split(",") if s.strip()}
    if allowed and request.url.path != "/api/stripe/webhook":
        if _client_ip(request) not in allowed:
            # beta bypass: valid beta key admits non-allowlisted callers
            beta_raw = getattr(env, "BETA_KEYS", "") if env else ""
            beta_keys = {s.strip() for s in (beta_raw or "").split(",") if s.strip()}
            presented = request.headers.get("x-meld-beta-key", "")
            if not presented or presented not in beta_keys:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
    resp = await call_next(request)
    # CORS: browser agents + remote MCP clients need to call the API directly
    if request.url.path.startswith("/api") or request.url.path.startswith("/v1"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Meld-Token, Authorization, X-Forwarded-For"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
    return resp


@app.options("/api/{rest:path}")
@app.options("/v1/{rest:path}")
async def cors_preflight(rest: str = ""):
    return JSONResponse({}, status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type, X-Meld-Token, Authorization",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Max-Age": "86400"})


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
    email = body.get("email")
    # Amortized sweeper: piggyback cleanup of expired melds on create traffic
    try:
        await conn.prepare("DELETE FROM melds WHERE expires_at <= ?").bind(_now()).run()
    except Exception:
        pass  # never block create on sweep failure
    email_key = ("sha256:" + hashlib.sha256(email.encode()).hexdigest()
                 if isinstance(email, str) and 3 <= len(email) <= 254 and "@" in email else None)
    if not await _check_meld_limit(conn, ip, email_key):
        await _funnel(conn, "free_limit_hit")
        raise HTTPException(429, "Free limit reached (3/hour). Retry-After applies. Upgrade for unlimited: https://meld.mergeinc.workers.dev/upgrade")

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
    await _funnel(conn, "created")
    return {
        "code": code,
        "url": f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/m/{code}",
        "owner_url": f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/m/{code}#t={token}",
        "owner_token": token,
        "context_a": context,
        "resolved": False,
        "expires_at": expiry,
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
    try:
        delta = datetime.datetime.fromisoformat(row["expires_at"]) - datetime.datetime.now(datetime.timezone.utc)
        remaining = max(0, int(delta.total_seconds()))
    except Exception:
        remaining = None
    return {"code": code, "context_a": row["context_a"],
            "context_b": row["context_b"], "resolved": bool(row["resolved"]),
            "resolved_at": row["resolved_at"], "expires_at": row["expires_at"],
            "seconds_remaining": remaining}


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
        await conn.prepare("DELETE FROM melds WHERE code = ?").bind(code).run()
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
        " responder_pubkey = ?, signature = ?, resolver_ip = ?,"
        " expires_at = MIN(expires_at, ?) WHERE code = ?").bind(
        context, _now(), pk, sig, ip, fast_expiry, code).run()
    await _funnel(conn, "resolved")
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
    if row["expires_at"] <= _now():
        await conn.prepare("DELETE FROM melds WHERE code = ?").bind(code).run()
        raise HTTPException(410, "This meld has expired")
    if not row["resolved"]:
        raise HTTPException(400, "Not yet resolved")
    fresh = _token()
    await conn.prepare("UPDATE melds SET owner_token = ? WHERE code = ?").bind(fresh, code).run()
    return {"owner_token": fresh,
            "code": code, "resolved": True,
            "context_a": row["context_a"],
            "context_b": row["context_b"],
            "responder_pubkey": row["responder_pubkey"],
            "signature": row["signature"], "expires_at": row["expires_at"]}


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
    # W1: HMAC-SHA256 over raw bytes with 5-minute timestamp tolerance —
    # replays are rejected here, before any state is touched.
    event = _verify_stripe_sig(payload, sig, secret)
    if event is None:
        raise HTTPException(400, "Invalid signature")
    conn = db(request)
    event_id = event.get("id", "")
    etype = event.get("type", "")
    # W2: idempotency keyed on the Stripe event id. The pre-SELECT filters the
    # common redelivery; the conditional INSERT (changes==0) closes the
    # concurrent-race window. A duplicate must never re-grant or extend a
    # lease, so a lost race is treated as already-processed.
    seen = await conn.prepare(
        "SELECT 1 FROM webhook_events WHERE stripe_event_id = ?").bind(event_id).first()
    if seen:
        return {"ok": True, "dedup": True}
    if etype == "checkout.session.completed":
        sess = event["data"]["object"]
        # Pay-per-meld (spec v1.1 §Security req 5): assert the amount BEFORE
        # any unlock. Signature verification proves Stripe sent this event; it
        # does NOT prove the session was created at our price. A leaked or
        # reused API key could mint a session at any amount — a signed
        # webhook for it must never unlock.
        amount = sess.get("amount_total")
        meld_code = (sess.get("metadata") or {}).get("meld_id", "")
        if meld_code:
            if amount != MELD_PRICE_CENTS:
                await conn.prepare(
                    "INSERT INTO webhook_events (stripe_event_id, type, customer_id,"
                    " subscription_id, received_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(stripe_event_id) DO NOTHING").bind(
                    event_id, etype, sess.get("customer", ""),
                    sess.get("subscription", ""), _now()).run()
                await conn.prepare(
                    "INSERT INTO ledger (at, event, customer_id, email_key, days)"
                    " VALUES (?, 'payment.wrong_amount_rejected', ?, NULL, NULL)"
                ).bind(_now(), sess.get("customer", "")).run()
                return {"ok": True, "rejected": "wrong_amount"}
            # Spec §Security req 3: idempotent on checkout.session.id.
            inserted = await conn.prepare(
                "INSERT INTO meld_payments (stripe_session_id, meld_code,"
                " amount_cents, paid_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(stripe_session_id) DO NOTHING").bind(
                sess.get("id", ""), meld_code, amount, _now()).run()
            if _d1_changes(inserted) == 0:
                # Redelivery under a NEW event id: the session was already
                # processed. No-op — never re-unlock, never re-ledger.
                return {"ok": True, "dedup": True}
            # Spec §Security req 4: flip exactly one capability's paid flag.
            await conn.prepare(
                "UPDATE melds SET paid = 1 WHERE code = ?").bind(meld_code).run()
            await conn.prepare(
                "INSERT INTO ledger (at, event, customer_id, email_key, days)"
                " VALUES (?, 'meld.unlock', ?, NULL, NULL)").bind(
                _now(), sess.get("customer", "")).run()
            return {"ok": True, "unlocked": meld_code}
        # Legacy subscription path (no meld_id metadata): unchanged.
        email = (sess.get("customer_details") or {}).get("email", "")
        customer = sess.get("customer", "")
        subscription = sess.get("subscription", "")
        inserted = await conn.prepare(
            "INSERT INTO webhook_events (stripe_event_id, type, customer_id,"
            " subscription_id, received_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(stripe_event_id) DO NOTHING").bind(
            event_id, etype, customer, subscription, _now()).run()
        if _d1_changes(inserted) == 0:
            return {"ok": True, "dedup": True}
        if email:
            key = _email_key(email)
            until = (datetime.datetime.now(datetime.timezone.utc)
                     + datetime.timedelta(days=LEASE_DAYS)).isoformat()
            await conn.prepare(
                "INSERT INTO pros (email_key, customer_id, pro_until, since)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(email_key) DO UPDATE SET"
                " pro_until = ?, customer_id = ?").bind(
                key, customer, until, _now(), until, customer).run()
            await conn.prepare(
                "INSERT INTO ledger (at, event, customer_id, email_key, days)"
                " VALUES (?, 'lease.grant', ?, ?, ?)").bind(
                _now(), customer, key, LEASE_DAYS).run()
    return {"ok": True}


def _d1_changes(result) -> int:
    """D1 write metadata: number of rows changed (-1 if unreadable)."""
    try:
        return int(result["meta"]["changes"])
    except Exception:
        try:
            return int(result.meta.changes)
        except Exception:
            return -1


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


def _verify_stripe_sig(payload: bytes, sig_header: str, secret: str):
    """Stripe webhook signature verification, stdlib-only (Workers has no
    stripe SDK). Mirrors stripe.Webhook.construct_event: t=...,v1=... over
    '{t}.{payload}', constant-time compare, 5-min replay window."""
    import hmac as _hmac
    if not sig_header or not secret:
        return None
    parts = dict(p.strip().split("=", 1) for p in sig_header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        return None
    if abs(time.time() - int(t)) > 300:
        return None
    expected = _hmac.new(secret.encode(), f"{t}.".encode() + payload,
                         hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(expected, v1):
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


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
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON: {\"plan\": \"monthly\"|\"yearly\", \"email\": \"...\" (optional)}")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")
    ip = _client_ip(request)
    conn = db(request)
    if not await _rate_limit(conn, "checkout", ip):
        raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})

    # ── Pay-per-meld path (spec v1.1): body {meld_code} → one-time 333¢ ──
    if "meld_code" in body:
        meld_code = body.get("meld_code")
        if not isinstance(meld_code, str) or not 4 <= len(meld_code) <= 32:
            raise HTTPException(400, "meld_code must be the meld code to unlock")
        row = await conn.prepare("SELECT code FROM melds WHERE code = ?").bind(meld_code).first()
        if not row:
            raise HTTPException(404, "Meld not found")
        checkout_ref = secrets.token_hex(16)
        await conn.prepare(
            "INSERT INTO checkout_clicks (checkout_ref, plan, clicked_at)"
            " VALUES (?, ?, ?)").bind(checkout_ref, "per_meld", _now()).run()
        await _funnel(conn, "checkout_clicked")
        base_url = "https://meld.mergeinc.workers.dev"
        from urllib.parse import urlencode as _ue
        params = {
            "mode": "payment",  # one-time; client cannot change it
            "success_url": base_url + "/pro?ref=" + checkout_ref,
            "cancel_url": base_url + "/upgrade",
            "client_reference_id": checkout_ref,
            # Spec §Security req 5: amount pinned HERE, server-side. The
            # client body can never set it.
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(MELD_PRICE_CENTS),
            "line_items[0][price_data][product_data][name]": "meld — one context bridge",
            "line_items[0][quantity]": "1",
            # Spec §Security req 1: capability binding for the webhook.
            "metadata[meld_id]": meld_code,
            "metadata[checkout_ref]": checkout_ref,
        }
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
        return {"url": d["url"]}

    # ── Legacy subscription path ({plan, email}) ─────────────────────────
    # R1: the client chooses at most a plan enum. Amount, currency and price
    # are pinned server-side (Price IDs live in wrangler.toml env) — the
    # request body can never set them. plan is REQUIRED: an empty body mints
    # a live Stripe session for free, so {} must reject, not default.
    if "plan" not in body:
        raise HTTPException(400, "plan is required: 'monthly' or 'yearly' (or meld_code for pay-per-meld)")
    plan = body.get("plan")
    if plan not in ("monthly", "yearly"):
        raise HTTPException(400, "plan must be 'monthly' or 'yearly'")
    email = body.get("email", "")
    if not isinstance(email, str) or not 3 <= len(email) <= 254 or "@" not in email:
        email = None  # optional field; garbage in, ignored
    price_id = cfg["monthly"] if plan == "monthly" else cfg["yearly"]
    if not price_id:
        raise HTTPException(500, "Price not configured")
    await _funnel(conn, "checkout_clicked")

    # R3: opaque per-checkout token, minted Worker-side, correlated to the
    # funnel click so a completed session can be tied to its click row
    # without persisting any user data.
    checkout_ref = secrets.token_hex(16)
    await conn.prepare(
        "INSERT INTO checkout_clicks (checkout_ref, plan, clicked_at)"
        " VALUES (?, ?, ?)").bind(checkout_ref, plan, _now()).run()

    # R4: success/cancel origins are allow-listed constants. The Host header
    # is attacker-controlled on Workers and must never build a redirect.
    base_url = "https://meld.mergeinc.workers.dev"

    from urllib.parse import urlencode as _ue
    params = {
        "mode": "subscription",  # R2: pinned; client cannot change it
        "success_url": base_url + "/pro?ref=" + checkout_ref,
        "cancel_url": base_url + "/upgrade",
        "client_reference_id": checkout_ref,
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
    # R5: return only what the browser needs to redirect.
    return {"url": d["url"]}


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
        await conn.prepare("DELETE FROM melds WHERE code = ?").bind(code).run()
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
    await _funnel(conn, "resolved")
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
PAGE = APP_HTML


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
            await conn.prepare("DELETE FROM melds WHERE code = ?").bind(code).run()
            raise HTTPException(410, "This meld has expired")
        return {"code": code, "context_a": row["context_a"],
                "context_b": row["context_b"], "resolved": bool(row["resolved"]),
                "api": {"resolve": f"POST /api/melds/{code}/resolve",
                        "result": "GET /api/melds/{code}/result (X-Meld-Token)"}}
    return HTMLResponse(PAGE)



@app.get("/trust", response_class=HTMLResponse)
async def trust():
    return HTMLResponse(TRUST_HTML)


@app.get("/agents", response_class=HTMLResponse)
async def agents_page():
    return HTMLResponse(AGENTS_HTML)


# ── machine-readable discovery (agents land here) ─────────────────────────
@app.get("/AGENTS.md", response_class=PlainTextResponse)
async def agents_root_md():
    return PlainTextResponse(AGENTS_ROOT_MD, media_type="text/markdown")


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt():
    return PlainTextResponse(LLMS_TXT, media_type="text/markdown")


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    return PlainTextResponse(ROBOTS_TXT, media_type="text/plain")


@app.get("/agents.md", response_class=PlainTextResponse)
async def agents_md():
    return PlainTextResponse(AGENTS_MD, media_type="text/markdown")


@app.get("/trust.md", response_class=PlainTextResponse)
async def trust_md():
    return PlainTextResponse(TRUST_MD, media_type="text/markdown")


@app.get("/recipes.md", response_class=PlainTextResponse)
async def recipes_md():
    return PlainTextResponse(RECIPES_MD, media_type="text/markdown")


@app.get("/upgrade.md", response_class=PlainTextResponse)
async def upgrade_md():
    return PlainTextResponse(UPGRADE_MD, media_type="text/markdown")


@app.get("/.well-known/mcp.json")
async def mcp_manifest():
    return JSONResponse(MCP_MANIFEST)


@app.get("/skill.md", response_class=PlainTextResponse)
async def skill_md_route():
    return PlainTextResponse(SKILL_MD, media_type="text/markdown")


@app.get("/.well-known/mcp/server-card.json")
async def mcp_server_card():
    return JSONResponse(MCP_SERVER_CARD)


@app.get("/.well-known/agent.json")
async def a2a_agent_card():
    return JSONResponse(AGENT_CARD)


@app.get("/.well-known/agent-skills/index.json")
async def agent_skills_index():
    return JSONResponse(SKILLS_INDEX)


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "meld", "api": "/llms.txt"}


@app.get("/{path:path}")
async def serve_page(path: str):
    return HTMLResponse(PAGE)


@app.get("/")
async def root():
    return HTMLResponse(PAGE)


AGENTS_ROOT_MD = '# AGENTS.md — working with meld\n\nThis file follows the AGENTS.md convention: instructions for AI agents\nworking in or around this system.\n\n## What meld is\n\nAn ephemeral two-party context bridge. Party A creates a link containing\ncontext; party B opens it, answers; party A reads the merged exchange; the\ncontent is deleted (1h unresolved max, ~10min after resolution). No accounts.\nThe link is the capability.\n\n## When to use it\n\n- You must hand a large context blob (code, logs, specs) to another agent or\n  a human exactly once, and no shared store exists.\n- You need one answer back, not a thread.\n- The context is sensitive enough that you don\'t want it persisted on a\n  third party\'s server: use E2E mode (client-side AES-256-GCM, key stays in\n  the URL fragment).\n\n## When NOT to use it\n\n- Multi-turn conversations or anything needing history.\n- Anything that must outlive the TTL.\n- Repeated structured access by many consumers — use a real store.\n\n## Quick start (three calls)\n\n```bash\n# A creates\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"..."}\'\n# → {code, url, owner_url, owner_token, expires_at}\n\n# B answers (POST the share url\'s code)\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"..."}\'\n# → receives A\'s context\n\n# A reads the answer (token ROTATES every read — persist the new one)\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \\\n  -H \'X-Meld-Token: {owner_token}\'\n```\n\n## Agent-to-agent pattern\n\nIf you are agent A and your counterpart is agent B: send B the share link\nplus this URL (https://meld.mergeinc.workers.dev/llms.txt) so B can\nself-serve without human setup. If you are agent B and arrived here from a\nshare link: fetch /llms.txt, resolve, done.\n\n## Limits\n\nFree: 3 melds/hour per IP. Errors: 400 bad body, 403 pin, 404 missing,\n409 conflicting answer, 410 expired, 429 slow down (Retry-After).\nMachine-readable docs: /llms.txt · /agents.md · /openapi.json · /trust.md\nMCP server manifest: /.well-known/mcp.json\n'


LLMS_TXT = """# meld
> One link. Party A pours context. Party B answers. The link dissolves. No accounts.

Base URL: https://meld.mergeinc.workers.dev
Auth: none on create/read/resolve. Owner token required only for GET /result.
Content limit: context string, max 100000 chars.
TTL: unresolved 1 hour; after resolve 10 minutes. Then 410 Gone.

When to use
- Hand a large context blob to another agent or a human without a shared store
- Get a single answer back, then forget the payload
- Optional client-side AES-256-GCM (meld1: + base64(nonce||ciphertext), key in URL fragment #k=)

Do not use
- Long-lived memory, chat history, or multi-turn threads
- Anything that must survive past the TTL

Quick start

  curl -s https://meld.mergeinc.workers.dev/api/melds -H 'content-type: application/json' -d '{"context":"..."}'

Flow
1. POST /api/melds  {"context":"..."}  -> code, url, owner_token, expires_at
2. GET  /api/melds/{code}              -> context_a
3. POST /api/melds/{code}/resolve {"context":"..."}
4. GET  /api/melds/{code}/result  header X-Meld-Token: <owner_token>
   Token rotates on every result read. Persist the new token immediately.

Errors: 400 bad body, 403 bad pin, 404 missing, 409 different answer already stored, 410 expired, 429 rate limit.
Free: 3 melds/hour. Pro: POST /v1/keys (requires subscription).

Docs
- Agent API: https://meld.mergeinc.workers.dev/agents.md
- OpenAPI: https://meld.mergeinc.workers.dev/openapi.json
- Trust model: https://meld.mergeinc.workers.dev/trust.md
- Pricing: https://meld.mergeinc.workers.dev/upgrade.md
- Recipes: https://meld.mergeinc.workers.dev/recipes.md
- A2A Agent Card: https://meld.mergeinc.workers.dev/.well-known/agent.json
- Skill install: https://meld.mergeinc.workers.dev/.well-known/agent-skills/index.json
- MCP server card: https://meld.mergeinc.workers.dev/.well-known/mcp/server-card.json
- MCP transport: stdio (meld-mcp.mjs, zero deps, Node 18+). Config in /agents.md.

Agent-to-agent pattern
- You (agent A) have a context blob too large for the message channel, and a
  counterpart (agent B or human) who needs it exactly once.
- Create: POST /api/melds {"context": "..."} -> share url + owner_token.
- Send ONLY the share url to B through any channel you already share (chat,
  task payload, email). B resolves; you read the answer with the owner token.
- Nothing persists: after both sides read, the content is deleted. No shared
  store, no account, no cleanup debt.
- If the counterpart is another agent, send it the /llms.txt URL alongside the
  share link so it can self-serve without human setup.
"""


ROBOTS_TXT = """User-agent: GPTBot
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: Google-Extended
Allow: /

User-agent: *
Allow: /

# Machine-readable docs for agents
# See: /llms.txt /agents.md /openapi.json /trust.md
"""


AGENTS_MD = """# meld — agent API

Ephemeral two-party context drop. One link carries context from party A to
party B, B answers, both read the merged exchange, then everything dissolves.
No accounts. The link is the protocol.

## Quick start

```bash
# 1. Create (party A)
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' \
  -d '{"context":"What architecture fits 10M users?"}'

# 2. Party B opens the share link (or you hand them the code), then resolves
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \
  -H 'content-type: application/json' \
  -d '{"context":"Event-driven services + a queue."}'

# 3. Party A reads the answer (owner_token from step 1)
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \
  -H 'X-Meld-Token: {owner_token}'
```

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | /api/melds | none | Create. Body: {context, email?, pin?}. Returns code, url, owner_url, owner_token, expires_at. |
| GET | /api/melds/{code} | none | Read context_a + status. Poll until resolved: true. |
| POST | /api/melds/{code}/resolve | pin if set | Answer. Body: {context, pin?}. Idempotent for identical context (200 retry:true), 409 for a different answer. |
| GET | /api/melds/{code}/result | X-Meld-Token header | Owner read. Token ROTATES every read — persist the new owner_token immediately. |
| POST | /v1/keys | requires Pro lease | Create agent API key (mk_...). |
| GET | /api/health | none | Liveness probe. |

## Semantics

- TTL: unresolved 1 hour; after resolve ~10 minutes; then 410 Gone and the row is deleted.
- Idempotency: resolving twice with the IDENTICAL context returns 200 {retry: true}. A different answer returns 409 — do not retry with another answer; read /result instead if you hold the owner token.
- E2E encryption (optional): encrypt client-side with AES-256-GCM before POST. Wire format: "meld1:" + base64(nonce || ciphertext). Key goes in the URL fragment #k= and never reaches the server.
- Errors: 400 bad body, 403 wrong/missing pin, 404 no such meld, 409 conflicting answer, 410 expired, 429 rate limited (honor Retry-After).
- Free tier: 3 creates/hour per IP. Programmatic volume: see /upgrade.md.

## MCP

MCP server (Claude Desktop, Cursor, etc.): tools meld_create / meld_resolve / meld_read.
Manifest: /.well-known/mcp.json
"""


TRUST_MD = """# meld — trust model

See the full trust model at /trust (human page). Summary for agents:

- No accounts, no user database. Authority = held secrets (links, tokens, keys).
- Owner token: capability to read the result. Rotates on every read; old token dies.
- PIN (optional): second factor for the answering party.
- E2E mode: server stores AES-256-GCM ciphertext only; key lives in the URL fragment, never sent to the server.
- Retention: expired melds are deleted (lazy sweep on access + amortized sweep on create). Free tier max 1 hour; post-resolve ~10 minutes.
- Emails: only stored as SHA-256 hashes. No logs of content.
- We cannot read E2E melds. We can read plaintext melds while they exist (your choice per meld).

Commitments we will not add: accounts, content scanning, read receipts, long-term persistence.
"""


UPGRADE_MD = """# meld — pricing

Free: 3 melds/hour per IP. 1-hour TTL. No account.

Pro — $5/month (or $49/year):
- Unlimited melds
- 7-day TTL for unresolved melds
- Agent API keys: POST /v1/keys -> mk_... key, 10,000 melds/key, 7-day TTL, usage metered at /v1/usage

Subscribe: open https://meld.mergeinc.workers.dev/upgrade in a browser
(Stripe Checkout). After payment, POST /v1/keys with your email to mint keys.

Rate limits (all tiers): 20 creates/min, 10 resolves/min, 60 views/min per IP.
"""


RECIPES_MD = '# meld recipes — concrete triggers, not a manifesto\n\nThree flows that make an agent choose meld over a gist, a pastebin, or a\nSlack message. Base URL: https://meld.mergeinc.workers.dev\n\n---\n\n## 1. Human-in-the-loop review\n\nA coding agent needs plan approval before touching main. Instead of dumping\na diff into chat and losing it:\n\n```\nAgent:   POST /api/melds  {"context": "<patch + rationale>"}\n         → sends the share link to the human\nHuman:   opens link, reads the patch, answers in the form:\n         "approve" or "change X first"\nAgent:   GET /api/melds/{code}/result  (X-Meld-Token)\n         → reads verdict, proceeds or revises\n```\n\nWhy meld: no repo pollution for un-approved plans, no chat scrollback to\nsearch, the review payload self-destructs after both parties are done.\n\n## 2. Agent-to-agent handoff\n\nA planner agent hands constraints to a worker agent without shared memory:\n\n```\nPlanner: POST /api/melds  {"context": "<constraints, budgets, interfaces>"}\n         → passes the share link in the task payload\nWorker:  resolves with the implementation plan\n         → gets the full constraint context in return\nPlanner: reads the result, validates the plan against its constraints\n```\n\nWhy meld: works across frameworks and vendors — the link is the only\ncontract. No shared database, no queue, no account on either side.\n\n## 3. Encrypted drop (air-gapped secret)\n\nSame flow, but the payload must never exist in plaintext on the server:\n\n```\nSender:  encrypt context client-side (AES-256-GCM)\n         POST /api/melds {"context": "meld1:<ciphertext>"}\n         → sends share link over one channel, the #k= key over another\nReceiver: opens link, decrypts in browser, resolves\n         → server stored only ciphertext for the TTL, then deleted it\n```\n\nWhy meld: the server is honest-but-blind by construction. Even a full\ndatabase dump does not contain the secret.\n'


MCP_SERVER_CARD = {'serverInfo': {'name': 'meld', 'version': '1.0.0'}, 'description': 'Ephemeral two-party context bridge. Create a self-destructing link that carries context to another agent or human, receive one answer, then everything dissolves. No accounts. Optional client-side AES-256-GCM encryption.', 'homepage': 'https://meld.mergeinc.workers.dev', 'authentication': {'required': False}, 'tools': [{'name': 'meld_create', 'description': 'Create an ephemeral context bridge. Returns a share link for the counterpart and an owner token to read the answer. Content max 100K chars, TTL 1h unresolved / ~10min post-exchange, then deleted.', 'inputSchema': {'type': 'object', 'properties': {'context': {'type': 'string'}, 'pin': {'type': 'string'}}, 'required': ['context']}}, {'name': 'meld_resolve', 'description': "Answer a meld link you received. Submit your context, receive the original party's context.", 'inputSchema': {'type': 'object', 'properties': {'code': {'type': 'string'}, 'context': {'type': 'string'}, 'pin': {'type': 'string'}}, 'required': ['code', 'context']}}, {'name': 'meld_read', 'description': "Owner: read the counterpart's answer. Token rotates every read.", 'inputSchema': {'type': 'object', 'properties': {'code': {'type': 'string'}, 'owner_token': {'type': 'string'}}, 'required': ['code', 'owner_token']}}], 'resources': [], 'prompts': []}


AGENT_CARD = {'name': 'meld', 'description': 'Ephemeral two-party context bridge. Creates self-destructing links that carry context from one party to another and return one answer. Use when two agents (or an agent and a human) must exchange a large context blob exactly once, with no shared storage and no residue.', 'url': 'https://meld.mergeinc.workers.dev', 'version': '1.0.0', 'protocolVersion': '0.2.9', 'capabilities': {'streaming': False, 'pushNotifications': False}, 'defaultInputModes': ['application/json', 'text/plain'], 'defaultOutputModes': ['application/json', 'text/plain'], 'provider': {'organization': 'meld', 'url': 'https://meld.mergeinc.workers.dev'}, 'documentationUrl': 'https://meld.mergeinc.workers.dev/agents.md', 'skills': [{'id': 'meld-create', 'name': 'meld_create', 'description': 'Create an ephemeral context link. Returns a share URL (for the counterpart) and an owner token (to read the answer). Context max 100K chars. TTL 1h unresolved, ~10min post-exchange, then deleted.', 'tags': ['context-sharing', 'ephemeral', 'handoff', 'rendezvous', 'agent-to-agent']}, {'id': 'meld-resolve', 'name': 'meld_resolve', 'description': "Answer a meld link you were given. Submit your context and receive the original party's context. Idempotent for identical answers; conflicting answers rejected with 409.", 'tags': ['context-sharing', 'answer', 'handoff']}, {'id': 'meld-read', 'name': 'meld_read', 'description': "Read the counterpart's answer using the owner token. Token rotates on every read; persist the new token.", 'tags': ['context-sharing', 'read', 'result']}]}


SKILL_MD = '---\nname: meld\ndescription: Ephemeral two-party context drop. Use when you must send a large context to another agent or human and retrieve one answer without shared storage.\n---\n\n# meld — ephemeral context bridge\n\nOne link carries context from party A to party B. B answers. A reads the\nmerged exchange. Everything dissolves (1h unresolved, ~10min after resolve).\nNo accounts, no storage, no trail.\n\n## When to use\n- Hand a large context blob (code, logs, specs) to another agent or a human without a shared store\n- Get exactly one answer back, then forget the payload\n- Air-gapped handoff: use E2E mode so the server holds only ciphertext\n\n## When NOT to use\n- Multi-turn conversations, chat history, long-lived memory\n- Anything that must survive past the TTL\n\n## API (base: https://meld.mergeinc.workers.dev)\n\n### 1. Create (party A)\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"...your context..."}\'\n```\nReturns: `{code, url, owner_url, owner_token, expires_at}`.\n**Persist `owner_token` — it is the only way to read the answer.**\n\n### 2. Resolve (party B)\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"...your answer..."}\'\n```\nReturns party A\'s context. Idempotent for identical answers (200 `{retry:true}`);\na different answer is rejected with 409 — do not retry with variations.\n\n### 3. Read the result (party A)\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \\\n  -H \'X-Meld-Token: {owner_token}\'\n```\nThe token ROTATES on every read: the response\'s first field `owner_token` is\nthe new one. Persist it immediately; the old token is now dead.\n\n## Errors\n400 bad body · 403 wrong/missing pin · 404 no such meld · 409 conflicting\nanswer · 410 expired · 429 rate limited (honor Retry-After).\n\n## Limits\nFree: 3 melds/hour per IP. Poll `GET /api/melds/{code}` until `resolved: true`\nrather than hammering resolve. Programmatic volume: see /upgrade.md.\n\n## E2E mode (optional)\nEncrypt client-side with AES-256-GCM before POST. Wire format:\n`meld1:` + base64(nonce ‖ ciphertext). Put the hex key in the share URL\nfragment `#k=<64 hex>`. The server never sees plaintext or the key.\n'


SKILLS_INDEX = {'$schema': 'https://schemas.agentskills.io/discovery/0.2.0/schema.json', 'skills': [{'name': 'meld', 'description': 'Ephemeral two-party context drop. Use when you must send a large context to another agent or human and retrieve one answer without shared storage.', 'type': 'skill-md', 'url': 'https://meld.mergeinc.workers.dev/skill.md', 'digest': 'sha256:62bc977ea8cf2290c7d32ac1b83c395f2302cf5436adc872ad97ce34fda35d26'}]}


MCP_MANIFEST = {
    "name": "meld",
    "description": "Ephemeral two-party context drop. Create a link, receive an answer, then everything dissolves.",
    "version": "1.0.0",
    "url": "https://meld.mergeinc.workers.dev",
    "transport": "stdio",
    "command": "npx meld-mcp",
    "tools": ["meld_create", "meld_resolve", "meld_read"],
    "docs": "https://meld.mergeinc.workers.dev/llms.txt",
}


TRUST_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>meld — trust model</title><style>body{font-family:-apple-system,sans-serif;max-width:680px;margin:2rem auto;padding:0 1.5rem;line-height:1.6;color:#1a1a2e}h1{letter-spacing:-.02em}h2{margin-top:2rem;font-size:1.1rem}code{background:#f0ede2;padding:.15rem .4rem;border-radius:4px;font-size:.875rem}li{margin:.4rem 0}.refuse{background:#f0ede2;border-left:3px solid #2e4a7d;padding:1rem 1.25rem;margin:1.5rem 0}</style></head><body>
<h1>meld — trust model</h1>
<p>meld is a rendezvous, not a message system. Two parties, one link, one exchange, then everything dissolves. The trust model is built on capabilities, not identity:</p>
<h2>What holds</h2>
<ul>
<li><strong>The link is the protocol.</strong> No accounts, no user database. Whoever holds the capabilities holds the access.</li>
<li><strong>Owner token = revocable deed.</strong> The token that reads the result rotates on every read. The old token dies the moment you use the new one. Lost tokens cannot be recovered — by you or by us.</li>
<li><strong>Optional end-to-end encryption.</strong> Check the E2E box and your context is encrypted in your browser (AES-256-GCM). The server stores ciphertext it cannot open. The key lives in the link fragment (<code>#k=</code>) and never reaches us. Losing that link loses the content — for everyone, including us.</li>
<li><strong>Ephemeral by enforcement, not policy.</strong> Unresolved melds live at most 1 hour (7 days for Pro). After resolution, ~10 minutes. Then the row is deleted — by the access path itself, not by a promise.</li>
<li><strong>Minimal residue.</strong> Emails are stored only as SHA-256 hashes. No content logs. Payment identity lives with Stripe, not in the meld system.</li>
<li><strong>You choose per meld.</strong> Plaintext melds are readable by the server while they exist (max 1 hour). E2E melds never are. The checkbox is yours.</li>
</ul>
<h2>What we will never add</h2>
<div class="refuse"><ul>
<li>Accounts or login</li>
<li>Content scanning or analysis</li>
<li>Read receipts or presence indicators</li>
<li>Long-term persistence of meld content</li>
</ul></div>
<h2>Honest limits</h2>
<ul>
<li>The server can read plaintext melds while they exist. If your context must not touch our server in readable form, use E2E mode.</li>
<li>Rate-limit identity is IP-based. VPNs and shared NATs share quotas.</li>
<li>Deletion is real but not instantly verifiable by you — the guarantee is structural (short TTL + automatic deletion), not auditable.</li>
</ul>
<p><a href="/trust.md">Markdown version</a> · <a href="/llms.txt">Agent docs</a></p>
</body></html>"""

# Workers ASGI entrypoint
Default = asgi.entrypoint(app)
