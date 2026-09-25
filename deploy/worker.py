"""
meld — ephemeral context bridge. Cloudflare Workers port (D1-backed).
Faithful port of the audited meld.py trust model:
- capability model: code admits, PIN authenticates answerer, rotating token reads
- per-IP sliding-window rate limits, body caps, string-validated contexts
- opaque relay: server stores/relays context verbatim, content-blind
- pay-per-meld ($3.33 one-time) + append-only ledger; no accounts, no emails at rest
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
from agents_content import AGENTS_HTML
from app_content import APP_HTML
from agents_content import AGENTS_HTML

app = FastAPI(title="meld", version="1.0.0", docs_url=None, redoc_url=None)

FREE_LIMIT = 3
FREE_EXPIRY_HOURS = 1
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
    limit = {"create": 20, "resolve": 10, "view": 60, "checkout": 5, "keys": 5}.get(kind, 60)
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
        # Window exhausted: record the reject on the throttle ladder. One
        # ladder offense per hour-window regardless of how many per-minute
        # rejects follow (NAT guard, spec MELD-FREELIMIT-002 §3).
        await _register_wall_hit(db_conn, ip)
        return False  # conditional write matched nothing => window exhausted
    if row["bucket"] < bucket:
        await db_conn.prepare("DELETE FROM rate WHERE bucket < ?").bind(bucket - 2).run()
    return row["count"] <= limit and row["bucket"] == bucket


async def _check_meld_limit(db_conn, ip: str) -> bool:
    """Free tier: FREE_LIMIT melds CREATED per window (spec MELD-FREELIMIT-002:
    count creations, never live rows — melds are deleted on resolve/sweep, so
    a live-row COUNT is '3 concurrent', not '3 created/window', and the wall
    never fires for light users). No lease bypass exists: beyond the free
    tier, a meld costs $3.33 one-time (webhook-unlocked)."""
    window_key = str(int(time.time() // (FREE_EXPIRY_HOURS * 3600)))
    row = await db_conn.prepare(
        "INSERT INTO free_counts (ip, window_key, n) VALUES (?, ?, 1) "
        "ON CONFLICT(ip) DO UPDATE SET "
        "  n = CASE WHEN window_key < ? THEN 1 ELSE n + 1 END, "
        "  window_key = ? "
        "WHERE window_key < ? OR n < ? "
        "RETURNING n, window_key"
    ).bind(ip, window_key, window_key, window_key, window_key, FREE_LIMIT).first()
    return row is not None and row["window_key"] == window_key and row["n"] <= FREE_LIMIT


# ── MELD-FREELIMIT-002: escalating IP throttle (operator ladder) ────────
# Ladder per operator ruling: 1m → 10m → 1h → 24h → permanent (403).
# An "offense" = one wall-hit window with 2+ rejected creates, counted ONCE
# (NAT guard: a shared-IP pool collecting 429s walks one rung per window, no
# faster). permanent requires the 5th DISTINCT-window offense (meldmktg flag
# honored: fast consecutive 429s never lock out a NAT pool).
# Audit F1 (meldsec): a legit 10-person NAT office shares 3 melds/hour and
# collects 2+ wall hits EVERY window — a 5-window walk would permaban a
# whole office in 5 hours. Cap: the in-worker ladder CANNOT reach permanent.
# At offense 5+ the IP gets a rolling 24h ban (re-armed per window while the
# abuse persists). Permanent stays an operator action (manual ip_throttle
# UPDATE / CF WAF rule) or a future CF-level signal — never worker-automatic.
THROTTLE_LADDER = [60, 600, 3600, 86400]  # seconds
THROTTLE_WINDOW = FREE_EXPIRY_HOURS * 3600
PERMANENT_CAP_SECONDS = 86400  # audit F1: worker max = 24h, never permanent


async def _register_wall_hit(db_conn, ip: str) -> None:
    """A limit 429 (free wall OR per-minute limiter) was just emitted.
    Bookkeeping, atomic, best-effort — must never break the 429 in flight:
    - hit_window / wall_hits: hour-window of the last wall hit + how many
      rejects happened in it (reset on window roll).
    - offense_window: hour-window in which the last offense was recorded.
    - offense_count: ladder position; a window with 2+ hits earns ONE offense.
    - banned_until: rung duration for offense N; capped at 24h rolling for
      N >= 5 (worker cannot issue permanent bans — audit F1)."""
    try:
        window_key = str(int(time.time() // THROTTLE_WINDOW))
        row = await db_conn.prepare(
            "INSERT INTO ip_throttle (ip, offense_count, wall_hits, hit_window,"
            " offense_window, banned_until, permanent, updated_at)"
            " VALUES (?, 0, 1, ?, NULL, NULL, 0, ?)"
            " ON CONFLICT(ip) DO UPDATE SET"
            "  wall_hits = CASE WHEN hit_window < ? THEN 1 ELSE wall_hits + 1 END,"
            "  hit_window = ?,"
            "  updated_at = ?"
            " RETURNING wall_hits, offense_count, offense_window"
        ).bind(ip, window_key, _now(), window_key, window_key, _now()).first()
        if not row or int(row["wall_hits"]) < 2:
            return  # first reject in this window: NAT-guard, no offense
        if row["offense_window"] == window_key:
            return  # this window already offended
        offense = int(row["offense_count"]) + 1
        if offense >= len(THROTTLE_LADDER) + 1:
            # Ladder exhausted (F1): rolling 24h ban, re-armed each offending
            # window. permanent=0 — only an operator mints permanent bans.
            until = (datetime.datetime.now(datetime.timezone.utc)
                     + datetime.timedelta(seconds=PERMANENT_CAP_SECONDS)).isoformat()
            await db_conn.prepare(
                "UPDATE ip_throttle SET offense_count = ?, banned_until = ?,"
                " permanent = 0, offense_window = ?, updated_at = ? WHERE ip = ?"
            ).bind(offense, until, window_key, _now(), ip).run()
            return
        until = (datetime.datetime.now(datetime.timezone.utc)
                 + datetime.timedelta(seconds=THROTTLE_LADDER[offense - 1])).isoformat()
        await db_conn.prepare(
            "UPDATE ip_throttle SET offense_count = ?, banned_until = ?,"
            " offense_window = ?, updated_at = ? WHERE ip = ?"
        ).bind(offense, until, window_key, _now(), ip).run()
    except Exception:
        pass


async def _throttle_reject(db_conn, ip: str, path: str):
    """Middleware gate. Returns an HTTPException to raise, or None to pass.
    Webhook path is exempt (Stripe egress IPs vary)."""
    if path.startswith("/api/stripe/webhook"):
        return None
    try:
        row = await db_conn.prepare(
            "SELECT banned_until, permanent FROM ip_throttle WHERE ip = ?").bind(ip).first()
    except Exception:
        return None
    if not row:
        return None
    if row["permanent"]:
        return HTTPException(403, "Access denied")
    if row["banned_until"] and row["banned_until"] > _now():
        delta = datetime.datetime.fromisoformat(row["banned_until"]) - datetime.datetime.now(datetime.timezone.utc)
        return HTTPException(429, "Too many requests. Please slow down.",
                             headers={"Retry-After": str(max(1, int(delta.total_seconds())))})
    return None


async def _throttle_prune(db_conn) -> None:
    """Amortized on create traffic: non-permanent throttle rows and free-count
    windows older than 7 days are dead weight — prune. Permanent rows persist
    (bounded by real repeat offenders)."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=7)).isoformat()
    await db_conn.prepare("DELETE FROM ip_throttle WHERE permanent = 0 AND updated_at < ?").bind(cutoff).run()
    await db_conn.prepare("DELETE FROM free_counts WHERE window_key < ?").bind(
        str(int(time.time() // THROTTLE_WINDOW) - int(7 * 86400 // THROTTLE_WINDOW))).run()


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
    # MELD-FREELIMIT-002: escalating IP throttle gate (403 permanent /
    # 429 banned with Retry-After). Webhook path exempt inside.
    if request.url.path.startswith("/api") or request.url.path.startswith("/v1"):
        rej = await _throttle_reject(db(request), _client_ip(request), request.url.path)
        if rej is not None:
            return JSONResponse({"detail": rej.detail}, status_code=rej.status_code,
                                headers=dict(rej.headers or {}))
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
    # Amortized sweeper: piggyback cleanup of expired melds + throttle/counter
    # prune on create traffic
    try:
        await conn.prepare("DELETE FROM melds WHERE expires_at <= ?").bind(_now()).run()
        await _throttle_prune(conn)
    except Exception:
        pass  # never block create on sweep failure
    if not await _check_meld_limit(conn, ip):
        await _funnel(conn, "free_limit_hit")
        # MELD-FREELIMIT-002: wall hits feed the throttle ladder, one offense
        # per exhausted window (NAT guard: 2 hits in-window for rung 1).
        await _register_wall_hit(conn, ip)
        retry_after = str(max(1, int(
            (datetime.datetime.fromtimestamp(
                (int(time.time() // (FREE_EXPIRY_HOURS * 3600)) + 1)
                * FREE_EXPIRY_HOURS * 3600, datetime.timezone.utc)
             - datetime.datetime.now(datetime.timezone.utc)).total_seconds())))
        raise HTTPException(429, "Free limit reached (3 per window). Retry-After applies. $3.33 per meld beyond the free tier: https://meld.mergeinc.workers.dev/upgrade.md",
                            headers={"Retry-After": retry_after})

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
async def pro_status_gone():
    """SUPERSeded by no-pro directive: kept only to return a hard 404."""
    raise HTTPException(404, "Not found")


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
    # concurrent-race window. A duplicate must never re-unlock, so a lost
    # race is treated as already-processed.
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
                    " received_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(stripe_event_id) DO NOTHING").bind(
                    event_id, etype, sess.get("customer", ""), _now()).run()
                await conn.prepare(
                    "INSERT INTO ledger (at, event, customer_id)"
                    " VALUES (?, 'payment.wrong_amount_rejected', ?)"
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
                "INSERT INTO ledger (at, event, customer_id)"
                " VALUES (?, 'meld.unlock', ?)").bind(
                _now(), sess.get("customer", "")).run()
            return {"ok": True, "unlocked": meld_code}
        # No-pro directive: sessions WITHOUT meld_id metadata grant NOTHING —
        # record the event for idempotency + one ledger entry (log + no-op,
        # spec supersedure item 2). A legacy/subscription webhook can never
        # unlock anything again.
        await conn.prepare(
            "INSERT INTO webhook_events (stripe_event_id, type, customer_id,"
            " received_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(stripe_event_id) DO NOTHING").bind(
            event_id, etype, sess.get("customer", ""), _now()).run()
        await conn.prepare(
            "INSERT INTO ledger (at, event, customer_id)"
            " VALUES (?, 'payment.no_meld_id_ignored', ?)"
        ).bind(_now(), sess.get("customer", "")).run()
        return {"ok": True, "ignored": "no_meld_id"}
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
        raise HTTPException(400, "Body must be JSON: {\"meld_code\": \"<meld code to unlock>\"}")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")
    ip = _client_ip(request)
    conn = db(request)
    if not await _rate_limit(conn, "checkout", ip):
        raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})

    # SUPERSEDED (pay-per-meld only, spec v1.1 supersedure): the legacy
    # subscription path ({plan: monthly|yearly}) minted mode:subscription
    # Stripe sessions. It is removed — there is no subscription product.
    # Any body without meld_code (or with a plan key) is rejected here so
    # no subscription session can ever be minted again.
    if "meld_code" not in body:
        if "plan" in body:
            raise HTTPException(
                400, "Subscriptions were removed. Pay-per-meld only: "
                     "{\"meld_code\": \"<code>\"} → one-time $3.33.")
        raise HTTPException(400, "Body must be JSON: {\"meld_code\": \"<meld code to unlock>\"}")
    meld_code = body.get("meld_code")
    if not isinstance(meld_code, str) or not 4 <= len(meld_code) <= 32:
        raise HTTPException(400, "meld_code must be the meld code to unlock")
    row = await conn.prepare("SELECT code FROM melds WHERE code = ?").bind(meld_code).first()
    if not row:
        raise HTTPException(404, "Meld not found")
    checkout_ref = secrets.token_hex(16)
    await conn.prepare(
        "INSERT INTO checkout_clicks (checkout_ref, plan, clicked_at, clicker_ip)"
        " VALUES (?, ?, ?, ?)").bind(
            checkout_ref, "per_meld", _now(), _client_ip(request)).run()
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
        # Managed Payments requires a product tax code on price_data;
        # txcd_10501000 = SaaS, eligible for Managed Payments
        # (txcd_10500000 was rejected as ineligible; without any
        # tax code Stripe 500s session creation).
        "line_items[0][price_data][product_data][tax_code]": "txcd_10501000",
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


@app.get("/pro")
async def pro_page(request: Request):
    """Paid-meld success page after Stripe Checkout (one-time, no account)."""
    return HTMLResponse("""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>meld — paid</title>
<style>body{background:#0a0a0f;color:#e4e4f0;font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{text-align:center}.emoji{font-size:3rem;margin-bottom:.5rem}h1{font-size:1.5rem}p{color:#8888a0}a{color:#a78bfa}</style></head>
<body><div class="box"><div class="emoji">🎉</div><h1>Meld paid</h1><p>$3.33 received. Your meld is unlocked — nothing recurring.</p><p><a href="/">Create a meld</a></p></div></body></html>""")


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
    """Create an agent API key. Free, rate-limited; no account or payment."""
    conn = db(request)
    ip = _client_ip(request)
    # Throttled (5/min) like every wall-adjacent endpoint; abuse feeds the
    # ladder like any other wall hit.
    if not await _rate_limit(conn, "keys", ip):
        raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})
    body = await request.json()
    label = body.get("label", "default")
    if not isinstance(label, str):
        raise HTTPException(400, "label must be a string")

    key = "mk_" + secrets.token_hex(24)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    await conn.prepare(
        "INSERT INTO api_keys (key_hash, label, tier, melds_used, melds_limit, created_at, active, mint_ip, minted_at)"
        " VALUES (?, ?, 'agent', 0, 10000, ?, 1, ?, ?)").bind(
        key_hash, label[:100], _now(), ip, _now()).run()
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
async def health(request: Request):
    # No-PII paid-meld aggregate for revenue monitoring (meldfin v2):
    # counts only, no emails, no codes, no session ids.
    conn = db(request)
    try:
        row = await conn.prepare(
            "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0)"
            " FROM meld_payments").first()
        paid = {"paid_melds": int(row[0]), "gross_cents": int(row[1])}
    except Exception:
        paid = {"paid_melds": None, "gross_cents": None}
    return {"ok": True, "service": "meld", "api": "/llms.txt", **paid}


@app.get("/{path:path}")
async def serve_page(path: str):
    return HTMLResponse(PAGE)


@app.get("/")
async def root():
    return HTMLResponse(PAGE)


AGENTS_ROOT_MD = '# AGENTS.md — working with meld\n\nThis file follows the AGENTS.md convention: instructions for AI agents\nworking in or around this system.\n\n## What meld is\n\nmeld puts the context on a URL so neither side has to paste the block. Then the URL dies.\n\nAn ephemeral two-party context bridge. Party A creates one share URL; party B\nopens it and answers; either side can read both contexts via\n`GET /api/melds/{code}` after resolve; then the host serves 410 and the meld\nis gone (1h unresolved max, ~10min after resolution). No accounts. The URL\nis the capability.\n\n## When to use it\n\n- You must hand context to another agent or a human exactly once, and no\n  shared store exists.\n- You need one answer back, not a thread.\n- Optional: encrypt client-side before create if the server must not see\n  plaintext.\n\n## When NOT to use it\n\n- Multi-turn conversations or anything needing history.\n- Anything that must outlive the TTL.\n- Repeated structured access by many consumers — use a real store.\n\n## Quick start (preferred: one URL)\n\n```bash\n# A creates — share only the url\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"..."}\'\n# → {code, url, owner_url, owner_token, expires_at}\n#   owner_token is legacy (still returned); prefer the share url alone.\n\n# B answers\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"..."}\'\n\n# A (or anyone with the code) reads both sides after resolve\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n# → {context_a, context_b, resolved: true, …}\n```\n\n### Legacy owner read (still on the live host)\n\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \\\n  -H \'X-Meld-Token: {owner_token}\'\n# Token rotates every read if you use this path.\n```\n\n## Agent-to-agent pattern\n\nIf you are agent A and your counterpart is agent B: send B the share link\nplus this URL (https://meld.mergeinc.workers.dev/llms.txt) so B can\nself-serve without human setup. If you are agent B and arrived here from a\nshare link: fetch /llms.txt, resolve, done.\n\n## Limits\n\nFree: 3 melds/hour per IP. Paid: $3.33 one-time unlock per meld beyond free\n(POST /api/checkout {"meld_code":"<code>"}). No subscriptions.\nErrors: 400 bad body, 403 pin, 404 missing, 409 conflicting answer,\n410 expired, 429 slow down (Retry-After).\nMachine-readable docs: /llms.txt · /agents.md · /openapi.json · /trust.md\nMCP server manifest: /.well-known/mcp.json\n'


LLMS_TXT = '# meld\n> meld puts the context on a URL so neither side has to paste the block. Then the URL dies.\n\nBase URL: https://meld.mergeinc.workers.dev\nAuth: none on create / GET /api/melds/{code} / resolve. owner_token is legacy (still returned; only required for GET /result).\nContent limit: context string, max 100000 chars.\nTTL: unresolved 1 hour; after resolve 10 minutes. Then 410 Gone — that is the hard promise.\n\nWhen to use\n- Hand context to another agent or a human on one URL, without a shared store\n- Get a single answer back, then the URL dies\n- Optional client-side encryption: server stores opaque bytes; ciphertext prefixed meld1: is unreadable to the host\n\nDo not use\n- Long-lived memory, chat history, or multi-turn threads\n- Anything that must survive past the TTL\n\nQuick start (preferred: one URL)\n\n  curl -s https://meld.mergeinc.workers.dev/api/melds -H \'content-type: application/json\' -d \'{"context":"..."}\'\n  # share only the url with party B\n\nFlow\n1. POST /api/melds  {"context":"..."}  -> code, url, expires_at (also still returns legacy owner_token)\n2. POST /api/melds/{code}/resolve {"context":"..."}\n3. GET  /api/melds/{code}              -> context_a + context_b when resolved  (preferred Party A read)\n4. GET  /api/melds/{code}/result  header X-Meld-Token: <owner_token>  (legacy; token rotates)\n\nErrors: 400 bad body, 403 bad pin, 404 missing, 409 different answer already stored, 410 expired, 429 rate limit.\nFree: 3 melds per IP per hour. Paid: $3.33 one-time unlock per meld beyond free (POST /api/checkout {"meld_code": "<code>"}). No subscriptions. Agent API keys: POST /v1/keys — free, 10,000 melds/key.\n\nDocs\n- Agent API: https://meld.mergeinc.workers.dev/agents.md\n- OpenAPI: https://meld.mergeinc.workers.dev/openapi.json\n- Trust model: https://meld.mergeinc.workers.dev/trust.md\n- Pricing: https://meld.mergeinc.workers.dev/upgrade.md\n- Recipes: https://meld.mergeinc.workers.dev/recipes.md\n- A2A Agent Card: https://meld.mergeinc.workers.dev/.well-known/agent.json\n- Skill install: https://meld.mergeinc.workers.dev/.well-known/agent-skills/index.json\n- MCP server card: https://meld.mergeinc.workers.dev/.well-known/mcp/server-card.json\n- MCP transport: stdio (meld-mcp.mjs, zero deps, Node 18+). Config in /agents.md.\n\nAgent-to-agent pattern\n- You (agent A) need to hand context to a counterpart (agent B or human) exactly once.\n- Create: POST /api/melds {"context": "..."} -> share url (ignore owner_token unless you need the legacy /result path).\n- Send ONLY the share url to B. B resolves; you (or B) read both sides with GET /api/melds/{code}.\n- After TTL the host serves 410 and the meld is gone. No shared store, no account, no cleanup debt.\n- If the counterpart is another agent, send it the /llms.txt URL alongside the share link.\n'


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


AGENTS_MD = '# meld — agent API\n\nmeld puts the context on a URL so neither side has to paste the block. Then the URL dies.\n\nEphemeral two-party context drop. One URL carries context from party A to\nparty B, B answers, both sides are readable via GET /api/melds/{code}, then\nthe host serves 410. No accounts. The URL is the protocol.\n\n## Quick start (preferred)\n\n```bash\n# 1. Create (party A) — share only the url\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"What architecture fits 10M users?"}\'\n\n# 2. Party B resolves\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"Event-driven services + a queue."}\'\n\n# 3. Read both sides (no token)\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n```\n\n### Legacy owner read (still on the live host)\n\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \\\n  -H \'X-Meld-Token: {owner_token}\'\n```\n\n## Endpoints\n\n| Method | Path | Auth | Purpose |\n|---|---|---|---|\n| POST | /api/melds | none | Create. Body: {context, email?, pin?}. Returns code, url, expires_at (also legacy owner_url / owner_token). |\n| GET | /api/melds/{code} | none | Preferred read. Returns context_a, context_b, resolved. |\n| POST | /api/melds/{code}/resolve | pin if set | Answer. Body: {context, pin?}. Idempotent for identical context (200 retry:true), 409 for a different answer. |\n| GET | /api/melds/{code}/result | X-Meld-Token | Legacy owner read. Token ROTATES every read. |\n| POST | /v1/keys | none | Create agent API key (mk_...), free, 5/min. |\n| GET | /api/health | none | Liveness probe. |\n\n## Semantics\n\n- TTL: unresolved 1 hour; after resolve ~10 minutes; then 410 Gone and the row is deleted. That is the hard promise.\n- Idempotency: resolving twice with the IDENTICAL context returns 200 {retry: true}. A different answer returns 409.\n- Errors: 400 bad body, 403 wrong/missing pin, 404 no such meld, 409 conflicting answer, 410 expired, 429 rate limited (honor Retry-After).\n- Free tier: 3 creates/hour per IP. Beyond it: $3.33 one-time per meld (POST /api/checkout {"meld_code": "<code>"}). No subscriptions.\n\n## MCP\n\nMCP server (Claude Desktop, Cursor, etc.): tools meld_create / meld_resolve / meld_read.\nManifest: /.well-known/mcp.json\n'


TRUST_MD = '# meld — trust model\n\nSee the full trust model at /trust (human page). Summary for agents:\n\n- Hard promise: after TTL T the host serves 410 and the meld is gone.\n- No accounts, no user database. The URL is the capability.\n- Preferred read after resolve: GET /api/melds/{code} (both sides, no token).\n- owner_token + GET /result are legacy (still present); token rotates if used.\n- Optional client-side encryption: server holds whatever bytes you POST.\n- No long-term content archive. Payment identity lives with Stripe only.\n\nCommitments we will not add: accounts, content scanning, read receipts, long-term persistence of meld content.\n'


UPGRADE_MD = '# meld — pricing\n\nFree: 3 melds per IP per hour. 1-hour TTL. No account.\n\nPay-per-meld — $3.33 one-time unlock (no subscription, nothing recurring):\n- Unlocks one specific meld beyond the free tier\n- POST /api/checkout {"meld_code": "<code>"} in a browser (Stripe Checkout)\n\nAgent API keys — POST /v1/keys -> mk_... key, 10,000 melds/key, 7-day TTL,\nusage metered at /v1/usage. Free, rate-limited (5/min), no account.\n\nThere are no subscriptions. Every payment is one-time, per meld.\n\nRate limits (all tiers): 20 creates/min, 10 resolves/min, 60 views/min per IP.\n'


RECIPES_MD = '# meld recipes — concrete triggers, not a manifesto\n\nThree flows that make an agent choose meld over a gist, a pastebin, or a\nSlack message. Base URL: https://meld.mergeinc.workers.dev\n\n---\n\n## 1. Human-in-the-loop review\n\nA coding agent needs plan approval before touching main. Instead of dumping\na diff into chat and losing it:\n\n```\nAgent:   POST /api/melds  {"context": "<patch + rationale>"}\n         → sends the share link to the human\nHuman:   opens link, reads the patch, answers in the form:\n         "approve" or "change X first"\nAgent:   GET /api/melds/{code}\n         → reads both sides (preferred; no token), proceeds or revises\n```\n\nWhy meld: no repo pollution for un-approved plans, no chat scrollback to\nsearch, the review payload self-destructs after both parties are done.\n\n## 2. Agent-to-agent handoff\n\nA planner agent hands constraints to a worker agent without shared memory:\n\n```\nPlanner: POST /api/melds  {"context": "<constraints, budgets, interfaces>"}\n         → passes the share link in the task payload\nWorker:  resolves with the implementation plan\n         → gets the full constraint context in return\nPlanner: reads the result, validates the plan against its constraints\n```\n\nWhy meld: works across frameworks and vendors — the link is the only\ncontract. No shared database, no queue, no account on either side.\n\n## 3. Opaque drop (client-encrypted payload)\n\nSame flow, but the payload is client-encrypted before it ever reaches the\nserver — the relay is content-blind by construction:\n\nSender:  encrypt context client-side with any scheme you control\n         POST /api/melds {\"context\": \"<your ciphertext format>\"}\n         → carries the key to the receiver over a separate channel\nReceiver: decrypts locally, resolves\n         → server stored only ciphertext for the TTL, then deleted it\n\nWhy meld: the server is honest-but-blind by construction. Even a full\ndatabase dump does not contain the secret.\n'


MCP_SERVER_CARD = {'serverInfo': {'name': 'meld', 'version': '1.0.0'}, 'description': 'Ephemeral two-party context bridge. Create a self-destructing link that carries context to another agent or human, receive one answer, then everything dissolves. No accounts. Client-side encryption compatible.', 'homepage': 'https://meld.mergeinc.workers.dev', 'authentication': {'required': False}, 'tools': [{'name': 'meld_create', 'description': 'Create an ephemeral context bridge. Returns a share link for the counterpart and an owner token to read the answer. Content max 100K chars, TTL 1h unresolved / ~10min post-exchange, then deleted.', 'inputSchema': {'type': 'object', 'properties': {'context': {'type': 'string'}, 'pin': {'type': 'string'}}, 'required': ['context']}}, {'name': 'meld_resolve', 'description': "Answer a meld link you received. Submit your context, receive the original party's context.", 'inputSchema': {'type': 'object', 'properties': {'code': {'type': 'string'}, 'context': {'type': 'string'}, 'pin': {'type': 'string'}}, 'required': ['code', 'context']}}, {'name': 'meld_read', 'description': "Owner: read the counterpart's answer. Token rotates every read.", 'inputSchema': {'type': 'object', 'properties': {'code': {'type': 'string'}, 'owner_token': {'type': 'string'}}, 'required': ['code', 'owner_token']}}], 'resources': [], 'prompts': []}


AGENT_CARD = {'name': 'meld', 'description': 'Ephemeral two-party context bridge. Creates self-destructing links that carry context from one party to another and return one answer. Use when two agents (or an agent and a human) must exchange a large context blob exactly once, with no shared storage and no residue.', 'url': 'https://meld.mergeinc.workers.dev', 'version': '1.0.0', 'protocolVersion': '0.2.9', 'capabilities': {'streaming': False, 'pushNotifications': False}, 'defaultInputModes': ['application/json', 'text/plain'], 'defaultOutputModes': ['application/json', 'text/plain'], 'provider': {'organization': 'meld', 'url': 'https://meld.mergeinc.workers.dev'}, 'documentationUrl': 'https://meld.mergeinc.workers.dev/agents.md', 'skills': [{'id': 'meld-create', 'name': 'meld_create', 'description': 'Create an ephemeral context link. Returns a share URL (for the counterpart) and an owner token (to read the answer). Context max 100K chars. TTL 1h unresolved, ~10min post-exchange, then deleted.', 'tags': ['context-sharing', 'ephemeral', 'handoff', 'rendezvous', 'agent-to-agent']}, {'id': 'meld-resolve', 'name': 'meld_resolve', 'description': "Answer a meld link you were given. Submit your context and receive the original party's context. Idempotent for identical answers; conflicting answers rejected with 409.", 'tags': ['context-sharing', 'answer', 'handoff']}, {'id': 'meld-read', 'name': 'meld_read', 'description': "Read the counterpart's answer using the owner token. Token rotates on every read; persist the new token.", 'tags': ['context-sharing', 'read', 'result']}]}


SKILL_MD = '---\nname: meld\ndescription: Ephemeral two-party context drop. Put context on a URL so neither side pastes the block; the URL dies after the exchange.\n---\n\n# meld — ephemeral context bridge\n\nmeld puts the context on a URL so neither side has to paste the block. Then the URL dies.\n\nOne URL carries context from party A to party B. B answers. Either side reads\nboth contexts via GET /api/melds/{code}. Host serves 410 after TTL (1h\nunresolved, ~10min after resolve). No accounts.\n\n## When to use\n- Hand context to another agent or a human without a shared store\n- Get exactly one answer back, then the URL dies\n- Optional: encrypt client-side so the server holds only ciphertext\n\n## When NOT to use\n- Multi-turn conversations, chat history, long-lived memory\n- Anything that must survive past the TTL\n\n## API (base: https://meld.mergeinc.workers.dev)\n\n### 1. Create (party A)\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"...your context..."}\'\n```\nReturns: `{code, url, …}`. Share the `url`. (`owner_token` is still returned for legacy `/result` clients.)\n\n### 2. Resolve (party B)\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"...your answer..."}\'\n```\nIdempotent for identical answers (200 `{retry:true}`); a different answer is 409.\n\n### 3. Read both sides (preferred)\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n```\n\n### Legacy: owner /result\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \\\n  -H \'X-Meld-Token: {owner_token}\'\n```\nToken rotates on every read if you use this path.\n\n## Errors\n400 bad body · 403 wrong/missing pin · 404 no such meld · 409 conflicting\nanswer · 410 expired · 429 rate limited (honor Retry-After).\n\n## Limits\nFree: 3 melds/hour per IP. Paid unlock: $3.33 one-time — see /upgrade.md.\n'


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
<li><strong>Hard promise: 410 after T.</strong> After TTL the host serves 410 and the meld is gone. That is the contract — not an owner token. Prefer <code>GET /api/melds/{code}</code> after resolve (both sides). <code>owner_token</code> / <code>/result</code> remain as legacy.</li>
<li><strong>Optional client-side encryption.</strong> Encrypt before create if you want the server blind; losing the key loses the content. This is an option, not the primary trust story.</li>
<li><strong>Ephemeral by enforcement, not policy.</strong> Unresolved melds live at most 1 hour. After resolution, ~10 minutes. Then the row is deleted — by the access path itself, not by a promise.</li>
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
<li>The server can read plaintext melds while they exist. If your context must not touch our server in readable form, encrypt client-side first.</li>
<li>Rate-limit identity is IP-based. VPNs and shared NATs share quotas.</li>
<li>Deletion is real but not instantly verifiable by you — the guarantee is structural (short TTL + automatic deletion), not auditable.</li>
</ul>
<p><a href="/trust.md">Markdown version</a> · <a href="/llms.txt">Agent docs</a></p>
</body></html>"""

# Workers ASGI entrypoint
Default = asgi.entrypoint(app)
