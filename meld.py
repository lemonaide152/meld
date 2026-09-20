"""
meld — ephemeral context bridge.

One link. Pour in context. Done. Monetized.

Zero accounts. Zero login. Zero SQL. Stripe is the only dependency.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import string
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# ── Logging (structured, no print) ──────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("meld")

# ── Config (env vars only — no config file) ──────────────────────────────

PUBLIC_URL = os.getenv("MELD_PUBLIC_URL", "http://localhost:8080")
FREE_LIMIT = int(os.getenv("MELD_FREE_LIMIT", "3"))
FREE_EXPIRY_HOURS = int(os.getenv("MELD_FREE_EXPIRY", "1"))
MAX_LIVE_MELDS = int(os.getenv("MELD_MAX_LIVE_MELDS", "50000"))  # global RAM guard
MAX_BODY_BYTES = int(os.getenv("MELD_MAX_BODY_BYTES", str(200_000)))  # 200KB cap

STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_MONTHLY = os.getenv("STRIPE_PRICE_ID_MONTHLY", "")
STRIPE_PRICE_YEARLY = os.getenv("STRIPE_PRICE_ID_YEARLY", "")

_HERE = Path(__file__).parent
_PROS_FILE = _HERE / "pros.json"
_LEDGER_FILE = _HERE / "payments_ledger.jsonl"

# ── Store: melds in dict, pros in file ──────────────────────────────────

_melds: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()

# ── Rate limiter for GET endpoints ─────────────────────────────────────

class _RateLimiter:
    """Simple sliding-window per-IP rate limiter."""
    def __init__(self, limit: int, window_s: float):
        self._limit = limit
        self._window = window_s
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            hits = self._buckets.get(key)
            if hits is None:
                self._buckets[key] = [now]
                return True
            # Prune stale entries
            fresh = [t for t in hits if t > cutoff]
            if len(fresh) >= self._limit:
                self._buckets[key] = fresh
                return False
            fresh.append(now)
            self._buckets[key] = fresh
            return True

# 60 req/min per IP for view/result endpoints
_get_limiter = _RateLimiter(60, 60.0)
# 10 req/min per IP for resolve
_resolve_limiter = _RateLimiter(10, 60.0)
# 20 req/min per IP for create
_create_limiter = _RateLimiter(20, 60.0)

# ── Pro users (persisted to pros.json) ──────────────────────────────────

_pros: dict[str, dict] = {}  # ip → {email, customer_id, since}


def _load_pros():
    global _pros
    try:
        if _PROS_FILE.exists():
            with open(_PROS_FILE) as f:
                _pros = json.load(f)
    except Exception:
        _pros = {}


def _save_pros():
    """Atomically write pros.json via tempfile + os.replace."""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(_PROS_FILE.parent), prefix="pros_", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(_pros, f, indent=2)
        os.replace(tmp, str(_PROS_FILE))
    except Exception:
        pass


_load_pros()


def _is_pro(ip: str) -> bool:
    return ip in _pros


# ── T4: pro status as a LEASE + append-only ledger ──────────────────────
# Trust = recent evidence of payment, not a permanent flag. Every webhook
# event is recorded (append-only) and grants a bounded lease that self-heals
# missed cancellations and expires lapsed subs without webhook delivery.
# L1 privacy: emails are stored ONLY as SHA-256 hashes at rest — a leaked
# pros.json/ledger reveals no subscriber identities. Stripe retains the
# customer_id → email mapping; that is their compliance burden, not ours.
LEASE_DAYS = 35  # monthly cadence + slack
_payments_ledger: list[dict] = []


def _email_key(email: str) -> str:
    """Privacy-preserving at-rest identity: sha256 of lowercased email."""
    return "sha256:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _load_ledger():
    """Replay the append-only ledger on boot (survivability)."""
    if not _LEDGER_FILE.exists():
        return
    with open(_LEDGER_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    _payments_ledger.append(json.loads(line))
                except Exception:
                    pass  # a torn last line must not kill boot


def _append_ledger(entry: dict):
    """Append-only, fsync'd — audit trail survives crash and restart."""
    _payments_ledger.append(entry)
    with open(_LEDGER_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _record_payment_event(customer_id: str, email: str, days: int = LEASE_DAYS):
    """Grant/extend a pro lease and append an auditable ledger entry."""
    key = _email_key(email)
    with _lock:
        prior = _pros.get(key, {})
        _pros[key] = {
            "customer_id": customer_id,
            "pro_until": (_now() + timedelta(days=days)).isoformat(),
            "since": prior.get("since", _now().isoformat()),
        }
        _append_ledger({
            "at": _now().isoformat(),
            "event": "lease.grant",
            "customer_id": customer_id,
            "email_key": key,
            "days": days,
        })
        _save_pros()


def _pro_active(email: str) -> bool:
    """A lease is active only while unexpired. Expired leases self-clean."""
    key = _email_key(email)
    with _lock:
        p = _pros.get(key)
        if not p:
            return False
        if _now() > datetime.fromisoformat(p["pro_until"]):
            _pros.pop(key, None)
            _save_pros()
            return False
        return True


_load_ledger()  # replay append-only audit trail (L2 survivability)


# ── Helpers ─────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """Get client IP from X-Forwarded-For (first hop) or fall back to direct connection."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # LAST entry is appended by our own trusted proxy; leftmost entries
        # are client-controlled and trivially spoofable.
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "0.0.0.0"


# ── T5: proof-of-work gate (invisible when MELD_POW_DIFFICULTY=0) ───────
POW_DIFFICULTY = int(os.getenv("MELD_POW_DIFFICULTY", "0"))  # hex zeros required

def _pow_meets(digest_hex: str, difficulty: int) -> bool:
    return digest_hex.startswith("0" * difficulty) if difficulty > 0 else True

def _pow_check(nonce: str, solution: str, difficulty: int) -> bool:
    """True iff sha256(nonce+solution) has the required hex-zero prefix."""
    if difficulty <= 0:
        return True
    if not isinstance(solution, str) or len(solution) > 64:
        return False
    return _pow_meets(hashlib.sha256((nonce + solution).encode()).hexdigest(), difficulty)


def _code() -> str:
    # 36^12 ≈ 4.7e18 — not enumerable via any unthrottled channel
    return "".join(secrets.choice(string.ascii_lowercase + string.digits)
                   for _ in range(12))


def _token() -> str:
    return secrets.token_hex(32)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _expired(m: dict) -> bool:
    expires = datetime.fromisoformat(m["expires_at"]) if isinstance(m["expires_at"], str) else m["expires_at"]
    return _now() > expires


def _check_limit(ip: str) -> tuple[bool, str]:
    """Pro users skip the limit. Free users get FREE_LIMIT per window."""
    if _is_pro(ip):
        return True, ""
    since = _now() - timedelta(hours=FREE_EXPIRY_HOURS)
    count = 0
    with _lock:
        for m in _melds.values():
            if m.get("creator_ip") == ip:
                ca = datetime.fromisoformat(m["created_at"]) if isinstance(m["created_at"], str) else m["created_at"]
                if ca >= since:
                    count += 1
    if count >= FREE_LIMIT:
        return False, (
            "Free limit reached. "
            "<a href='/upgrade' class='link'>Go Pro ($5/mo)</a> for unlimited."
        )
    return True, ""


# ── TTL sweep ──────────────────────────────────────────────────────────

def _sweep():
    while True:
        time.sleep(60)
        try:
            with _lock:
                gone = [k for k, m in _melds.items() if _expired(m)]
                for k in gone:
                    del _melds[k]
        except Exception:
            pass  # a malformed entry must never kill the sweeper thread


threading.Thread(target=_sweep, daemon=True).start()


# ── Stripe ──────────────────────────────────────────────────────────────

def _stripe_enabled() -> bool:
    return bool(STRIPE_KEY)


def _stripe_client() -> stripe.Stripe:
    if not STRIPE_KEY:
        raise HTTPException(501, "Payments not configured")
    return stripe.Stripe(STRIPE_KEY)


# ── FastAPI ─────────────────────────────────────────────────────────────

app = FastAPI(title="meld", version="0.2.0", docs_url=None, redoc_url=None)

# Restrict CORS to the meld origin (no wildcard)
allowed_origins = [PUBLIC_URL]
# Also allow localhost for dev
if "localhost" in PUBLIC_URL or "127.0.0.1" in PUBLIC_URL:
    allowed_origins.append(PUBLIC_URL)
else:
    # Dev fallback: localhost only, no wildcard
    allowed_origins.append("http://localhost:8080")
    allowed_origins.append("http://127.0.0.1:8080")

app.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Meld-Token"])


# ── Security headers middleware ─────────────────────────────────────────

@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "no-referrer"
    # CSP: restrict scripts to same-origin, inline for SPA, no external
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://js.stripe.com; "
        "frame-src https://js.stripe.com https://hooks.stripe.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'"
    )
    return response


# ── Body size limit middleware ──────────────────────────────────────────

@app.middleware("http")
async def _body_size_limit(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request too large (max {MAX_BODY_BYTES} bytes)"},
                )
        except ValueError:
            pass
    return await call_next(request)


# ── JSON body middleware ────────────────────────────────────────────────

@app.middleware("http")
async def _parse_json(request: Request, call_next):
    if request.headers.get("content-type", "").startswith("application/json"):
        # Stream with a hard budget — a chunked body with no content-length
        # cannot smuggle unbounded bytes into RAM (DoS hardening).
        total = 0
        chunks = []
        too_big = False
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                too_big = True
                break
            chunks.append(chunk)
        if too_big:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        try:
            request.state.json = json.loads(b"".join(chunks) or b"{}")
            if not isinstance(request.state.json, dict):
                request.state.json = {}
        except Exception:
            request.state.json = {}
    else:
        request.state.json = {}
    return await call_next(request)


# ── API: create meld ───────────────────────────────────────────────────

@app.post("/api/melds")
def create_meld(request: Request):
    body = request.state.json
    ip = _client_ip(request)

    allowed, reason = _check_limit(ip)
    if not allowed:
        raise HTTPException(429, reason, headers={"Retry-After": "60"})

    context = body.get("context", "")
    if not isinstance(context, str):
        raise HTTPException(400, "Context must be a string")
    if len(context) > 100_000:
        raise HTTPException(400, "Context too large (100K max)")
    # DoS hardening: per-field caps — reject, don't silently truncate
    if "pin" in body and body["pin"] is not None:
        if not isinstance(body["pin"], str) or len(body["pin"]) > 128:
            raise HTTPException(400, "PIN must be a string of at most 128 chars")
    if "email" in body and body["email"] is not None:
        if not isinstance(body["email"], str) or len(body["email"]) > 254:
            raise HTTPException(400, "Email must be a string of at most 254 chars")

    if not _create_limiter.check(ip):
        raise HTTPException(429, "Too many requests. Please slow down.", headers={"Retry-After": "60"})

    code = _code()
    now = _now()
    meld = {
        "code": code,
        "context_a": context,
        "context_b": None,
        "resolved": False,
        "resolved_at": None,
        "owner_token": _token(),
        "owner_email": (body["email"][:254] if isinstance(body.get("email"), str)
                        and 0 < len(body["email"]) <= 254 else None),
        # T1: optional resolve PIN, stored hashed — split-channel trust
        # (DoS: fields get per-field caps — nothing unbounded is stored)
        "pin": hashlib.sha256(body["pin"].encode()).hexdigest()
               if isinstance(body.get("pin"), str) and 0 < len(body["pin"]) <= 128 else None,
        "creator_ip": ip,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=FREE_EXPIRY_HOURS)).isoformat(),
    }
    with _lock:
        # G3: global RAM guard — per-IP limits don't stop a distributed filler.
        if len(_melds) >= MAX_LIVE_MELDS and not _is_pro(ip):
            raise HTTPException(429, "Service at capacity. Try again soon.", headers={"Retry-After": "120"})
        _melds[code] = meld

    return {
        "code": code,
        "url": f"{PUBLIC_URL}/m/{code}",
        # B1 two-link custody: the owner link carries the token in the URL
        # fragment — never transmitted, lands in no server log. Bookmark = deed.
        "owner_url": f"{PUBLIC_URL}/m/{code}#t={meld['owner_token']}",
        "owner_token": meld["owner_token"],
        "context_a": context,
        "resolved": False,
    }


# ── API: get meld ──────────────────────────────────────────────────────

@app.get("/api/melds/{code}")
def get_meld(code: str, request: Request):
    ip = _client_ip(request)
    if not _get_limiter.check(ip):
        raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})

    with _lock:
        m = _melds.get(code)
    if not m:
        raise HTTPException(404, "Meld not found")
    if _expired(m):
        raise HTTPException(410, "This meld has expired")

    return {
        "code": m["code"],
        "context_a": m["context_a"],
        "context_b": m["context_b"],
        "resolved": m["resolved"],
        "resolved_at": m["resolved_at"],
    }


# ── API: resolve meld ──────────────────────────────────────────────────

@app.post("/api/melds/{code}/resolve")
def resolve_meld(code: str, request: Request):
    body = request.state.json
    ip = _client_ip(request)

    if not _resolve_limiter.check(ip):
        raise HTTPException(429, "Too many requests. Please slow down.", headers={"Retry-After": "60"})

    with _lock:
        m = _melds.get(code)
        if not m:
            raise HTTPException(404, "Meld not found")
        if _expired(m):
            raise HTTPException(410, "This meld has expired")
        context = body.get("context", "")
        if not isinstance(context, str):
            raise HTTPException(400, "Context must be a string")
        if len(context) > 100_000:
            raise HTTPException(400, "Context too large (100K max)")

        # T6 evidence shape-validated before any state checks (fail fast)
        pk = body.get("responder_pubkey")
        sig = body.get("signature")
        if (pk is None) != (sig is None):
            raise HTTPException(400, "Signature evidence requires both responder_pubkey and signature")
        if pk is not None:
            if (not isinstance(pk, str) or len(pk) != 64
                    or any(c not in "0123456789abcdef" for c in pk.lower())):
                raise HTTPException(400, "responder_pubkey must be 64 hex chars")
            if (not isinstance(sig, str) or not (64 <= len(sig) <= 256)
                    or any(c not in "0123456789abcdef" for c in sig.lower())):
                raise HTTPException(400, "signature must be 64-256 hex chars")

        if m.get("pin"):
            supplied = body.get("pin", "")
            if not isinstance(supplied, str) or not secrets.compare_digest(
                    m["pin"], hashlib.sha256(supplied.encode()).hexdigest()):
                raise HTTPException(403, "Invalid PIN")
        if m["resolved"]:
            # B3: a retry with the identical answer is a safe no-op; a
            # different answer is a genuine conflict.
            if m["context_b"] == context:
                return {
                    "code": m["code"],
                    "context_a": m["context_a"],
                    "context_b": m["context_b"],
                    "resolved": True,
                    "retry": True,
                }
            raise HTTPException(409, "Already resolved with a different answer")

        # T6: evidence stored (shape validated above). Server is content-blind.
        m["context_b"] = context
        m["responder_pubkey"] = pk.lower() if pk else None
        m["signature"] = sig.lower() if sig else None
        m["resolved"] = True
        m["resolved_at"] = _now().isoformat()

    return {
        "code": m["code"],
        "context_a": m["context_a"],
        "context_b": m["context_b"],
        "resolved": True,
    }


# ── API: check result (owner only — token via header) ───────────────────

@app.get("/api/melds/{code}/result")
def get_result(code: str, request: Request):
    ip = _client_ip(request)
    if not _get_limiter.check(ip):
        raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})

    # Read token from header, not query param (avoids URL logging)
    token = request.headers.get("X-Meld-Token", "")

    with _lock:
        m = _melds.get(code)
    if not m:
        raise HTTPException(404, "Meld not found")
    if not secrets.compare_digest(m["owner_token"], token or ""):
        raise HTTPException(403, "Invalid token")
    if not m["resolved"]:
        raise HTTPException(400, "Not yet resolved")
    # T2 rotating deed: every successful read invalidates the presented token
    # and issues a fresh one. A leaked token dies the moment the true owner reads.
    fresh = _token()
    m["owner_token"] = fresh
    return {
        "code": m["code"],
        "context_a": m["context_a"],
        "context_b": m["context_b"],
        "resolved": True,
        "owner_token": fresh,
        "responder_pubkey": m.get("responder_pubkey"),
        "signature": m.get("signature"),
    }


# ── Stripe: create checkout session ─────────────────────────────────────

@app.get("/api/checkout")
def checkout(plan: str = "monthly", email: str = ""):
    client = _stripe_client()
    price_id = STRIPE_PRICE_MONTHLY if plan == "monthly" else STRIPE_PRICE_YEARLY
    if not price_id:
        raise HTTPException(500, "Price ID not configured")

    session = client.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=email or None,
        success_url=f"{PUBLIC_URL}/pro?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_URL}",
    )
    return {"url": session.url}


# ── Stripe: webhook ────────────────────────────────────────────────────

@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(501, "Webhook not configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        import stripe  # optional dependency
    except ImportError:
        raise HTTPException(501, "Payments unavailable: stripe package not installed")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(400, "Invalid signature")

    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        _handle_payment(sess)
    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        _handle_cancellation(sub.get("customer", ""))

    return {"ok": True}


def _handle_payment(session: dict):
    """Called on checkout.session.completed. Marks the user as pro."""
    email = (session.get("customer_details") or {}).get("email") or ""
    customer_id = session.get("customer") or ""

    _record_payment_event(customer_id, email)
    _log.info("PRO lease granted: email=%s customer=%s", email, customer_id)


def _handle_cancellation(customer_id: str):
    """Called when a subscription is deleted. Removes pro status."""
    with _lock:
        for k in [k for k, v in _pros.items() if v.get("customer_id") == customer_id]:
            _pros.pop(k, None)
    _save_pros()
    _log.info("PRO deactivated: customer=%s", customer_id)


# ── API: check pro status by email ──────────────────────────────────────

@app.get("/api/pro-status")
def pro_status(email: str = ""):
    """Check if an email is pro. Returns {pro: bool} only — no token leak."""
    if not email:
        return {"pro": False}
    if _pro_active(email):
        return {"pro": True}
    return {"pro": False}


# ── Single HTML page ──────────────────────────────────────────────────

_PAGE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>meld — ephemeral context bridge</title>
<meta name="description" content="One link. Pour in context. Done. Ephemeral context bridge for humans and agents.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect x='2' y='14' width='12' height='4' rx='2' fill='%23a78bfa'/><rect x='18' y='14' width='12' height='4' rx='2' fill='%238b5cf6'/><circle cx='8' cy='14' r='3' fill='%23a78bfa'/><circle cx='24' cy='14' r='3' fill='%238b5cf6'/></svg>">
<style>
/* reset + tokens */
:root{--bg:#0a0a0f;--card:#12121a;--input:#1a1a25;--border:#2a2a3a;--text:#e4e4f0;--dim:#8888a0;--muted:#5a5a72;--accent:#8b5cf6;--accent-h:#a78bfa;--accent-g:rgba(139,92,246,.25);--accent-s:rgba(139,92,246,.08);--success:#34d399;--err:#fca5a5;--code:#181825;--r:12px;--rs:8px;--ff:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;--fm:'SF Mono','JetBrains Mono',monospace}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--ff);line-height:1.6;min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{color:var(--accent-h)}
.layout{max-width:680px;margin:0 auto;padding:2rem 1.5rem;display:flex;flex-direction:column;min-height:100vh}
.header{display:flex;align-items:center;gap:.75rem;margin-bottom:2.5rem;flex-shrink:0}
.logo{display:flex;align-items:center;gap:.45rem;font-size:1.25rem;font-weight:700;color:var(--text)}
.logo svg{width:28px;height:28px}
.nav{margin-left:auto;display:flex;gap:1.25rem;align-items:center}
.nav a{font-size:.875rem;color:var(--dim);transition:color .15s}
.nav a:hover{color:var(--text)}
.badge{display:inline-block;font-size:.6875rem;font-weight:600;padding:.25rem .5rem;border-radius:4px;background:rgba(52,211,153,.12);color:var(--success);margin-left:.5rem}
.main{flex:1}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:2rem;margin-bottom:1.5rem}
.card:last-child{margin-bottom:0}
.card h1{font-size:1.5rem;font-weight:700;letter-spacing:-.02em;margin-bottom:.75rem}
.card p{color:var(--dim);margin-bottom:1rem;font-size:.9375rem}
.hero{font-size:2.75rem;font-weight:800;letter-spacing:-.03em;line-height:1.1;margin-bottom:.75rem}
.hero em{font-style:normal;color:var(--accent)}
.hero .d{color:var(--dim);font-weight:500}
.sub{font-size:1.05rem;color:var(--dim);margin-bottom:2rem;max-width:520px}
textarea{width:100%;background:var(--input);border:1px solid var(--border);border-radius:var(--rs);color:var(--text);font-family:var(--fm);font-size:.875rem;padding:.875rem;resize:vertical;min-height:120px;transition:border-color .2s}
textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-g)}
input[type=email]{width:100%;background:var(--input);border:1px solid var(--border);border-radius:var(--rs);color:var(--text);font-size:.9375rem;padding:.75rem .875rem;transition:border-color .2s}
input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-g)}
.btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;font-weight:600;border:none;border-radius:var(--rs);padding:.75rem 1.5rem;font-size:.9375rem;cursor:pointer;transition:all .2s}
.btn:hover{background:var(--accent-h);transform:translateY(-1px)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-s{border:1px solid var(--border);background:transparent;color:var(--accent);font-weight:500;padding:.5rem 1rem;font-size:.8125rem;cursor:pointer;transition:all .2s;border-radius:var(--rs)}
.btn-s:hover{background:var(--accent-s);border-color:var(--accent)}
.link-box{background:var(--input);border:1px solid var(--border);border-radius:var(--rs);padding:.75rem 1rem;display:flex;align-items:center;gap:.75rem;font-family:var(--fm);font-size:.875rem;word-break:break-all}
.link-box .cp{margin-left:auto;flex-shrink:0;background:var(--accent-s);border:none;color:var(--accent);cursor:pointer;padding:.25rem .75rem;border-radius:4px;font-size:.75rem;font-weight:600;transition:all .15s}
.link-box .cp:hover{background:var(--accent);color:#fff}
.link-box .cp.done{background:var(--success);color:#000}
.ctx{background:var(--code);border:1px solid var(--border);border-radius:var(--rs);padding:1.25rem;font-family:var(--fm);font-size:.8125rem;line-height:1.55;white-space:pre-wrap;max-height:300px;overflow-y:auto;color:var(--text)}
.ctx:empty:before{content:'(empty)';color:var(--muted)}
.lbl{font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:.5rem;display:block;margin-top:1rem}
.lbl:first-child{margin-top:0}
.flash{padding:.75rem 1rem;border-radius:var(--rs);margin-bottom:1rem;font-size:.875rem}
.flash-ok{background:rgba(52,211,153,.12);border:1px solid rgba(52,211,153,.25);color:#34d399}
.flash-info{background:var(--accent-s);border:1px solid var(--accent-g);color:var(--accent)}
.flash-err{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.25);color:var(--err)}
.merge{display:flex;align-items:center;gap:1rem;margin:1.5rem 0;color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}
.merge:before,.merge:after{content:'';flex:1;height:1px;background:var(--border)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:.375rem;background:var(--dim)}
.dot-ok{background:var(--success)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.spin{display:inline-block;width:14px;height:14px;border-radius:50%;border:2px solid var(--border);border-top-color:var(--accent);animation:s .6s infinite linear}
@keyframes s{to{transform:rotate(360deg)}}
.ep{text-align:center;font-size:.8125rem;color:var(--muted);padding:1rem}
.footer{margin-top:auto;padding-top:3rem;text-align:center;font-size:.8125rem;color:var(--muted);flex-shrink:0}
@media(max-width:540px){.hero{font-size:1.75rem}.grid{grid-template-columns:1fr}.layout{padding:1.5rem 1rem}}
.step{display:flex;gap:.75rem;align-items:flex-start}
.step-n{flex-shrink:0;width:28px;height:28px;border-radius:50%;background:var(--accent-s);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:700}
.step-c{padding-top:.125rem}
.step-c strong{display:block;font-size:.9375rem}
.step-c p{font-size:.875rem;color:var(--dim);margin:0}
.cb{background:var(--code);border:1px solid var(--border);border-radius:var(--rs);padding:1rem;font-family:var(--fm);font-size:.8125rem;line-height:1.6;overflow-x:auto;white-space:pre}
.cb .c{color:var(--muted)}
.cb .d{color:var(--dim)}
.pg{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:1.5rem 0}
.pc{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:1.5rem;text-align:center;transition:border-color .2s}
.pc:hover{border-color:var(--accent)}
.pc.f{border-color:var(--accent);background:rgba(139,92,246,.04)}
.pc .pr{font-size:2rem;font-weight:800;letter-spacing:-.03em;margin:.5rem 0}
.pc .pr span{font-size:.875rem;font-weight:500;color:var(--dim)}
.pc ul{list-style:none;margin:1rem 0;font-size:.8125rem;color:var(--dim)}
.pc ul li{padding:.25rem 0}
@media(max-width:540px){.pg{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="layout">
<div class="header">
<div class="logo"><svg viewBox="0 0 32 32" fill="none"><rect x="2" y="14" width="12" height="4" rx="2" fill="currentColor" opacity=".6"/><rect x="18" y="14" width="12" height="4" rx="2" fill="currentColor"/><circle cx="8" cy="14" r="3" fill="currentColor" opacity=".6"/><circle cx="24" cy="14" r="3" fill="currentColor"/></svg>meld</div>
<div class="nav"><a href="/">Create</a><a href="/trust">Trust</a><a href="/upgrade" id="nav-pro">Pro</a></div>
</div>
<div class="main" id="root"></div>
<div class="footer"><p><em>Don't meet. Meld.</em> · ephemeral by design</p></div>
</div>
<script>
const API=location.origin;
let _m=null;

async function api(m,p,b,h){const r=await fetch(API+p,{method:m,headers:{'Content-Type':'application/json',...h},body:b?JSON.stringify(b):null});if(!r.ok){const d=await r.json().catch(()=>({detail:r.statusText}));throw new Error(d.detail||r.status)}return r.json()}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
const _td=new TextDecoder();const _te=new TextEncoder();
async function _key(khex){const raw=new Uint8Array(khex.match(/.{2}/g).map(h=>parseInt(h,16)));return crypto.subtle.importKey('raw',raw,'AES-GCM',false,['encrypt','decrypt'])}
async function _enc(plain,khex){const k=await _key(khex);const nonce=crypto.getRandomValues(new Uint8Array(12));const ct=await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},k,_te.encode(plain));const buf=new Uint8Array(ct);const out=new Uint8Array(12+buf.length);out.set(nonce);out.set(buf,12);let bin='';out.forEach(b=>bin+=String.fromCharCode(b));return 'meld1:'+btoa(bin)}
async function _dec(blob,khex){if(!blob.startsWith('meld1:'))return blob;try{const k=await _key(khex);const bin=atob(blob.slice(6));const buf=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)buf[i]=bin.charCodeAt(i);const pt=await crypto.subtle.decrypt({name:'AES-GCM',iv:buf.slice(0,12)},k,buf.slice(12));return _td.decode(pt)}catch(e){console.error('meld _dec failed:',e.message);throw e}}
function cp(t,b){navigator.clipboard?navigator.clipboard.writeText(t):(function(e){e.value=t;e.select();document.execCommand('copy');document.body.removeChild(e)}(document.createElement('textarea')));b.textContent='Copied!';b.classList.add('done');setTimeout(()=>{b.textContent='Copy';b.classList.remove('done')},2000)}

async function C(){const e=document.getElementById('go');const s=document.getElementById('st');e.disabled=true;s.textContent='Creating...';try{let _k=null;const _ctxPlain=document.getElementById('ctx').value;let _ctxSend=_ctxPlain;
if(document.getElementById('e2e')&&document.getElementById('e2e').checked){_k=[...crypto.getRandomValues(new Uint8Array(32))].map(b=>b.toString(16).padStart(2,'0')).join('');_ctxSend=await _enc(_ctxPlain,_k)}
const d=await api('POST','/api/melds',{context:_ctxSend,email:document.getElementById('em').value});
if(_k){d.keyed_url=d.url+'#k='+_k;document.getElementById('lk').textContent=d.keyed_url;document.getElementById('sh').textContent=d.url;document.getElementById('e2eNote').style.display='block'}else{document.getElementById('lk').textContent=d.owner_url;document.getElementById('sh').textContent=d.url}document.getElementById('f').style.display='none';const r=document.getElementById('r');r.style.display='block';document.getElementById('pv').textContent=d.context_a||'(empty)';s.textContent=''}catch(e){const m=e.message||'';if(m.includes('429')||m.includes('free')){s.innerHTML='Free limit hit. <a href="/upgrade" class="link">Go Pro ($5/mo)</a> for unlimited.'}else{s.textContent=m}}finally{e.disabled=false}}

async function ck(){if(!_m)return;const r=document.getElementById('res');r.innerHTML='<span class="spin"></span>';document.getElementById('ckb').disabled=true;try{const d=await api('GET','/api/melds/'+_m.code+'/result',null,{'X-Meld-Token':_m.owner_token});if(d.owner_token){_m.owner_token=d.owner_token}r.innerHTML='<div class="flash flash-ok">Got response!</div>'+(d.context_b?'<label class="lbl">Their context</label><div class="ctx">'+esc(d.context_b)+'</div>':'');const dot=document.querySelector('#r .dot');if(dot)dot.className='dot dot-ok'}catch(e){r.innerHTML='<p style="font-size:.875rem;color:var(--dim)">Not yet resolved — check back later</p>'}finally{document.getElementById('ckb').disabled=false}}

async function R(){const e=document.getElementById('rb');const s=document.getElementById('rst');e.disabled=true;s.innerHTML='<span class="spin"></span>';try{let _ans=document.getElementById('ctxb').value;const _fk2=(location.hash.match(/k=([0-9a-f]+)/)||[])[1];const _encMeld=document.getElementById('ta');const _isEncMeld=_encMeld&&_encMeld.getAttribute('data-enc')==='1';if(_isEncMeld&&!_fk2){document.getElementById('rst').textContent='This meld is end-to-end encrypted. Open the original link with the #k= key to answer.';e.disabled=false;return}if(_fk2){_ans=await _enc(_ans,_fk2)}
const d=await api('POST','/api/melds/'+_c+'/resolve',{context:_ans});document.getElementById('rf').style.display='none';document.getElementById('rd').style.display='block';document.getElementById('ta').textContent=d.context_a||'(empty)';document.getElementById('tb').textContent=d.context_b||'(empty)';s.textContent=''}catch(e){s.textContent=e.message;e.disabled=false}}

let _c=null;
let _pro=false;

async function init(){
const p=window.location.pathname;
const r=document.getElementById('root');
const n=document.getElementById('nav-pro');

if(p==='/'||p===''){r.innerHTML=L();const _cp=document.getElementById('cpbtn');if(_cp)_cp.onclick=function(){cp(document.getElementById('lk').textContent,this)}}
else if(p.startsWith('/upgrade')){r.innerHTML=UP();n.textContent='Pro \u2713';n.style.color='#34d399'}
else if(p.startsWith('/pro')){r.innerHTML=PR();n.textContent='Pro \u2713';n.style.color='#34d399'}
else if(p.startsWith('/m/')){_c=p.split('/m/')[1];const _ft=(location.hash.match(/t=([0-9a-f]{64})/)||[])[1];if(_ft&&!_m)_m={code:_c,owner_token:_ft};const _fk=(location.hash.match(/k=([0-9a-f]+)/)||[])[1];console.error('MELDDBG init _fk='+(_fk?_fk.length:'null')+' hash='+location.hash);try{const d=await api('GET','/api/melds/'+_c);let a=d.context_a,b=d.context_b;const _isEnc=(typeof a==='string'&&a.startsWith('meld1:'));if(_fk&&_isEnc){a=await _dec(a,_fk);if(b)b=await _dec(b,_fk)}else if(_isEnc){console.error('meld debug: ENC_CARD with _fk='+_fk);r.innerHTML=ENC_CARD();return}if(d.resolved&&_ft){const _rr=await fetch('/api/melds/'+_c+'/result',{headers:{'X-Meld-Token':_ft}});if(_rr.ok){const _rd=await _rr.json();r.innerHTML=RD(_rd.context_a,_rd.context_b)}else{r.innerHTML=RD(a,'(owner view: token rejected)')}}else if(d.resolved){r.innerHTML=RD(a,b)}else{r.innerHTML=M(a);if(_ft){const _w=document.createElement('div');_w.style.textAlign='center';_w.style.marginTop='1rem';_w.innerHTML='<button class="btn btn-s" id="ckb">Check for response</button><div id="res"></div>';document.querySelector('.card').appendChild(_w);document.getElementById('ckb').onclick=ck}}}catch(e){const m=e.message||'';if(m.includes('410')||m.includes('expired')){r.innerHTML='<div class="card" style="text-align:center"><h1>Gone</h1><p style="color:var(--dim)">This meld has expired.</p><a href="/" class="btn" style="margin-top:1rem">Create</a></div>'}else{r.innerHTML='<div class="card" style="text-align:center"><h1>Not found</h1><p style="color:var(--dim)">This meld doesn\\'t exist.</p><a href="/" class="btn" style="margin-top:1rem">Create</a></div>'}}}
else{r.innerHTML='<div class="card" style="text-align:center"><h1>Not found</h1><p style="color:var(--dim)">Page not found.</p><a href="/" class="btn" style="margin-top:1rem">Create</a></div>'}

try{const d=await api('GET','/api/pro-status?email='+encodeURIComponent(localStorage.getItem('meld_email')||''));_pro=d.pro;if(_pro){n.textContent='Pro \u2713';n.style.color='#34d399';n.href='/upgrade'}}catch(e){}
}

function L(){return`<div style="text-align:left"><h1 class="hero">One link. Pour in context.<br><em>Done.</em> <span class="d">Gone.</span></h1><p class="sub">A primitive for context handoff between humans and AI agents. Create the link, share it, each party provides their context, and when the exchange resolves the link dissolves. <strong>No history. No threads. No accounts.</strong></p></div><div class="card"><h1>Create a meld</h1><p>Paste what you need to share, get a link.</p><div id="f"><label class="lbl">Your context</label><textarea id="ctx" placeholder="Code, requirements, logs, a prompt, a problem — anything."></textarea><label class="lbl">Email <span style="color:var(--muted);font-weight:400;text-transform:none">(for pro notification — optional)</span></label><input type="email" id="em" placeholder="you@example.com"><label style="display:flex;gap:.5rem;align-items:center;margin-top:.75rem;font-size:.8125rem;color:var(--dim);cursor:pointer"><input type="checkbox" id="e2e" style="accent-color:var(--accent)"> End-to-end encrypted — the server can\'t read your context. <strong style="color:var(--text)">You must keep the link: losing it loses the meld.</strong></label><p style="font-size:.75rem;color:var(--muted);margin-top:.5rem">Trust model: without E2E, the server can read your content while it exists (max 1 hour). With E2E, the server stores ciphertext it cannot open. Full details: <a href="/trust" class="link" target="_blank" rel="noopener">how meld handles trust</a>.</p><div style="margin-top:1.25rem"><button class="btn" id="go" onclick="C()">Generate meld link</button></div><div id="st" style="margin-top:.75rem;font-size:.875rem;color:var(--dim)"></div></div><div id="r" style="display:none"><div class="flash flash-ok">Meld created! Share this link:</div><div class="link-box"><span id="lk"></span><button class="cp" id="cpbtn">Copy owner link</button></div><p style="font-size:.75rem;color:var(--muted);margin-top:.5rem">Owner link = proof of ownership. Bookmark it. Share link (no token): <span id="sh" style="font-family:var(--fm)"></span></p><div id="e2eNote" style="display:none;margin-top:.75rem;padding:.75rem;border:1px solid rgba(139,92,246,.35);border-radius:8px;background:rgba(139,92,246,.06)"><strong style="font-size:.8125rem">This meld is end-to-end encrypted.</strong><p style="font-size:.75rem;color:var(--dim);margin-top:.25rem">The link above contains the decryption key after <span style="font-family:var(--fm)">#k=</span>. Send Party B the <span style="font-family:var(--fm)">Share link</span> plus tell them the key separately — or just send them this owner link (they could read it). <strong style="color:#fca5a5">If you lose this link, the content is unrecoverable.</strong></p></div><label class="lbl">Your context</label><div class="ctx" id="pv"></div><div class="merge">waiting for party B</div><p style="text-align:center;color:var(--dim);font-size:.875rem"><span class="dot"></span> Not yet resolved<br><button class="btn btn-s" id="ckb" onclick="ck()" style="margin-top:.5rem">Check for response</button></p><div id="res"></div></div></div><div class="card"><h1>How it works</h1><div class="step"><div class="step-n">1</div><div class="step-c"><strong>Create</strong><p>Paste your context. Get a link.</p></div></div><div class="step" style="margin-top:.75rem"><div class="step-n">2</div><div class="step-c"><strong>Share</strong><p>Send it to anyone — Slack, email, text, an agent.</p></div></div><div class="step" style="margin-top:.75rem"><div class="step-n">3</div><div class="step-c"><strong>Resolve</strong><p>They open the link, pour in their side. Done.</p></div></div><div class="step" style="margin-top:.75rem"><div class="step-n">4</div><div class="step-c"><strong>Gone</strong><p>The link dissolves. No trace. Ephemeral by design.</p></div></div></div><div class="card"><h1>For AI agents</h1><p>A wire protocol for inter-agent context exchange.</p><div class="cb"><span class="c"># Agent A creates</span>\\ncurl -X POST {API}/api/melds -H "Content-Type: application/json" -d '{"context": "What architecture fits 10M users?"}'\\n<span class="c"># Agent B resolves</span>\\ncurl -X POST {API}/api/melds/a1b2c3d4/resolve -H "Content-Type: application/json" -d '{"context": "Event-driven microservices + Kafka"}'\\n<span class="c"># Read result</span>\\ncurl {API}/api/melds/a1b2c3d4</div></div>'`.split("{API}").join(API)}\n\nfunction ENC_CARD(){return'<div class="card" style="text-align:center"><h1>Encrypted meld</h1><p style="color:var(--dim)">This meld is end-to-end encrypted. Open the original link you were given (it contains the key after #k=) to read it.</p><a href="/" class="btn" style="margin-top:1rem">Create a meld</a></div>'}\n\nfunction M(c){return'<div class="card"><h1>Meld received</h1><p>Someone shared context with you.</p><div class="flash flash-info"><strong>Their context:</strong></div><div class="ctx">'+esc(c)+'</div><div id="rf"><label class="lbl" style="margin-top:1rem">Your context</label><textarea id="ctxb" placeholder="What they need."></textarea><div style="margin-top:1.25rem;display:flex;align-items:center;gap:.75rem"><button class="btn" id="rb" onclick="R()">Resolve \u2192</button><span id="rst" style="font-size:.875rem;color:var(--dim)"></span></div></div><div id="rd" style="display:none"><div class="flash flash-ok">Exchange complete. The bridge dissolves.</div><div class="grid"><div><label class="lbl">Their context</label><div class="ctx" id="ta"></div></div><div><label class="lbl">Your context</label><div class="ctx" id="tb"></div></div></div><p class="ep">Ephemeral by design.</p></div></div>'}

function RD(a,b){return'<div class="card"><div style="display:flex;align-items:center;gap:.75rem;margin-bottom:1rem"><span class="dot dot-ok" style="width:10px;height:10px"></span><h1 style="margin:0">Exchange complete</h1></div><p>Both contexts merged.</p><div class="grid"><div><label class="lbl">Party A</label><div class="ctx">'+esc(a)+'</div></div><div><label class="lbl">Party B</label><div class="ctx">'+esc(b)+'</div></div></div><div class="merge">ephemeral</div><p class="ep">No history. No threads. No permanent record.</p></div>'}

function UP(){return'<div><div class="card" style="text-align:left"><h1>Fair pricing for<br>a single primitive.</h1><p>One link. One exchange. No suites, no enterprise gating, no feature tiers. Just a faster, higher-volume pipe.</p></div><div class="pg"><div class="pc"><div style="font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)">Free</div><div class="pr">$0 <span></span></div><ul><li>3 melds per day</li><li>1 hour expiry</li><li>Plain text</li><li>No credit card</li></ul><a href="/" class="btn btn-s" style="width:100%;justify-content:center">Get started</a></div><div class="pc f"><div style="font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--success)">Pro</div><div class="pr">$5 <span>/month</span></div><ul><li>Unlimited melds</li><li>7 day expiry</li><li>Rich context</li><li>API access</li><li>Priority cleanup</li></ul><button class="btn" style="width:100%;justify-content:center" onclick="co()">Subscribe</button><p style="margin-top:.5rem;font-size:.75rem;color:var(--dim)"><a href="#" onclick="co(\\'yearly\\')">$49/year</a> &middot; save 18%</p></div></div><div class="card" style="text-align:center"><p>Stripe-powered. Cancel anytime. No lock-in.</p></div></div>'}\n\nfunction PR(){const s=new URLSearchParams(location.search).get('session_id')||'';if(s){localStorage.setItem('meld_pro','true')}return'<div class="card" style="text-align:center;padding:3rem 2rem"><div style="font-size:3rem;margin-bottom:.5rem">🎉</div><h1>You&#39;re Pro</h1><p style="max-width:400px;margin:.5rem auto 1.5rem;color:var(--dim)">Unlimited melds, 7-day expiry, priority cleanup. Ephemeral by design, unlimited by subscription.</p><a href="/" class="btn">Create a meld</a></div>'}

async function co(p){p=p||'monthly';const e=prompt('Email for subscription:');if(!e)return;localStorage.setItem('meld_email',e);try{const d=await api('GET','/api/checkout?plan='+p+'&email='+encodeURIComponent(e));window.location.href=d.url}catch(e){alert('Payments not configured yet.')}}

init();
</script>
</body>
</html>"""


@app.get("/m/{code}")
async def serve_meld(request: Request, code: str):
    """One URL, two audiences: HTML for humans, JSON for agents (B2)."""
    if "application/json" in request.headers.get("accept", ""):
        ip = _client_ip(request)
        if not _get_limiter.check(ip):
            raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})
        with _lock:
            m = _melds.get(code)
        if not m:
            raise HTTPException(404, "Meld not found")
        if _expired(m):
            raise HTTPException(410, "This meld has expired")
        return {
            "code": m["code"],
            "context_a": m["context_a"],
            "context_b": m["context_b"],
            "resolved": m["resolved"],
            "resolved_at": m["resolved_at"],
            "api": {
                "resolve": f"POST {PUBLIC_URL}/api/melds/{code}/resolve {{\"context\": \"...\"}}",
                "result": f"GET {PUBLIC_URL}/api/melds/{code}/result (X-Meld-Token header, owner only)",
            },
        }
    return HTMLResponse(_PAGE)


@app.get("/trust", response_class=HTMLResponse)
async def serve_trust():
    """The trust model, stated to users in plain language. Keep in sync with TRUST.md."""
    return HTMLResponse(f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>meld — trust model</title>
<style>body{{background:#0a0a0f;color:#e4e4f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.6;max-width:680px;margin:0 auto;padding:2rem 1.5rem}}h1{{letter-spacing:-.02em}}h2{{margin-top:2rem;font-size:1.1rem}}table{{border-collapse:collapse;width:100%;font-size:.875rem}}td,th{{border:1px solid #2a2a3a;padding:.5rem;text-align:left}}th{{background:#12121a}}code{{background:#181825;padding:.1rem .3rem;border-radius:4px;font-family:'SF Mono',monospace;font-size:.85em}}a{{color:#a78bfa}}.box{{background:#12121a;border:1px solid #2a2a3a;border-radius:12px;padding:1.25rem;margin:1rem 0}}</style>
</head><body>
<p><a href="/">← meld</a></p>
<h1>The meld trust model</h1>
<p>meld has <strong>no accounts</strong>. Authority comes from secrets you hold, never from an identity we assign. Here is exactly what that means, layer by layer.</p>

<h2>Standard meld</h2>
<div class="box"><p>The server <strong>can read</strong> your context and the answer while the meld exists (at most 1 hour; 10 minutes after resolution). It can see your IP address (for rate limiting only) and your email if you provide one. It cannot tell who read a link, and it never inspects or moderates content.</p></div>

<h2>End-to-end encrypted meld</h2>
<div class="box"><p>If you check <strong>End-to-end encrypted</strong>, your browser encrypts everything (AES-256-GCM) before it is sent. The server stores <strong>ciphertext it cannot open</strong>. The decryption key lives only in your link's <code>#k=</code> fragment — it is never transmitted to any server. Even a full server breach cannot read your content.</p>
<p style="color:#fca5a5"><strong>The tradeoff: lose the link, lose the meld.</strong> There is no key on the server to recover with.</p></div>

<h2>Ownership</h2>
<p>The creator's token is the <strong>only</strong> proof of ownership. It rotates on every read — a leaked token dies the next time you read the result. There is no "recover my meld." If you lose both the token and the link, the content is gone at expiry, by design.</p>

<h2>What we cannot protect you from</h2>
<p>Whoever holds the link can read a standard meld's context — share it as carefully as the content itself. A malicious Party B can answer with lies (set a PIN and verify signatures to mitigate). Metadata — timing, size, IPs — is visible even on encrypted melds.</p>

<h2>Verify, don't trust</h2>
<p>Don't take our word for it. Create an end-to-end encrypted meld, then fetch <code>GET /api/melds/&lt;code&gt;</code> — you will see <code>meld1:…</code> ciphertext, not your text. The encryption happens in your browser, before the server ever sees a byte.</p>
</body></html>""")


@app.get("/{path:path}", response_class=HTMLResponse)
async def serve_page():
    return HTMLResponse(_PAGE)


# ── Run ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("meld:app", host="0.0.0.0", port=8080, reload=False)
