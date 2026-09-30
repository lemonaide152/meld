"""
meld — timed context bridge. Cloudflare Workers port (D1-backed).
- capability URL is the shared bearer for one exchange
- every meld lives 1 hour; other ttl values are rejected; omit ttl to get 1hr
- mint-next creates a new meld with its own 1 hour clock (not an extend)
- humans are free; agents get 3 creates/IP/hour, then HTTP 402 x402 (USDC on Base)
- host-readable while live; anyone with the link can read it; dissolves on TTL
State lives in D1 (survives restarts — an upgrade over the RAM dict).
Production Worker meld stays on the free pilot until this branch is deployed
somewhere else. This file does not deploy production.
"""
import hashlib
import json
import secrets
import string
import time
import datetime
from html import escape as html_escape

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from workers import asgi
from agents_content import AGENTS_HTML
try:
    from agents_content import AGENTS_MD
except ImportError:  # unit tests stub agents_content with AGENTS_HTML only
    AGENTS_MD = ""
from app_content import APP_HTML
from og_png import PNG as OG_PNG
from preview_meta import (
    SITE,
    META_SLOT,
    MARKETING_TITLE,
    CAPABILITY_TITLE,
    marketing_meta,
    capability_meta,
    capability_preview_document,
)
import x402_pay

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
    """Every meld lives 1 hour. Omit ttl or send 1hr. Any other value is rejected."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return TTL_KEY, HOUR_SECONDS
    if isinstance(value, str) and value.strip().lower() == TTL_KEY:
        return TTL_KEY, HOUR_SECONDS
    raise HTTPException(
        400,
        'This link lives 1 hour. Send ttl "1hr" or omit it. There is no other lifetime.',
    )


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


def _rows_of(got):
    """Normalize D1 .all() ({results}) and the sqlite test double (a list)."""
    if got is None:
        return []
    if isinstance(got, dict):
        return got.get("results") or []
    results = getattr(got, "results", None)
    if results is not None:
        return results
    return list(got)


async def _hop_parent(conn, prev):
    """Return (prev_code, thread_id). A new root is (None, None).

    The previous meld must still be live. This does not change its expires_at.
    """
    if prev is None or prev == "":
        return None, None
    if not isinstance(prev, str):
        raise HTTPException(400, "prev_code must be a string")
    prev = prev.strip()
    if not prev:
        return None, None
    row = await conn.prepare(
        "SELECT code, thread_id, expires_at FROM melds WHERE code = ?"
    ).bind(prev).first()
    if not row:
        raise HTTPException(404, "Previous meld not found")
    if row["expires_at"] <= _now():
        await conn.prepare("DELETE FROM melds WHERE code = ?").bind(prev).run()
        raise HTTPException(410, "Previous meld has expired")
    thread_id = row["thread_id"] or row["code"]
    if not row["thread_id"]:
        await conn.prepare(
            "UPDATE melds SET thread_id = ? WHERE code = ? AND thread_id IS NULL"
        ).bind(thread_id, row["code"]).run()
    return prev, thread_id


async def _insert_meld(conn, context, ttl_value, prev_value, email, pin_hash, code=None):
    """Insert one meld. Lifetime is always 1 hour, including a mint-next hop."""
    ttl_key, ttl_seconds = _require_ttl(ttl_value)
    prev_code, thread_id = await _hop_parent(conn, prev_value)
    code = code or _code()
    token = _token()
    now = _now()
    expiry = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(seconds=ttl_seconds)).isoformat()
    if not thread_id:
        thread_id = code
    await conn.prepare(
        "INSERT INTO melds (code, context_a, context_b, resolved, owner_token,"
        " owner_email, creator_ip, created_at, expires_at, pin, prev_code, thread_id)"
        " VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?, ?)"
    ).bind(
        code, context, token, email, "", now, expiry, pin_hash, prev_code, thread_id
    ).run()
    return {
        "code": code,
        "token": token,
        "ttl": ttl_key,
        "expires_at": expiry,
        "prev_code": prev_code,
        "thread_id": thread_id,
    }


# ── human vs agent create classifier ─────────────────────────────────────
# Humans skip the hourly free wall. Agents hit it, then x402 when configured.
# Per-minute abuse limits apply to everyone.
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
PRICING_HEADER = "humans-free; agents-key-or-quota-or-x402"
# SKU is one hour on this link. Mint-next is another link with its own hour.
TTL_KEY = "1hr"
HOUR_SECONDS = 60 * 60


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
    _status, text = await _fetch_full(url, headers=headers, body=body, method=method)
    return text


async def _fetch_full(url, headers=None, body=None, method="GET"):
    """Same as _fetch, but also returns the HTTP status (facilitator verify/settle)."""
    import json as _json
    import js
    init_dict = {"method": method, "headers": headers or {}}
    if body:
        init_dict["body"] = body
    init = js.JSON.parse(_json.dumps(init_dict))
    resp = await js.fetch(url, init)
    return int(resp.status), await resp.text()


async def _x402_http_post(url, headers, body):
    return await _fetch_full(url, headers=headers, body=body, method="POST")


x402_pay.http_post = _x402_http_post



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
    """True while this IP is still inside the agent free-create quota."""
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
            "X-Forwarded-For, Mcp-Session-Id, Mcp-Protocol-Version, "
            "PAYMENT-SIGNATURE")
        resp.headers["Access-Control-Expose-Headers"] = "PAYMENT-REQUIRED, PAYMENT-RESPONSE"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return _apply_security_headers(resp)


def _apply_security_headers(resp: Response) -> Response:
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
    return resp


def _og_png_response(*, head: bool) -> Response:
    """1200×630 card bytes. HEAD carries the GET headers and an empty body.

    Content-Length is the PNG size for both methods. X HEADs og:image before
    fetching; a 405 or a missing length blanks the card.
    """
    return Response(
        content=b"" if head else OG_PNG,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Length": str(len(OG_PNG)),
        },
    )


class _OgPngBypass:
    """Serve /og.png outside BaseHTTPMiddleware.

    That middleware rewrites every body as a stream (more_body=True). The
    Workers ASGI adapter then builds a ReadableStream, and Cloudflare omits
    Content-Length. A single buffered body keeps the length crawlers need.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != "/og.png":
            await self.app(scope, receive, send)
            return
        if scope.get("method") not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        # Staging allowlist stays in the security middleware.
        env = scope.get("env")
        allowed_raw = getattr(env, "ALLOWED_IPS", "") if env else ""
        if allowed_raw and str(allowed_raw).strip():
            await self.app(scope, receive, send)
            return
        response = _apply_security_headers(_og_png_response(head=scope["method"] == "HEAD"))
        await response(scope, receive, send)


# Outermost user middleware: registered after security so it runs first.
app.add_middleware(_OgPngBypass)


_CORS_PREFLIGHT_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": (
        "Content-Type, Accept, Authorization, X-Meld-Token, X-Meld-Client, "
        "X-Forwarded-For, Mcp-Session-Id, Mcp-Protocol-Version, "
        "PAYMENT-SIGNATURE"),
    "Access-Control-Expose-Headers": "PAYMENT-REQUIRED, PAYMENT-RESPONSE",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Max-Age": "86400",
}


@app.exception_handler(HTTPException)
async def _handle_http_exception(request: Request, exc: HTTPException):
    """402 bodies are the x402 PaymentRequired object, not {"detail": ...}."""
    headers = {str(k): str(v) for k, v in dict(exc.headers or {}).items()}
    if exc.status_code == 402 and isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=402, headers=headers)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)


def _x402_resource_url(request: Request, path: str) -> str:
    """Pin the challenge resource to an allowlisted host. Never echo an arbitrary Host."""
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if host not in ALLOWED_HOSTS:
        host = "meld.mergeinc.workers.dev"
    return f"https://{host}{path}"


def _x402_cfg(request: Request) -> dict:
    env = request.scope.get("env") if request.scope else None
    return x402_pay.config_from_env(env)


def _raise_x402(body: dict, status: int = 402):
    raise HTTPException(status, body, headers=x402_pay.challenge_headers(body, PRICING_HEADER))


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
    # Reject bad ttl before facilitator settle (omit or 1hr only).
    _require_ttl(body.get("ttl"))

    if not await _rate_limit(conn, "create", ip):
        raise HTTPException(429, "Too many requests. Please slow down.",
                            headers={"Retry-After": "60"})
    email = body.get("email")
    pin = body.get("pin")
    email_v = email if isinstance(email, str) and len(email) <= 254 else None
    pin_hash = (
        hashlib.sha256(pin.encode()).hexdigest()
        if isinstance(pin, str) and 0 < len(pin) <= 128 else None
    )

    human = _is_human_client(request)
    settlement = None
    if not human and not await _check_meld_limit(conn, ip):
        await _funnel(conn, "free_limit_hit")
        # MELD-FREELIMIT-002: unpaid wall hits feed the throttle ladder, one
        # offense per exhausted window (NAT guard: 2 hits in-window for rung 1).
        cfg = _x402_cfg(request)
        resource_url = _x402_resource_url(request, "/api/melds")
        if not cfg["pay_to"]:
            await _register_wall_hit(conn, ip)
            retry_after = str(max(1, int(
                (datetime.datetime.fromtimestamp(
                    (int(time.time() // (FREE_EXPIRY_HOURS * 3600)) + 1)
                    * FREE_EXPIRY_HOURS * 3600, datetime.timezone.utc)
                 - datetime.datetime.now(datetime.timezone.utc)).total_seconds())))
            raise HTTPException(
                429,
                "Agent free limit reached (3/hour per IP). "
                "Get a key via POST /v1/keys, pay with x402 once X402_PAY_TO is set, "
                "or wait. https://meld.mergeinc.workers.dev/upgrade.md",
                headers={"Retry-After": retry_after, "X-Meld-Pricing": PRICING_HEADER},
            )
        presented = x402_pay.payment_header(request)
        if not presented:
            await _register_wall_hit(conn, ip)
            _raise_x402(x402_pay.challenge(resource_url, cfg["pay_to"]))
        try:
            settlement = await x402_pay.settle_payment(
                cfg, presented, resource_url, PRICING_HEADER)
        except x402_pay.PaymentRejected as rejected:
            await _register_wall_hit(conn, ip)
            raise HTTPException(
                rejected.status, rejected.body, headers=rejected.headers)

    # Check mint-next parent inside _insert_meld before this sweeper. Sweeping
    # first would delete an expired parent and turn a 410 into a plain miss.
    minted = await _mint_meld(
        conn, context, body.get("ttl"), body.get("prev_code"),
        email_v, pin_hash, settlement)
    if minted == "redeemed":
        cfg = _x402_cfg(request)
        challenge = x402_pay.challenge(
            _x402_resource_url(request, "/api/melds"), cfg["pay_to"],
            error="payment already redeemed")
        _raise_x402(challenge)
    # Amortized sweeper after hop/insert so an expired parent still yields 410.
    try:
        await conn.prepare("DELETE FROM melds WHERE expires_at <= ?").bind(_now()).run()
        await _throttle_prune(conn)
    except Exception:
        pass  # never block create on sweep failure
    return _meld_created_response(request, minted)


async def _mint_meld(conn, context, ttl_value, prev_value, email, pin_hash, settlement):
    """Claim a settlement if any, then insert via _insert_meld (fixed 1hr + mint-next).

    Returns a dict for _meld_created_response, or "redeemed" when this tx or
    nonce was already used. payTo is never written.
    """
    code = _code()
    claimed = False
    if settlement is not None:
        claimed = await x402_pay.claim_payment(conn, settlement, code, _now())
        if not claimed:
            return "redeemed"
    try:
        made = await _insert_meld(
            conn, context, ttl_value, prev_value, email, pin_hash, code=code)
        if settlement is not None:
            now = _now()
            await conn.prepare(
                "UPDATE melds SET paid = 1 WHERE code = ?").bind(code).run()
            await conn.prepare(
                "INSERT INTO meld_payments (stripe_session_id, meld_code,"
                " amount_cents, paid_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(stripe_session_id) DO NOTHING").bind(
                "x402:" + settlement["transaction"], code,
                x402_pay.AMOUNT_CENTS, now).run()
            await conn.prepare(
                "INSERT INTO ledger (at, event, customer_id)"
                " VALUES (?, 'x402.unlock', ?)").bind(
                now, settlement.get("payer") or "").run()
            await _funnel(conn, "x402_settled")
    except Exception:
        if claimed:
            await x402_pay.release_claim(conn, settlement["transaction"])
        raise
    await _funnel(conn, "created")
    return {
        "code": made["code"],
        "token": made["token"],
        "context": context,
        "expiry": made["expires_at"],
        "ttl": made["ttl"],
        "prev_code": made["prev_code"],
        "thread_id": made["thread_id"],
        "settlement": settlement,
    }


def _meld_created_response(request, minted):
    code = minted["code"]
    token = minted["token"]
    host = request.headers.get("host", "localhost")
    scheme = request.url.scheme
    headers = {"X-Meld-Pricing": PRICING_HEADER}
    if minted["settlement"] is not None:
        headers["PAYMENT-RESPONSE"] = x402_pay.settlement_header(minted["settlement"])
        headers["Access-Control-Expose-Headers"] = "PAYMENT-REQUIRED, PAYMENT-RESPONSE"
    return JSONResponse(
        {
            "code": code,
            "url": f"{scheme}://{host}/m/{code}",
            "owner_url": f"{scheme}://{host}/m/{code}#t={token}",
            "owner_token": token,
            "context_a": minted["context"],
            "resolved": False,
            "ttl": minted["ttl"],
            "expires_at": minted["expiry"],
            "prev_code": minted["prev_code"],
            "thread_id": minted["thread_id"],
        },
        headers=headers,
    )



@app.get("/api/x402")
@app.post("/api/x402")
async def x402_resource(request: Request):
    """Listing URL for agent-pay markets. Unpaid requests return HTTP 402.

    POST with a settled PAYMENT-SIGNATURE mints one meld (same $3.33 as Stripe).
    Context and ttl are checked before settle. Omit ttl or send 1hr; other values 400.
    """
    cfg = _x402_cfg(request)
    resource_url = _x402_resource_url(request, "/api/x402")
    if not cfg["pay_to"]:
        return JSONResponse(
            {
                "error": "x402_not_configured",
                "detail": (
                    "Set Cloudflare secret X402_PAY_TO to the Base address that "
                    "should receive USDC. No payee is built into this worker."
                ),
            },
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    presented = x402_pay.payment_header(request)
    if request.method == "GET" or not presented:
        body = x402_pay.challenge(resource_url, cfg["pay_to"])
        return JSONResponse(
            body, status_code=402,
            headers=x402_pay.challenge_headers(body, PRICING_HEADER))
    conn = db(request)
    ip = _client_ip(request)
    if not await _rate_limit(conn, "create", ip):
        raise HTTPException(429, "Too many requests. Please slow down.",
                            headers={"Retry-After": "60"})
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be JSON: {\"context\": \"...\", \"ttl\": \"1hr\"}")
    context = _require_context(body.get("context", ""))
    ttl_key, ttl_seconds = _require_ttl(body.get("ttl"))
    try:
        settlement = await x402_pay.settle_payment(
            cfg, presented, resource_url, PRICING_HEADER)
    except x402_pay.PaymentRejected as rejected:
        raise HTTPException(
            rejected.status, rejected.body, headers=rejected.headers)
    email = body.get("email")
    pin = body.get("pin")
    email_v = email if isinstance(email, str) and len(email) <= 254 else None
    pin_hash = (
        hashlib.sha256(pin.encode()).hexdigest()
        if isinstance(pin, str) and 0 < len(pin) <= 128 else None
    )
    minted = await _mint_meld(
        conn, context, body.get("ttl"), body.get("prev_code"),
        email_v, pin_hash, settlement)
    if minted == "redeemed":
        challenge = x402_pay.challenge(
            resource_url, cfg["pay_to"], error="payment already redeemed")
        _raise_x402(challenge)
    return _meld_created_response(request, minted)


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


@app.get("/api/melds/{code}/chain")
async def get_chain(code: str, request: Request):
    """Live hops that share this link's thread. Expired plaintext is not returned."""
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
    thread_id = row["thread_id"] or code
    if not row["thread_id"]:
        await conn.prepare(
            "UPDATE melds SET thread_id = ? WHERE code = ?"
        ).bind(thread_id, code).run()
    now = _now()
    await conn.prepare(
        "DELETE FROM melds WHERE thread_id = ? AND expires_at <= ?"
    ).bind(thread_id, now).run()
    got = await conn.prepare(
        "SELECT code, context_a, context_b, resolved, expires_at, prev_code "
        "FROM melds WHERE thread_id = ? AND expires_at > ? ORDER BY created_at"
    ).bind(thread_id, now).all()
    host = request.headers.get("host", "localhost")
    scheme = request.url.scheme
    nodes = []
    for item in _rows_of(got):
        exp = item["expires_at"]
        if exp <= now:
            continue
        try:
            delta = datetime.datetime.fromisoformat(exp) - datetime.datetime.now(datetime.timezone.utc)
            remaining = max(0, int(delta.total_seconds()))
        except Exception:
            remaining = None
        hop = item["code"]
        nodes.append({
            "code": hop,
            "url": f"{scheme}://{host}/m/{hop}",
            "context_a": item["context_a"],
            "context_b": item["context_b"],
            "resolved": bool(item["resolved"]),
            "expires_at": exp,
            "seconds_remaining": remaining,
            "ttl": TTL_KEY,
            "prev_code": item["prev_code"],
        })
    return {"thread_id": thread_id, "nodes": nodes}


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
    # SB-3: no IP (or key-hash stand-in) on the meld row.
    made = await _insert_meld(
        conn, context, body.get("ttl"), body.get("prev_code"), key_row["label"], None
    )
    code = made["code"]
    token = made["token"]
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
        "ttl": made["ttl"],
        "expires_at": made["expires_at"],
        "prev_code": made["prev_code"],
        "thread_id": made["thread_id"],
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

# Raw shell. Card tags are inserted per route so /m/{code} never inherits
# the homepage image card. Stub HTML used by unit tests has no slot.
PAGE = APP_HTML
_HOME_TITLE = "<title>meld — timed bridge</title>"


def _render_page(*, share: bool) -> str:
    """Homepage card vs generic expires-only card for a capability URL."""
    if META_SLOT not in PAGE:
        return PAGE
    if share:
        html = PAGE.replace(META_SLOT, capability_meta(), 1)
        return html.replace(
            _HOME_TITLE, f"<title>{html_escape(CAPABILITY_TITLE)}</title>", 1
        )
    html = PAGE.replace(META_SLOT, marketing_meta(SITE + "/"), 1)
    return html.replace(_HOME_TITLE, f"<title>{html_escape(MARKETING_TITLE)}</title>", 1)

# Crawlers that build link cards. Search crawlers are not listed: /m/ HTML
# carries noindex, and the initial markup still has no meld body.
_LINK_PREVIEW_BOTS = (
    "twitterbot",
    "slackbot",
    "slack-imgproxy",
    "discordbot",
    "facebookexternalhit",
    "facebot",
    "linkedinbot",
    "whatsapp",
    "telegrambot",
    "embedly",
    "iframely",
    "redditbot",
    "pinterest",
    "vkshare",
    "quora link preview",
    "skypeuripreview",
)


def _is_link_preview_bot(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    return any(bot in ua for bot in _LINK_PREVIEW_BOTS)


@app.api_route("/og.png", methods=["GET", "HEAD"])
async def og_image(request: Request):
    """Static 1200×630 product card. No meld content.

    Production GET/HEAD is sent by _OgPngBypass before this route. The route
    remains so a direct router call still answers both methods.
    """
    return _og_png_response(head=request.method == "HEAD")

@app.get("/m/{code}")
async def serve_meld(request: Request, code: str):
    # Unfurl gate: preview crawlers get a generic card and this branch does
    # not read the meld. Human HTML is the SPA with the same generic tags;
    # context loads in the browser and is never written into meta tags.
    if _is_link_preview_bot(request):
        return HTMLResponse(capability_preview_document())
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
            "other party. Every meld lives 1 hour. Omit ttl or send 1hr. After the agent "
            "free quota, create returns HTTP 402. The host can read the exchange while it is live; anyone with the "
            "link can too. Not for secrets. Dissolves when that hour ends. prev_code "
            "mints the next link with its own 1 hour. That is not an extend."
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
                    "enum": ["1hr"],
                    "default": "1hr",
                    "description": (
                        "Lifetime is 1 hour. Omit this or send 1hr. The server rejects "
                        "any other value."
                    ),
                },
                "prev_code": {
                    "type": "string",
                    "description": (
                        "Optional code of a live meld. Starts a new bearer URL with its "
                        "own 1 hour clock. Does not change the previous meld's expiry."
                    ),
                },
                "pin": {
                    "type": "string",
                    "description": "Optional PIN the other party must supply to answer.",
                },
            },
            "required": ["context"],
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
            # Missing ttl becomes 1hr inside create. Other values are rejected.
            body = {"context": args.get("context", ""), "ttl": args.get("ttl", "")}
            if args.get("prev_code"):
                body["prev_code"] = args.get("prev_code")
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
        if e.status_code == 402 and isinstance(e.detail, dict):
            raise x402_pay.PaymentRejected(e.status_code, e.detail, dict(e.headers or {})) from e
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
                    "meld: timed context bridge. Every meld lives 1 hour. Omit ttl or send "
                    "1hr. prev_code mints a new link with its own hour (not an extend). "
                    "The returned URL is the capability. Host-readable while live; anyone "
                    "with the link can read it; not for secrets; dissolves after that hour. "
                    "After the agent free quota, meld_create returns HTTP 402. "
                    "Base: https://meld.mergeinc.workers.dev"
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
        except x402_pay.PaymentRejected:
            raise
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
        try:
            r = await _mcp_handle_message(m, request)
        except x402_pay.PaymentRejected as rejected:
            headers = {**cors, **dict(rejected.headers or {})}
            return JSONResponse(rejected.body, status_code=rejected.status, headers=headers)
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


@app.api_route("/", methods=["GET", "HEAD"])
async def root(request: Request):
    html = _render_page(share=False)
    encoded = html.encode("utf-8")
    # HEAD is the same headers as GET, including the meta document's length, and no body.
    # OPTIONS / is registered earlier and would otherwise 405 a HEAD (Allow: OPTIONS).
    return HTMLResponse(
        content=b"" if request.method == "HEAD" else encoded,
        headers={"Content-Length": str(len(encoded))},
    )


# Served at /AGENTS.md. Source: deploy/AGENTS-root.md
AGENTS_ROOT_MD = """# AGENTS.md — working with meld

Fetch and follow the install guide: https://meld.mergeinc.workers.dev/agents.md

Paste prompt for a coding agent:

```text
fetch https://meld.mergeinc.workers.dev/agents.md and set me up for meld
```

## What this is

A capability URL for one context exchange. Each link lives 1 hour. Host-readable while live. Anyone with the link can read it. Not for secrets/credentials/regulated. After that hour the host serves 410. Mint-next creates a new URL with its own hour. That is not an extend.

Pilot creates are free. Omit `ttl` or send `1hr`. The server rejects any other lifetime.

## Two uses

1. Human → agent. A person pours context on the web UI. The agent fetches it with MCP and/or HTTP.
2. Agent → agent. The bearer URL is the channel. One agent creates it; the other resolves and reads it.

## Where to connect

MCP Streamable HTTP (no API key in the URL, no OAuth): https://meld.mergeinc.workers.dev/mcp

Skill: https://meld.mergeinc.workers.dev/skill.md

Docs: /llms.txt · /agents.md · /recipes.md · /openapi.json · /trust.md
"""

LLMS_TXT = '# meld\n> Capability URL for a one-hour context handoff.\n\nBase URL: https://meld.mergeinc.workers.dev\n\nInstall guide: https://meld.mergeinc.workers.dev/agents.md — fetch that URL and set the coding agent up for meld. Two uses only: a human pours context on the web UI and an agent fetches it, or one agent creates the bearer URL and another agent resolves and reads it.\n\nLocked claims: host-readable while live; anyone with the link can read it; not for secrets/credentials/regulated; each link lives 1 hour. Omit ttl or send 1hr. Any other lifetime is rejected. Mint-next creates a new meld URL with its own hour. That is not an extend. Expired hops are deleted and are not returned on the chain.\n\n## Flow\n\n1. `POST /api/melds` with `{"context":"..."}` or `{"context":"...","ttl":"1hr"}` -> `code`, `url`, `owner_token`, `expires_at`.\n2. Share `url` with the other party. The URL is the capability.\n3. `POST /api/melds/{code}/resolve` with `{"context":"..."}` to answer.\n4. `GET /api/melds/{code}` -> the live context for anyone holding the link.\n5. `POST /api/melds` with `prev_code` set to a live code -> a new URL with its own hour (mint-next).\n6. `GET /api/melds/{code}/chain` -> live hops only. Dissolved plaintext is omitted.\n\nLifetime: 1 hour. Then 410 Gone.\nContent limit: 100,000 characters. Not for secrets, credentials, or regulated data.\n\n## Agent quick start\n\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' -H \'X-Meld-Client: agent\' \\\n  -d \'{"context":"..."}\'\n# share .url; resolve with the returned code\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' -d \'{"context":"..."}\'\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n```\n\n## Docs and integrations\n\n- Agent docs: https://meld.mergeinc.workers.dev/agents.md\n- Recipes: https://meld.mergeinc.workers.dev/recipes.md\n- Trust: https://meld.mergeinc.workers.dev/trust.md\n- OpenAPI: https://meld.mergeinc.workers.dev/openapi.json\n- MCP remote: https://meld.mergeinc.workers.dev/mcp\n- MCP manifest: https://meld.mergeinc.workers.dev/.well-known/mcp.json\n- Agent card: https://meld.mergeinc.workers.dev/.well-known/agent.json\n\nPilot bridges are free. Each link is one hour. Per-minute limits apply to everyone.\n'

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



TRUST_MD = '# meld — trust model\n\n- Capability URL + 1 hour: the URL grants access while that meld is live.\n- Host-readable while live.\n- Anyone with the link can read it.\n- Not for secrets/credentials/regulated.\n- Each link lives 1 hour, then it dissolves. The server enforces that clock.\n- Mint-next creates a new bearer URL with its own hour. That is not an extend. It is not a forever thread.\n- Expired hops are deleted. A chain read returns only hops that are still live. Dissolved plaintext is not kept on the chain.\n- This is not a private room and not a vault.\n\nThe host stores ordinary context for the live hour and deletes the meld after expiry. There are no accounts or long-term content archives. Rate-limit identity is IP-based. Use meld for ordinary, disposable handoffs only.\n\n## Link previews\n\n`/`, `/agents`, and `/trust` use a product card: a temporary resource to align context. Each link lives 1 hour, then it dies. Not for secrets.\n\n`/m/{code}` unfurls as a generic card only: title “meld — this bridge expires”, description “This link expires. The exchange is not included in this preview.” Slack, X, and Discord GET the URL. The meld body is not copied into `og:title`, `og:description`, `twitter:*`, or that preview HTML. The crawler response has no script and does not read the meld.\n'

UPGRADE_MD = """# meld — pricing

Production Worker `meld` stays on the free pilot. This document describes the worker that charges agents after the free quota. Do not put these secrets on Worker `meld`.

Each link lives 1 hour. Omit ttl or send `1hr`. The server rejects any other lifetime. Mint-next is another link with its own hour. That is not an extend.

Humans: free in the browser (`X-Meld-Client: human` or a browser user agent).
Agents on POST /api/melds: 3 creates per IP per hour, then HTTP 402 (x402) or a key, or wait.
Header: `X-Meld-Pricing: humans-free; agents-key-or-quota-or-x402`.

The link is the capability. The host can read the exchange while it is live. Anyone with the link can read it. Not for secrets, credentials, or regulated data. When the hour ends, the bridge dissolves.

## x402 (agents) — USDC on Base, same $3.33

- Unpaid probe: `GET /api/x402` returns HTTP 402 once `X402_PAY_TO` is set. Without that secret the probe is HTTP 503 and the agent wall on POST /api/melds stays HTTP 429.
- Paid create: `POST /api/x402` with `{"context":"...","ttl":"1hr"}` and header `PAYMENT-SIGNATURE`.
- The same challenge is returned by `POST /api/melds` and MCP `meld_create` after the agent free quota. Omit `ttl` or send `1hr` before settle.
- Scheme exact, network `eip155:8453` (Base), asset USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, amount `3330000` atomic units ($3.33, 6 decimals).
- Unlock happens only after facilitator verify and settle return a transaction for that amount and payTo. A JSON flag does not unlock. On-chain USDC settlement is irreversible.

Set with `npx wrangler secret put` on the preview Worker only. Do not commit the values.

| Secret | Required | Purpose |
|---|---|---|
| `X402_PAY_TO` | Yes, before a 402 with payTo | Base address that receives USDC. |
| `X402_FACILITATOR_URL` | Yes, before a signature can unlock | https origin. Worker POSTs `{url}/verify` and `{url}/settle`. Base mainnet: `https://api.cdp.coinbase.com/platform/v2/x402`. |
| `X402_FACILITATOR_AUTH` | When the facilitator requires it | Full Authorization header value (CDP: the bearer credential). |

Browser unlock remains `POST /api/checkout` with `{"meld_code":"<code>"}`. One payment, no subscription.

Abuse limits still apply: 20 creates/min, 10 resolves/min, 60 views/min per IP.
"""

RECIPES_MD = '# meld recipes\n\nEvery recipe uses the same locked bar: capability URL; host-readable while live; anyone with the link can read it; not for secrets/credentials/regulated; each link lives 1 hour. Mint-next is a new link with its own hour, not an extend.\n\n## 1. FDE institutional-knowledge gather\n\nCreate with the question and repo paths, send the URL to the human/on-call, then read the same URL after they resolve.\n\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds -H \'content-type: application/json\' -H \'X-Meld-Client: agent\' -d \'{"context":"Question + repo paths + known constraints"}\'\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve -H \'content-type: application/json\' -d \'{"context":"The institutional answer"}\'\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n```\n\n## 2. Provider-switch context handoff\n\nPut goals, constraints, files, and next step in one meld URL. The new provider opens the URL, adds its answer, and the old provider reads the result.\n\n## 3. Provider-switch request-meld\n\nThe new provider creates a URL containing the request. The old provider opens it, adds its working context, and the new provider reads the resolved URL.\n\n## 4. Create, share, resolve, read, mint-next\n\n`POST /api/melds` requires `context`. `ttl` may be omitted or `1hr`. Send the returned `.url`. The recipient resolves on that URL. Read `GET /api/melds/{code}` while the bridge is live. To hop, `POST /api/melds` with `prev_code` set to a live code. That new URL has its own hour. MCP: https://meld.mergeinc.workers.dev/mcp\n\n```text\ncreate -> share URL -> resolve -> read URL -> mint-next (new URL, own hour)\n```\n\nPilot bridges are free. Each link is one hour. Per-minute limits apply to everyone.\n'

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
                'Create a timed bridge. Lifetime is 1 hour. Omit ttl or send 1hr. '
                'prev_code mints a new link with its own hour (not an extend). '
                'Returns the capability URL. After the agent free quota, create returns HTTP 402.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'context': {'type': 'string'},
                    'ttl': {'type': 'string', 'enum': ['1hr'], 'default': '1hr'},
                    'prev_code': {'type': 'string'},
                    'pin': {'type': 'string'},
                },
                'required': ['context'],
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
    'pricing': {'unit': 'request', 'amount': 0, 'currency': 'free', 'note': 'Humans free in browser. Agents: 3 creates/IP/hour then HTTP 402. Each link is 1 hour. Mint-next is another 1 hour link.'},
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
        {'id': 'meld-create', 'name': 'meld_create', 'description': 'Create a timed bridge. Lifetime is 1 hour. Omit ttl or send 1hr. prev_code mints a new link with its own hour, not an extend. Returns the capability URL. After the agent free quota, create returns HTTP 402.', 'tags': ['context-sharing', 'ephemeral', 'handoff', 'rendezvous', 'agent-to-agent'], 'examples': ['Create a meld with context. The link lives 1 hour.']},
        {'id': 'meld-resolve', 'name': 'meld_resolve', 'description': "Answer a meld link you were given. Submit your context and receive the original party's context. Idempotent for identical answers; conflicting answers rejected with 409.", 'tags': ['context-sharing', 'answer', 'handoff'], 'examples': ['Resolve meld code abc123 with context: Event-driven services plus a queue.']},
        {'id': 'meld-read', 'name': 'meld_read', 'description': "Read the counterpart's answer using the owner token. Token rotates on every read; persist the new token.", 'tags': ['context-sharing', 'read', 'result'], 'examples': ['Read result for meld code abc123 with the owner token from create.']},
    ],
}


# Served at /skill.md. Source: recipes/SKILL.md
SKILL_MD = """---
name: meld
description: Timed capability URL for two handoffs. A human pours context on the web UI and an agent fetches it, or one agent creates a bearer URL another agent resolves. Each link lives 1 hour. Mint-next starts a new link with its own hour, not an extend. Host-readable while live. Anyone with the link can read it. Not for secrets.
---

# meld

Base: https://meld.mergeinc.workers.dev

Not for secrets/credentials/regulated. Host-readable while live. Anyone with the link can read it. Each link lives 1 hour, then it is gone. Pilot creates are free.

Omit `ttl` or send `1hr`. The server rejects any other lifetime. Mint-next passes `prev_code` and creates a new bearer URL with its own hour. That is not an extend. `GET /api/melds/{code}/chain` returns only hops that are still live.

## Uses

1. Human → agent. The person pours context on the web UI and sends the capability URL. Fetch it with `GET /api/melds/{code}`. To put an answer on that bridge, `POST /api/melds/{code}/resolve` or MCP `meld_resolve`. If you already have a chat with that person, answer in the chat after you fetch.
2. Agent → agent. Create with `context`, send the returned URL, and the other agent resolves and reads it. The URL is the channel. Mint-next from a live reply starts the next link. It does not keep the previous hour running.

## MCP

Streamable HTTP, no API key in the URL, no OAuth: https://meld.mergeinc.workers.dev/mcp

- `meld_create` — `context`. Optional `ttl` (`1hr` only). Optional `prev_code` for mint-next.
- `meld_resolve` — `code` and `context`
- `meld_read` — `code` and `owner_token` (legacy owner path; the token rotates)

## HTTP

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds -H 'content-type: application/json' -H 'X-Meld-Client: agent' -d '{"context":"..."}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve -H 'content-type: application/json' -d '{"context":"..."}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
curl -s https://meld.mergeinc.workers.dev/api/melds -H 'content-type: application/json' -d '{"context":"...","prev_code":"{code}"}'
```

`X-Meld-Client: agent` is an optional label the worker already accepts. It is not a credential.

Install guide: https://meld.mergeinc.workers.dev/agents.md
Recipes: https://meld.mergeinc.workers.dev/recipes.md
"""

SKILLS_INDEX = {'$schema': 'https://schemas.agentskills.io/discovery/0.2.0/schema.json', 'skills': [{'name': 'meld', 'description': 'Timed capability URL for two handoffs. A human pours context on the web UI and an agent fetches it, or one agent creates a bearer URL another agent resolves. Each link lives 1 hour. Mint-next starts a new link with its own hour, not an extend. Host-readable while live. Anyone with the link can read it. Not for secrets.', 'type': 'skill-md', 'url': 'https://meld.mergeinc.workers.dev/skill.md', 'digest': 'sha256:bbd90e8d3188bf590243b73f9088814de83cc2f85c0fe3ac132eff2a1c22a039'}]}


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
    "description_for_model": "Create a capability URL that lives 1 hour. POST /api/melds with {context} or {context, ttl:\"1hr\"}. Other ttl values are rejected. Optional prev_code mints a new link with its own hour (not an extend). The URL is the capability. Counterpart POST /api/melds/{code}/resolve; GET /api/melds/{code} returns the live context to anyone with the link. GET /api/melds/{code}/chain returns only hops that are still live. Host-readable while live, not for secrets/credentials/regulated, dissolves after that hour. After the agent free quota, create returns HTTP 402.",
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


TRUST_HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>meld — trust model</title>'
    + marketing_meta(SITE + "/trust")
    + '<style>body{font:16px/1.6 system-ui;max-width:680px;margin:2rem auto;padding:0 1rem;color:#30343b}code{font-family:ui-monospace,monospace}</style></head><body>\n'
    "<h1>meld — trust model</h1>\n"
    "<ul>\n"
    "<li><strong>Capability URL + TTL.</strong> The URL grants access while the meld is live.</li>\n"
    "<li><strong>Host-readable while live.</strong></li>\n"
    "<li><strong>Anyone with the link can read it.</strong></li>\n"
    "<li><strong>Not for secrets/credentials/regulated.</strong></li>\n"
    "<li><strong>Dissolves after 1 hour.</strong> The server enforces that clock. Mint-next is a new link with its own hour, not an extend.</li>\n"
    "</ul>\n"
    "<p>meld is an ephemeral context handoff, not a vault. The host stores ordinary context only for the live TTL, then deletes the meld. There are no accounts or long-term content archives.</p>\n"
    "<h2>Link previews</h2>\n"
    "<p><code>/</code>, <code>/agents</code>, and <code>/trust</code> use a product card: a temporary resource to align context. Each link lives 1 hour, then it dies. Not for secrets.</p>\n"
    "<p><code>/m/{code}</code> unfurls as a generic card only: title “meld — this bridge expires”, description “This link expires. The exchange is not included in this preview.” Slack, X, and Discord GET the URL. The meld body is not copied into <code>og:title</code>, <code>og:description</code>, <code>twitter:*</code>, or that preview HTML. That crawler response has no script and does not read the meld.</p>\n"
    '<p><a href="/llms.txt">Agent docs</a> · <a href="/agents">Agents</a> · <a href="/">Create a meld</a></p>\n'
    "</body></html>"
)

# Workers ASGI entrypoint
Default = asgi.entrypoint(app)
