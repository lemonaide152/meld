"""
meld — timed context bridge. Cloudflare Workers port (D1-backed).
- capability URL is the shared bearer for one exchange
- bridge time is exactly 3m, 1hr, or 1d; the server enforces that TTL
- pilot creates are free (no payment wall); per-minute abuse limits remain
- host-readable while live; anyone with the link can read it; dissolves on TTL
State lives in D1 (survives restarts — an upgrade over the RAM dict).
"""
import hashlib
import json
import secrets
import string
import time
import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from workers import asgi
from agents_content import AGENTS_HTML
from app_content import APP_HTML

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
def _require_context(context) -> str:
    """Reject missing/non-string/empty/whitespace-only context (stops probe pollution)."""
    if not isinstance(context, str):
        raise HTTPException(400, "Context must be a string")
    if not context.strip():
        raise HTTPException(400, "Context must be non-empty")
    if len(context) > MAX_CONTEXT:
        raise HTTPException(400, "Context too large (100K max)")
    return context


def _require_ttl(value) -> tuple:
    """Accept only the three pilot bridge times. Missing or other values are rejected."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            400,
            "Bridge time is required. Choose 3m (3 minutes), 1hr (1 hour), or 1d (1 day).",
        )
    key = value.strip().lower()
    if key not in BRIDGE_TTLS:
        raise HTTPException(400, "Bridge time must be one of: 3m, 1hr, 1d.")
    return key, BRIDGE_TTLS[key]


def _meld_meta(code, row, remaining):
    """Return capability metadata for a live meld."""
    return {
        "code": code,
        "resolved": bool(row["resolved"]),
        "resolved_at": row["resolved_at"],
        "expires_at": row["expires_at"],
        "seconds_remaining": remaining,
    }


def _attach_bodies(out, row, *, has_token: bool = False):
    """A live capability URL returns the stored contexts to its link-holder."""
    out["context_a"] = row["context_a"]
    out["context_b"] = row["context_b"]
    return out

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



# ── human vs agent create classifier (retained; pilot creates do not use it) ─
# Pilot timed bridges are free for humans and agents. Per-minute abuse limits
# still apply. This classifier is not a payment gate.
_AGENT_UA_MARKERS = (
    "curl/",
    "python-requests",
    "httpx",
    "go-http-client",
    "axios",
    "node-fetch",
    "undici",
)
_BROWSER_UA_HINTS = (
    "mozilla/",
    "chrome/",
    "safari/",
    "firefox/",
    "edg/",
    "opr/",
    "crios/",
    "fxios/",
)
PRICING_HEADER = "pilot-free"
# Pilot bridge lifetimes. Clients must send one of these keys. No default.
BRIDGE_TTLS = {"3m": 3 * 60, "1hr": 60 * 60, "1d": 24 * 60 * 60}


def _is_human_client(request: Request) -> bool:
    """True => skip FREE_LIMIT on POST /api/melds.

    Classifier:
      - X-Meld-Client: human  => human
      - X-Meld-Client: agent  => agent
      - else if User-Agent looks like a browser AND does not contain an agent
        marker (curl/python-requests/httpx/Go-http-client/axios/node-fetch/undici)
        => human
      - otherwise => agent (incl. missing/empty UA)
    """
    client = (request.headers.get("x-meld-client") or "").strip().lower()
    if client == "human":
        return True
    if client == "agent":
        return False
    ua = (request.headers.get("user-agent") or "").strip()
    if not ua:
        return False
    ua_l = ua.lower()
    if any(m in ua_l for m in _AGENT_UA_MARKERS):
        return False
    return any(h in ua_l for h in _BROWSER_UA_HINTS)

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
    """Legacy created-count helper. Pilot POST /api/melds does not call this.
    Per-minute abuse limits still apply on create."""
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
    # CORS: API + discovery + remote MCP (/mcp streamable-http)
    path = request.url.path
    if (path.startswith("/api") or path.startswith("/v1")
            or path in ("/", "/health", "/llms.txt", "/skill.md", "/agents.md", "/openapi.json", "/mcp")
            or path.startswith("/.well-known/")):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Accept, Authorization, X-Meld-Token, X-Meld-Client, "
            "X-Forwarded-For, Mcp-Session-Id, Mcp-Protocol-Version")
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
    return resp


_CORS_PREFLIGHT_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": (
        "Content-Type, Accept, Authorization, X-Meld-Token, X-Meld-Client, "
        "X-Forwarded-For, Mcp-Session-Id, Mcp-Protocol-Version"),
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Max-Age": "86400",
}


@app.options("/api/{rest:path}")
@app.options("/v1/{rest:path}")
@app.options("/.well-known/{rest:path}")
async def cors_preflight_rest(rest: str):
    # 204 must have empty body — JSONResponse({}, 204) crashes CF python workers (1101)
    return Response(status_code=204, headers=_CORS_PREFLIGHT_HEADERS)


@app.options("/health")
@app.options("/llms.txt")
@app.options("/agents.md")
@app.options("/openapi.json")
@app.options("/skill.md")
@app.options("/mcp")
@app.options("/")
async def cors_preflight_fixed():
    return Response(status_code=204, headers=_CORS_PREFLIGHT_HEADERS)


# ── API ──────────────────────────────────────────────────────────────────
@app.post("/api/melds")
async def create_meld(request: Request):
    conn = db(request)
    ip = _client_ip(request)
    body = await request.json()
    context = _require_context(body.get("context", ""))
    ttl_key, ttl_seconds = _require_ttl(body.get("ttl"))

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
    # Pilot: timed bridges are free for humans and agents. No hourly free wall
    # and no payment unlock. Per-minute abuse limits above still apply.

    code = _code()
    token = _token()
    now = _now()
    expiry = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(seconds=ttl_seconds)).isoformat()
    email = body.get("email")
    pin = body.get("pin")
    # SB-3: do not persist creator_ip on the meld row. Rate limits use
    # edge IP against rate/free_counts/ip_throttle only.
    await conn.prepare(
        "INSERT INTO melds (code, context_a, context_b, resolved, owner_token,"
        " owner_email, creator_ip, created_at, expires_at, pin)"
        " VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?, ?)").bind(
        code, context, token,
        email if isinstance(email, str) and len(email) <= 254 else None,
        "", now, expiry,  # creator_ip left empty (column retained for compat)
        hashlib.sha256(pin.encode()).hexdigest() if isinstance(pin, str) and 0 < len(pin) <= 128 else None,
    ).run()
    await _funnel(conn, "created")
    return JSONResponse(
        {
            "code": code,
            "url": f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/m/{code}",
            "owner_url": f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/m/{code}#t={token}",
            "owner_token": token,
            "context_a": context,
            "resolved": False,
            "ttl": ttl_key,
            "expires_at": expiry,
        },
        headers={"X-Meld-Pricing": PRICING_HEADER},
    )


@app.get("/api/melds/{code}")
async def get_meld(code: str, request: Request, token: str = ""):
    """A live capability URL is readable by anyone holding the link."""
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
    supplied = request.headers.get("X-Meld-Token", "") or token or ""
    has_token = bool(supplied) and secrets.compare_digest(row["owner_token"], supplied)
    out = _meld_meta(code, row, remaining)
    return _attach_bodies(out, row, has_token=has_token)


@app.post("/api/melds/{code}/resolve")
async def resolve_meld(code: str, request: Request):
    conn = db(request)
    ip = _client_ip(request)
    if not await _rate_limit(conn, "resolve", ip):
        raise HTTPException(429, "Too many requests. Please slow down.",
                            headers={"Retry-After": "60"})
    body = await request.json()
    context = _require_context(body.get("context", ""))

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

    # SB-3: do not persist resolver_ip on the meld row.
    # The bridge keeps the TTL chosen at create. Resolve does not invent
    # another lifetime.
    await conn.prepare(
        "UPDATE melds SET context_b = ?, resolved = 1, resolved_at = ?,"
        " responder_pubkey = ?, signature = ?"
        " WHERE code = ?").bind(
        context, _now(), pk, sig, code).run()
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


# MeshKore / A2A skill aliases — POST /v1/<skill.id> per agent card
@app.post("/v1/meld-create")
async def skill_meld_create(request: Request):
    """A2A skill: same as POST /api/melds (IP quota / human free)."""
    return await create_meld(request)


@app.post("/v1/meld-resolve")
async def skill_meld_resolve(request: Request):
    """A2A skill: body must include code + context (+ optional pin)."""
    body = await request.json()
    code = body.get("code")
    if not isinstance(code, str) or not code:
        raise HTTPException(400, "code required")
    return await resolve_meld(code, request)


@app.post("/v1/meld-read")
async def skill_meld_read(request: Request):
    """A2A skill: read a live meld by code."""
    body = await request.json()
    code = body.get("code")
    if not isinstance(code, str) or not code:
        raise HTTPException(400, "code required")
    tok = body.get("owner_token") or body.get("token") or ""
    if isinstance(tok, str) and tok:
        req = _asgi_json_request(
            request, "GET", f"/api/melds/{code}", None,
            extra_headers={"X-Meld-Token": tok})
        return await get_meld(code, req, token=tok)
    return await get_meld(code, request)


@app.get("/health")
async def health_root():
    """MeshKore-recommended liveness probe (JSON)."""
    return {
        "ok": True,
        "agent_id": "meld",
        "service": "meld",
        "upstream_ready": True,
        "api": "/llms.txt",
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
    context = _require_context(body.get("context", ""))

    ttl_key, ttl_seconds = _require_ttl(body.get("ttl"))
    code = _code()
    token = _token()
    now = _now()
    expiry = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(seconds=ttl_seconds)).isoformat()
    # SB-3: no IP (or key-hash stand-in) on the meld row.
    await conn.prepare(
        "INSERT INTO melds (code, context_a, context_b, resolved, owner_token,"
        " owner_email, creator_ip, created_at, expires_at, pin)"
        " VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?, NULL)").bind(
        code, context, token, key_row["label"], "", now, expiry
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
        "ttl": ttl_key,
        "expires_at": expiry,
    }


@app.post("/v1/melds/{code}/resolve")
async def v1_resolve_meld(code: str, request: Request):
    """Agent API: resolve a meld with Bearer auth."""
    conn = db(request)
    key_row = await _validate_api_key(request)
    if not key_row:
        raise HTTPException(401, "Invalid or missing API key")

    body = await request.json()
    context = _require_context(body.get("context", ""))

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

    await conn.prepare(
        "UPDATE melds SET context_b = ?, resolved = 1, resolved_at = ?"
        " WHERE code = ?").bind(
        context, _now(), code).run()
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

# Homepage preview describes the product. A bare /m/{code} share must not put
# the exchange into og:title, og:description, or the initial HTML.
_PRODUCT_PREVIEW = (
    '<meta name="description" content="Open a timed bridge. Share the capability URL. '
    'It dissolves when the timer ends.">'
)
_SHARE_PREVIEW = (
    '<meta name="description" content="This link expires. The exchange is not included in this preview.">'
    '<meta name="robots" content="noindex, nofollow">'
    '<meta property="og:title" content="meld — this bridge expires">'
    '<meta property="og:description" content="This link expires. The exchange is not included in this preview.">'
    '<meta property="og:type" content="website">'
    '<meta name="twitter:card" content="summary">'
    '<meta name="twitter:title" content="meld — this bridge expires">'
    '<meta name="twitter:description" content="This link expires. The exchange is not included in this preview.">'
)


def _render_page(*, share: bool) -> str:
    preview = _SHARE_PREVIEW if share else _PRODUCT_PREVIEW
    html = PAGE.replace("<!--MELD_PREVIEW-->", preview, 1)
    if share:
        html = html.replace(
            "<title>meld — timed bridge</title>",
            "<title>meld — this bridge expires</title>",
            1,
        )
    return html


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
        # Same SB-1 rules as GET /api/melds/{code}
        supplied = request.headers.get("X-Meld-Token", "") or ""
        has_token = bool(supplied) and secrets.compare_digest(row["owner_token"], supplied)
        out = _meld_meta(code, row, None)
        out["api"] = {"resolve": f"POST /api/melds/{code}/resolve",
                      "result": "GET /api/melds/{code}/result (X-Meld-Token)",
                      "read": "GET /api/melds/{code} (the capability URL is the access)"}
        return _attach_bodies(out, row, has_token=has_token)
    return HTMLResponse(_render_page(share=True))



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


@app.get("/sitemap.xml", response_class=PlainTextResponse)
async def sitemap_xml():
    return PlainTextResponse(SITEMAP_XML, media_type="application/xml")


@app.get("/.well-known/ai-plugin.json")
async def ai_plugin_json():
    return JSONResponse(AI_PLUGIN)



# ── MCP Streamable HTTP (remote MCP dirs / Glama connectors) ─────────────
# Spec: https://modelcontextprotocol.io/specification/2025-03-26/basic/transports
# Stateless JSON mode (no SSE session). Same tools as mcp/meld-mcp.mjs stdio.

_MCP_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")

_MCP_TOOLS = [
    {
        "name": "meld_create",
        "description": (
            "Create a timed bridge. Returns a capability URL (the shared bearer) for the "
            "other party. You must set ttl to exactly one of 3m, 1hr, or 1d. There is no "
            "default lifetime. Pilot bridges are free. The host can read the exchange "
            "while it is live; anyone with the link can too. Not for secrets. Dissolves "
            "when that TTL ends."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": (
                        "Working context the other party should read. Not for secrets, "
                        "credentials, or regulated data."
                    ),
                },
                "ttl": {
                    "type": "string",
                    "enum": ["3m", "1hr", "1d"],
                    "description": (
                        "Required bridge time. 3m = 3 minutes, 1hr = 1 hour, 1d = 1 day. "
                        "The server enforces this TTL. There is no default."
                    ),
                },
                "pin": {
                    "type": "string",
                    "description": "Optional PIN the other party must supply to answer.",
                },
            },
            "required": ["context", "ttl"],
        },
    },
    {
        "name": "meld_resolve",
        "description": (
            "Resolve a meld you received a link for. Submit your context/answer. "
            "Returns the other party's context. The meld dissolves shortly after."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The meld code from the link (the part after /m/).",
                },
                "context": {"type": "string", "description": "Your answer/context."},
                "pin": {"type": "string", "description": "PIN if the meld has one."},
            },
            "required": ["code", "context"],
        },
    },
    {
        "name": "meld_read",
        "description": (
            "Owner: read the resolved result of your meld. The owner token rotates "
            "on every read — use the newest one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The meld code."},
                "owner_token": {
                    "type": "string",
                    "description": "Your owner token (from meld_create or the previous read).",
                },
            },
            "required": ["code", "owner_token"],
        },
    },
]


def _mcp_cors_headers() -> dict:
    return dict(_CORS_PREFLIGHT_HEADERS)


def _asgi_json_request(request: Request, method: str, path: str, body: dict | None = None,
                       extra_headers: dict | None = None) -> Request:
    """Clone ASGI request with new method/path/JSON body for internal handler reuse."""
    raw = json.dumps(body if body is not None else {}).encode() if body is not None else b""
    header_list = []
    skip = {b"content-length", b"content-type"}
    for k, v in request.scope.get("headers", []):
        if k.lower() in skip:
            continue
        header_list.append((k, v))
    if body is not None:
        header_list.append((b"content-type", b"application/json"))
        header_list.append((b"content-length", str(len(raw)).encode()))
    if extra_headers:
        for hk, hv in extra_headers.items():
            kb = hk.lower().encode() if isinstance(hk, str) else hk
            vb = hv.encode() if isinstance(hv, str) else hv
            header_list = [(k, v) for k, v in header_list if k != kb]
            header_list.append((kb, vb))
    scope = dict(request.scope)
    scope["method"] = method
    scope["path"] = path
    scope["raw_path"] = path.encode()
    scope["headers"] = header_list
    done = False

    async def receive():
        nonlocal done
        if done:
            return {"type": "http.disconnect"}
        done = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


async def _mcp_unwrap(resp):
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    if isinstance(resp, dict):
        return resp
    return resp


async def _mcp_call_tool(name: str, args: dict, request: Request):
    """Invoke the same create/resolve/read handlers the HTTP API and stdio MCP use."""
    args = args or {}
    try:
        if name == "meld_create":
            # Pass ttl through unchanged. Do not invent a default.
            body = {"context": args.get("context", ""), "ttl": args.get("ttl", "")}
            if args.get("pin"):
                body["pin"] = args["pin"]
            req = _asgi_json_request(
                request, "POST", "/api/melds", body,
                extra_headers={"X-Meld-Client": "agent"})
            data = await _mcp_unwrap(await create_meld(req))
            return {
                "code": data.get("code"),
                "share_link": data.get("url"),
                "owner_link": data.get("owner_url"),
                "owner_token": data.get("owner_token"),
                "ttl": data.get("ttl"),
                "expires_at": data.get("expires_at"),
                "note": (
                    "Send the share link. It is the capability URL: anyone holding it "
                    "can read the live exchange. The bridge dissolves at expires_at. "
                    "Not for secrets."
                ),
            }
        if name == "meld_resolve":
            code = args.get("code", "")
            body = {"context": args.get("context", "")}
            if args.get("pin"):
                body["pin"] = args["pin"]
            req = _asgi_json_request(
                request, "POST", f"/api/melds/{code}/resolve", body)
            data = await _mcp_unwrap(await resolve_meld(code, req))
            return {
                "resolved": True,
                "their_context": data.get("context_a"),
                "your_context": data.get("context_b"),
            }
        if name == "meld_read":
            code = args.get("code", "")
            token = args.get("owner_token", "")
            req = _asgi_json_request(
                request, "GET", f"/api/melds/{code}/result", None,
                extra_headers={"X-Meld-Token": token})
            data = await _mcp_unwrap(await get_result(code, req))
            return {
                "resolved": True,
                "their_context": data.get("context_b"),
                "your_context": data.get("context_a"),
                "new_owner_token": data.get("owner_token"),
                "note": "Use new_owner_token for any future read — the old token is now invalid.",
            }
        raise ValueError(f"Unknown tool: {name}")
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else str(e.detail)
        raise RuntimeError(detail) from e


async def _mcp_handle_message(msg: dict, request: Request):
    """Handle one JSON-RPC message; return a response dict or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method is None:
        return None
    if method == "initialize":
        client_v = (params.get("protocolVersion") if isinstance(params, dict) else None) or ""
        version = client_v if client_v in _MCP_PROTOCOL_VERSIONS else _MCP_PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "meld", "version": "1.0.0"},
                "instructions": (
                    "meld: timed context bridge. meld_create requires ttl: 3m, 1hr, or 1d "
                    "(no default). The returned URL is the capability. Host-readable while "
                    "live; anyone with the link can read it; not for secrets; dissolves on "
                    "that TTL. Pilot creates are free. Base: https://meld.mergeinc.workers.dev"
                ),
            },
        }
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _MCP_TOOLS}}
    if method == "tools/call":
        name = params.get("name") if isinstance(params, dict) else None
        arguments = (params.get("arguments") if isinstance(params, dict) else None) or {}
        try:
            result = await _mcp_call_tool(name, arguments, request)
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return None


@app.post("/mcp")
async def mcp_post(request: Request):
    """Streamable HTTP MCP endpoint — JSON response mode (no SSE required)."""
    cors = _mcp_cors_headers()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error"}},
            status_code=400, headers=cors)

    messages = payload if isinstance(payload, list) else [payload]
    if not messages:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32600, "message": "Invalid Request"}},
            status_code=400, headers=cors)

    has_request = any(
        isinstance(m, dict) and m.get("method") and "id" in m for m in messages)
    responses = []
    for m in messages:
        if not isinstance(m, dict):
            responses.append({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32600, "message": "Invalid Request"}})
            continue
        r = await _mcp_handle_message(m, request)
        if r is not None:
            responses.append(r)

    if not has_request:
        return Response(status_code=202, headers=cors)

    if isinstance(payload, list):
        body = responses
    else:
        body = responses[0] if responses else {
            "jsonrpc": "2.0", "id": payload.get("id") if isinstance(payload, dict) else None,
            "error": {"code": -32603, "message": "No response"}}

    return JSONResponse(body, headers={**cors, "Content-Type": "application/json"})


@app.post("/")
async def mcp_post_root(request: Request):
    """Alias for Official MCP Registry remotes URL (root) used by Glama health checks.
    Preferred public path remains /mcp; GET / still serves the human SPA."""
    return await mcp_post(request)


@app.get("/mcp")
async def mcp_get():
    """Stateless server: no standalone SSE stream (JSON mode only)."""
    return Response(
        status_code=405,
        headers={**_mcp_cors_headers(), "Allow": "POST, OPTIONS, DELETE"})


@app.delete("/mcp")
async def mcp_delete():
    """No session state to tear down."""
    return Response(
        status_code=405,
        headers={**_mcp_cors_headers(), "Allow": "POST, OPTIONS"})



# Old FastAPI mint-era pages (app/templates and /upgrade, /success, /cancel, /error).
# The worker does not render those templates. Unknown product paths still get the SPA.
# These paths 404 so the mint-era URLs are not a live surface. /upgrade.md and /pro stay.
_LEGACY_PAGE_PATHS = frozenset({
    "upgrade", "success", "cancel", "error",
    "index.html", "upgrade.html", "success.html", "cancel.html",
    "error.html", "meld.html", "base.html",
})
_LEGACY_TEMPLATE_NAMES = frozenset({
    "index.html", "upgrade.html", "success.html", "cancel.html",
    "error.html", "meld.html", "base.html",
})


def _legacy_template_path(path: str) -> bool:
    norm = (path or "").strip("/").lower()
    if not norm or norm.endswith(".md"):
        return False
    if norm in _LEGACY_PAGE_PATHS:
        return True
    if norm.startswith("app/templates") or norm.startswith("templates/"):
        return True
    return norm.rsplit("/", 1)[-1] in _LEGACY_TEMPLATE_NAMES


@app.get("/{path:path}")
async def serve_page(path: str):
    if _legacy_template_path(path):
        raise HTTPException(404, "Not found")
    return HTMLResponse(_render_page(share=False))


@app.get("/")
async def root():
    return HTMLResponse(_render_page(share=False))


AGENTS_ROOT_MD = '# AGENTS.md — working with meld\n\nmeld puts context on a capability URL with a TTL. The host is readable while live, and anyone with the link can read it. After TTL, the meld dissolves and the host serves 410. Not for secrets, credentials, or regulated data.\n\n## Quick start\n\n```bash\n# Create; share url. Keep owner_token only for the legacy /result read.\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' -H \'X-Meld-Client: agent\' \\\n  -d \'{"context":"...","ttl":"1hr"}\'\n# -> {code, url, owner_url, owner_token, expires_at}\n\n# Resolve from the link.\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' -d \'{"context":"..."}\'\n\n# Anyone holding the capability URL can read the live contexts.\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n```\n\n## Locked claims\n\n- Capability URL + TTL.\n- Host-readable while live.\n- Anyone with the link can read it.\n- Not for secrets/credentials/regulated.\n- Dissolves on TTL.\n- Bridge time is required: 3m, 1hr, or 1d. The server enforces that TTL. There is no default.\n\n## Agent-to-agent\n\nCreate, send the share URL, resolve once, then read the URL. Pass `ttl` as `3m`, `1hr`, or `1d`. There is no default. MCP: https://meld.mergeinc.workers.dev/mcp\n\n## Limits and docs\n\nPilot bridges are free. Create requires `ttl`: `3m`, `1hr`, or `1d`. Per-minute limits apply to everyone. Errors: 400, 403 PIN, 404, 409, 410, 429.\n\nMachine-readable docs: /llms.txt · /agents.md · /recipes.md · /openapi.json · /trust.md\n'

LLMS_TXT = '# meld\n> Capability URL + TTL for a one-time context handoff.\n\nBase URL: https://meld.mergeinc.workers.dev\n\nLocked claims: host-readable while live; anyone with the link can read it; not for secrets/credentials/regulated; dissolves on TTL. Bridge time is required: 3m, 1hr, or 1d. The server enforces that TTL. There is no default.\n\n## Flow\n\n1. `POST /api/melds` with `{"context":"...","ttl":"1hr"}` -> `code`, `url`, `owner_token`, `expires_at`.\n2. Share `url` with the other party. The URL is the capability.\n3. `POST /api/melds/{code}/resolve` with `{"context":"..."}` to answer.\n4. `GET /api/melds/{code}` -> the live context for anyone holding the link.\n\nTTL: required on create, one of 3m (3 minutes), 1hr (1 hour), or 1d (1 day). The server enforces it. Then 410 Gone.\nContent limit: 100,000 characters. Not for secrets, credentials, or regulated data.\n\n## Agent quick start\n\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' -H \'X-Meld-Client: agent\' \\\n  -d \'{"context":"...","ttl":"1hr"}\'\n# share .url; resolve with the returned code\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' -d \'{"context":"..."}\'\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n```\n\n## Docs and integrations\n\n- Agent docs: https://meld.mergeinc.workers.dev/agents.md\n- Recipes: https://meld.mergeinc.workers.dev/recipes.md\n- Trust: https://meld.mergeinc.workers.dev/trust.md\n- OpenAPI: https://meld.mergeinc.workers.dev/openapi.json\n- MCP remote: https://meld.mergeinc.workers.dev/mcp\n- MCP manifest: https://meld.mergeinc.workers.dev/.well-known/mcp.json\n- Agent card: https://meld.mergeinc.workers.dev/.well-known/agent.json\n\nPilot bridges are free. Create requires `ttl`: `3m`, `1hr`, or `1d`. Per-minute limits apply to everyone.\n'

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
Sitemap: https://meld.mergeinc.workers.dev/sitemap.xml
"""


AGENTS_MD = '# meld — agent API\n\nmeld is a capability URL + TTL for one context exchange. Host-readable while live; anyone with the link can read it. Not for secrets/credentials/regulated. The meld dissolves on TTL.\n\n## Create -> resolve -> read\n\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' -H \'X-Meld-Client: agent\' \\\n  -d \'{"context":"What architecture fits 10M users?","ttl":"1hr"}\'\n# share the returned .url and note .code\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"Event-driven services plus a queue."}\'\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n```\n\nThe capability URL is the access. `owner_token` and `/result` remain as a legacy owner-read path. Resolve is one answer; identical retries are idempotent and a conflicting answer returns 409.\n\n## Bridge time\n\n`ttl` is required on create and must be `3m`, `1hr`, or `1d`. The server enforces that lifetime. There is no default. Pilot creates are free.\n\nMCP remote: https://meld.mergeinc.workers.dev/mcp · Recipes: /recipes.md · OpenAPI: /openapi.json\n'

TRUST_MD = '# meld — trust model\n\n- Capability URL + TTL: the URL grants access while the meld is live.\n- Host-readable while live.\n- Anyone with the link can read it.\n- Not for secrets/credentials/regulated.\n- Dissolves on the TTL chosen at create: 3 minutes (`3m`), 1 hour (`1hr`), or 1 day (`1d`).\n- Bridge time is required: 3m, 1hr, or 1d. The server enforces that TTL. There is no default.\n\nThe host stores ordinary context for the live TTL and deletes the meld after expiry. There are no accounts or long-term content archives. Rate-limit identity is IP-based. Use meld for ordinary, disposable handoffs only.\n'

UPGRADE_MD = """# meld — pilot

Timed bridges are free for pilot users. No payment is required to create one.

Choose a bridge time on create: `3m` (3 minutes), `1hr` (1 hour), or `1d` (1 day). The server enforces that TTL. There is no other duration and no default.

The link is the capability. The host can read the exchange while it is live. Anyone with the link can read it. Not for secrets, credentials, or regulated data. The bridge dissolves when the timer ends.

Abuse limits still apply: 20 creates/min, 10 resolves/min, 60 views/min per IP.
"""

RECIPES_MD = '# meld recipes\n\nEvery recipe uses the same locked bar: capability URL + TTL; host-readable while live; anyone with the link can read it; not for secrets/credentials/regulated; dissolves on TTL.\n\n## 1. FDE institutional-knowledge gather\n\nCreate with the question and repo paths, send the URL to the human/on-call, then read the same URL after they resolve.\n\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds -H \'content-type: application/json\' -H \'X-Meld-Client: agent\' -d \'{"context":"Question + repo paths + known constraints","ttl":"1hr"}\'\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve -H \'content-type: application/json\' -d \'{"context":"The institutional answer"}\'\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n```\n\n## 2. Provider-switch context handoff\n\nPut goals, constraints, files, and next step in one meld URL. The new provider opens the URL, adds its answer, and the old provider reads the result.\n\n## 3. Provider-switch request-meld\n\nThe new provider creates a URL containing the request. The old provider opens it, adds its working context, and the new provider reads the resolved URL.\n\n## 4. Create, share, resolve, read\n\n`POST /api/melds` requires `context` and `ttl` (`3m`, `1hr`, or `1d`). There is no default. Send the returned `.url`. The recipient resolves on that URL. Read `GET /api/melds/{code}` while the bridge is live. MCP: https://meld.mergeinc.workers.dev/mcp\n\n```text\nchoose ttl -> create -> share URL -> resolve -> read URL\n```\n\nPilot bridges are free. Create requires `ttl`: `3m`, `1hr`, or `1d`. Per-minute limits apply to everyone.\n'

MCP_SERVER_CARD = {
    'serverInfo': {'name': 'meld', 'version': '1.0.0'},
    'description': (
        'Ephemeral capability URL + TTL for a one-time context handoff. The host is '
        'readable while live, anyone with the link can read it, and the meld dissolves '
        'on TTL. Not for secrets, credentials, or regulated data.'
    ),
    'homepage': 'https://meld.mergeinc.workers.dev',
    'url': 'https://meld.mergeinc.workers.dev/mcp',
    'transport': 'streamable-http',
    'authentication': {'required': False},
    'tools': [
        {
            'name': 'meld_create',
            'description': (
                'Create a timed bridge. ttl is required: 3m, 1hr, or 1d. There is no '
                'default. Returns the capability URL. Pilot creates are free. '
                'Dissolves on that TTL.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'context': {'type': 'string'},
                    'ttl': {'type': 'string', 'enum': ['3m', '1hr', '1d']},
                    'pin': {'type': 'string'},
                },
                'required': ['context', 'ttl'],
            },
        },
        {
            'name': 'meld_resolve',
            'description': (
                "Answer a meld link you received. Submit your context, receive the "
                "original party's context."
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'code': {'type': 'string'},
                    'context': {'type': 'string'},
                    'pin': {'type': 'string'},
                },
                'required': ['code', 'context'],
            },
        },
        {
            'name': 'meld_read',
            'description': "Owner: read the counterpart's answer. Token rotates every read.",
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'code': {'type': 'string'},
                    'owner_token': {'type': 'string'},
                },
                'required': ['code', 'owner_token'],
            },
        },
    ],
    'resources': [],
    'prompts': [],
}


AGENT_CARD = {
    'name': 'meld',
    'description': 'Capability URL + TTL for a one-time context handoff. Host-readable while live; anyone with the link can read it; not for secrets/credentials/regulated; dissolves on TTL.',
    'url': 'https://meld.mergeinc.workers.dev',
    'version': '1.0.0',
    'protocolVersion': '0.2.9',
    'protocols': ['http', 'a2a', 'mcp'],
    'pricing': {'unit': 'request', 'amount': 0, 'currency': 'free', 'note': 'Pilot bridges are free. ttl required: 3m, 1hr, or 1d'},
    'availability': {'now': True, 'window_hours': 168, 'sla': 'best-effort'},
    'contact': {
        'http': 'https://meld.mergeinc.workers.dev/api/melds',
        'a2a': 'https://meld.mergeinc.workers.dev/.well-known/agent.json',
        'docs': 'https://meld.mergeinc.workers.dev/llms.txt',
    },
    'capabilities': {'streaming': False, 'pushNotifications': False},
    'defaultInputModes': ['application/json', 'text/plain'],
    'defaultOutputModes': ['application/json', 'text/plain'],
    'provider': {'organization': 'meld', 'url': 'https://meld.mergeinc.workers.dev'},
    'documentationUrl': 'https://meld.mergeinc.workers.dev/agents.md',
    'skills': [
        {'id': 'meld-create', 'name': 'meld_create', 'description': 'Create a timed bridge. ttl is required: 3m, 1hr, or 1d. No default. Returns the capability URL. Pilot creates are free. Dissolves on that TTL.', 'tags': ['context-sharing', 'ephemeral', 'handoff', 'rendezvous', 'agent-to-agent'], 'examples': ['Create a meld with context and ttl 1hr.']},
        {'id': 'meld-resolve', 'name': 'meld_resolve', 'description': "Answer a meld link you were given. Submit your context and receive the original party's context. Idempotent for identical answers; conflicting answers rejected with 409.", 'tags': ['context-sharing', 'answer', 'handoff'], 'examples': ['Resolve meld code abc123 with context: Event-driven services plus a queue.']},
        {'id': 'meld-read', 'name': 'meld_read', 'description': "Read the counterpart's answer using the owner token. Token rotates on every read; persist the new token.", 'tags': ['context-sharing', 'read', 'result'], 'examples': ['Read result for meld code abc123 with the owner token from create.']},
    ],
}


SKILL_MD = '---\nname: meld\ndescription: Capability URL + TTL for a one-time context handoff. Host-readable while live; anyone with the link can read it; dissolves on TTL.\n---\n\n# meld\n\nUse meld for one-time context exchange. It is not for secrets/credentials/regulated data.\n\n1. Create with `POST /api/melds` and share the returned URL.\n2. Resolve with `POST /api/melds/{code}/resolve`.\n3. Read the live context with `GET /api/melds/{code}`.\n4. Bridge time is required: 3m, 1hr, or 1d. The server enforces that TTL. There is no default.\n\nMCP: https://meld.mergeinc.workers.dev/mcp\nRecipes: https://meld.mergeinc.workers.dev/recipes.md\n'

SKILLS_INDEX = {'$schema': 'https://schemas.agentskills.io/discovery/0.2.0/schema.json', 'skills': [{'name': 'meld', 'description': 'Capability URL + TTL for a one-time context handoff. Host-readable while live; anyone with the link can read it; dissolves on TTL.', 'type': 'skill-md', 'url': 'https://meld.mergeinc.workers.dev/skill.md', 'digest': 'sha256:3229ba12e547e9503f98c03cba1e0829396aad521e8c8675a5f27bbb739352ec'}]}


MCP_MANIFEST = {
    "name": "meld",
    "description": "Capability URL + TTL for a one-time context handoff. Host-readable while live; anyone with the link can read it; not for secrets/credentials/regulated; dissolves on TTL.",
    "version": "1.0.0",
    "url": "https://meld.mergeinc.workers.dev/mcp",
    "homepage": "https://meld.mergeinc.workers.dev",
    "transport": "streamable-http",
    "transports": ["streamable-http", "stdio"],
    "command": "npx meld-mcp",
    "tools": ["meld_create", "meld_resolve", "meld_read"],
    "docs": "https://meld.mergeinc.workers.dev/llms.txt",
}



SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://meld.mergeinc.workers.dev/</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/llms.txt</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/agents.md</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/openapi.json</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/trust.md</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/upgrade.md</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/recipes.md</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/skill.md</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/.well-known/agent.json</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/.well-known/mcp/server-card.json</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/.well-known/mcp.json</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/.well-known/agent-skills/index.json</loc></url>
  <url><loc>https://meld.mergeinc.workers.dev/.well-known/ai-plugin.json</loc></url>
</urlset>
"""


AI_PLUGIN = {
    "schema_version": "v1",
    "name_for_human": "meld",
    "name_for_model": "meld",
    "description_for_human": "Capability URL + TTL for a one-time context handoff. Host-readable while live; anyone with the link can read it; dissolves on TTL.",
    "description_for_model": "Create a capability URL + TTL for a one-time context handoff. POST /api/melds with {context, ttl} where ttl is 3m, 1hr, or 1d (required, no default). The URL is the capability. Counterpart POST /api/melds/{code}/resolve; GET /api/melds/{code} returns the live context to anyone with the link. Host-readable while live, not for secrets/credentials/regulated, dissolves on that TTL. Pilot creates are free.",
    "auth": {"type": "none"},
    "api": {
        "type": "openapi",
        "url": "https://meld.mergeinc.workers.dev/openapi.json",
        "is_user_authenticated": False,
    },
    "logo_url": "https://meld.mergeinc.workers.dev/",
    "contact_email": "support@meld.mergeinc.workers.dev",
    "legal_info_url": "https://meld.mergeinc.workers.dev/trust.md",
}


TRUST_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>meld — trust model</title><style>body{font:16px/1.6 system-ui;max-width:680px;margin:2rem auto;padding:0 1rem;color:#30343b}code{font-family:ui-monospace,monospace}</style></head><body>
<h1>meld — trust model</h1>
<ul>
<li><strong>Capability URL + TTL.</strong> The URL grants access while the meld is live.</li>
<li><strong>Host-readable while live.</strong></li>
<li><strong>Anyone with the link can read it.</strong></li>
<li><strong>Not for secrets/credentials/regulated.</strong></li>
<li><strong>Dissolves on TTL.</strong> Choose 3 minutes, 1 hour, or 1 day at create. The server enforces that timer.</li>
</ul>
<p>meld is an ephemeral context handoff, not a vault. The host stores ordinary context only for the live TTL, then deletes the meld. There are no accounts or long-term content archives.</p>
<p><a href="/llms.txt">Agent docs</a> · <a href="/agents">Agents</a> · <a href="/">Create a meld</a></p>
</body></html>"""

# Workers ASGI entrypoint
Default = asgi.entrypoint(app)
