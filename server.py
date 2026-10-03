"""meld base-case server.

In-memory capability URLs. Party A creates a link with a declaration of
what the bridge is for and what it is not for, and sends that URL to B
privately. Two parties talk on the same bridge. The bridge stays open
while the context exchange is active. Until B's first reply it stays
open 36 hours from create. That first reply sets a 24 hour timer. Each
later reply is kept and resets that 24 hours. There is no maximum
lifetime after replies start. A body read does not start or reset the
timer. A link-preview crawl does not read the body. While the host still
remembers a dissolved code, it serves 410. A code that never existed, or
one forgotten after the tombstone cap or a restart, is 404.

The host can read a live meld. Anyone with the link can read it.
Not for secrets. No accounts. The host does not invent a reply.
Dissolved plaintext is deleted.
A tombstone keeps the code only, so a later request can still be 410.
"""

from __future__ import annotations

import logging
import os
import secrets
import string
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("meld")

app = FastAPI(title="meld", version="1.0.0", docs_url=None, redoc_url=None)

# Until B's first reply: 36 hours from create. After that, 24 hours from each reply.
OPEN_SECONDS = 36 * 60 * 60
TTL_SECONDS = 24 * 60 * 60
CODE_LEN = 12
MAX_CONTEXT = 100_000
MAX_DECLARATION = 2_000
# Dissolved codes only. Values stay None: the code, never the plaintext.
# Oldest dropped when full. A dropped code, or one forgotten on restart,
# is indistinguishable from a code that never existed, so the answer is 404.
TOMBSTONE_CAP = 4096

_melds: dict[str, dict] = {}
_tombstones: OrderedDict[str, None] = OrderedDict()
_lock = threading.Lock()


def _wall_now() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> datetime:
    return _wall_now()


def _clock_started(expires_at) -> bool:
    return expires_at is not None


def _reset_ttl(meld: dict, now: datetime) -> None:
    """A reply sets or resets the 24 hour timer. A body read does not call this."""
    meld["expires_at"] = now + timedelta(seconds=TTL_SECONDS)


def _require_context(context) -> str:
    if not isinstance(context, str):
        raise HTTPException(400, "Context must be a string")
    if not context.strip():
        raise HTTPException(400, "Context must be non-empty")
    if len(context) > MAX_CONTEXT:
        raise HTTPException(400, "Context too large (100K max)")
    return context


def _reject_ttl_picker(value) -> None:
    """The two windows are fixed. A client cannot pick another lifetime."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return
    raise HTTPException(
        400,
        "Until the first reply the bridge stays open 36 hours from create. "
        "The first reply sets a 24 hour timer. Each later reply resets that 24 hours. "
        "There is no other lifetime.",
    )


def _require_declaration(value, label: str) -> str:
    if not isinstance(value, str):
        raise HTTPException(400, f"{label} must be a string")
    if not value.strip():
        raise HTTPException(400, f"{label} must be non-empty")
    if len(value) > MAX_DECLARATION:
        raise HTTPException(400, f"{label} is too long")
    return value


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")
    return body


def _base(request: Request) -> str:
    configured = os.getenv("MELD_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _alloc_code() -> str:
    alphabet = string.ascii_lowercase + string.digits
    for _ in range(8):
        code = "".join(secrets.choice(alphabet) for _ in range(CODE_LEN))
        if code not in _melds and code not in _tombstones:
            return code
    raise HTTPException(500, "Could not allocate a code")


def _dissolve(code: str) -> None:
    """Delete the meld and remember the code only."""
    _melds.pop(code, None)
    _tombstones[code] = None
    _tombstones.move_to_end(code)
    while len(_tombstones) > TOMBSTONE_CAP:
        _tombstones.popitem(last=False)
    _log.info("dissolved code=%s", code)


def _purge(now: datetime) -> None:
    dead = [
        code
        for code, meld in _melds.items()
        if _clock_started(meld["expires_at"]) and meld["expires_at"] <= now
    ]
    for code in dead:
        _dissolve(code)


def _get_live(code: str, now: datetime):
    """Return (meld, 'live'|'missing'|'expired').

    An expired row is deleted and the code is tombstoned. A later request
    for that code is expired (410). A code we have never stored is missing (404).
    """
    meld = _melds.get(code)
    if meld is None:
        if code in _tombstones:
            return None, "expired"
        return None, "missing"
    if _clock_started(meld["expires_at"]) and meld["expires_at"] <= now:
        _dissolve(code)
        return None, "expired"
    return meld, "live"


def _require_live(code: str, now: datetime, *, missing: str, expired: str) -> dict:
    meld, state = _get_live(code, now)
    if state == "missing":
        raise HTTPException(404, missing)
    if state == "expired":
        raise HTTPException(410, expired)
    return meld


def _remaining(meld: dict, now: datetime) -> int | None:
    if not _clock_started(meld["expires_at"]):
        return None
    return max(0, int((meld["expires_at"] - now).total_seconds()))


def _read_payload(meld: dict, now: datetime) -> dict:
    exp = meld["expires_at"]
    return {
        "code": meld["code"],
        "resolved": meld["resolved"],
        "resolved_at": meld["resolved_at"],
        "expires_at": exp.isoformat() if exp is not None else None,
        "seconds_remaining": _remaining(meld, now),
        "context_a": meld["context_a"],
        "for": meld["for"],
        "not_for": meld["not_for"],
        "replies": list(meld["replies"]),
    }


def _url(base: str, code: str) -> str:
    return f"{base}/m/{code}"


# Link-preview crawlers. A match on /m/{code} gets an expires-only card and
# does not read the meld. A normal GET returns the plaintext and does not
# start or reset the silence timer.
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

_PREVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>meld — this bridge expires</title>
<meta name="description" content="This link expires. The exchange is not included in this preview.">
<meta name="robots" content="noindex, nofollow">
<meta property="og:title" content="meld — this bridge expires">
<meta property="og:description" content="This link expires. The exchange is not included in this preview.">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="meld — this bridge expires">
<meta name="twitter:description" content="This link expires. The exchange is not included in this preview.">
</head>
<body>
<p>meld — this bridge expires</p>
<p>This link expires. The exchange is not included in this preview.</p>
</body>
</html>
"""


def _is_link_preview_bot(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    return any(bot in ua for bot in _LINK_PREVIEW_BOTS)


@app.get("/", response_class=PlainTextResponse)
async def root():
    return (
        "meld base-case server. POST /api/melds with {context}. "
        "The conversation stays on that link. "
        "The bridge stays open while the context exchange is active. "
        "Until the first reply, it stays open 36 hours from create. "
        "The first reply sets a 24 hour timer. Each later reply resets that 24 hours. "
        "A read does not start or reset the timer. "
        "Host-readable while live. Anyone with the link can read it. Not for secrets. "
        "No AI in the loop.\n"
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/melds")
async def create_meld(request: Request):
    body = await _body(request)
    if body.get("prev_code") not in (None, ""):
        raise HTTPException(400, "The conversation stays on this bridge.")
    _reject_ttl_picker(body.get("ttl"))
    context = _require_context(body.get("context", ""))
    purpose_for = _require_declaration(body.get("for", ""), "for")
    purpose_not = _require_declaration(body.get("not_for", ""), "not_for")
    now = _now()
    base = _base(request)
    expires_at = now + timedelta(seconds=OPEN_SECONDS)
    with _lock:
        code = _alloc_code()
        _melds[code] = {
            "code": code,
            "context_a": context,
            "for": purpose_for,
            "not_for": purpose_not,
            "replies": [],
            "resolved": False,
            "resolved_at": None,
            "created_at": now,
            "expires_at": expires_at,
        }
        _purge(now)
        payload = _read_payload(_melds[code], now)
        payload["url"] = _url(base, code)
    _log.info("created code=%s", code)
    return payload


@app.get("/api/melds/{code}")
@app.get("/m/{code}")
async def get_meld(code: str, request: Request):
    """Plaintext read of a live meld. Does not start or reset the timer.

    Link-preview crawlers that GET /m/{code} receive an expires-only card.
    That response does not read the meld.
    """
    if request.url.path.startswith("/m/") and _is_link_preview_bot(request):
        return HTMLResponse(_PREVIEW_HTML)
    now = _now()
    with _lock:
        meld = _require_live(
            code,
            now,
            missing="Meld not found",
            expired="This meld has expired",
        )
        return _read_payload(meld, now)


@app.post("/api/melds/{code}/resolve")
async def resolve_meld(code: str, request: Request):
    """Append a reply. The first reply sets 24 hours. Each later reply resets that 24 hours."""
    body = await _body(request)
    context = _require_context(body.get("context", ""))
    now = _now()
    with _lock:
        meld = _require_live(
            code,
            now,
            missing="Meld not found",
            expired="This meld has expired",
        )
        meld["replies"].append(context)
        meld["resolved"] = True
        meld["resolved_at"] = now.isoformat()
        _reset_ttl(meld, now)
        payload = _read_payload(meld, now)
    _log.info("resolved code=%s", code)
    return payload


def main() -> None:
    import uvicorn

    host = os.getenv("MELD_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    # Trust X-Forwarded-* only from FORWARDED_ALLOW_IPS. Compose sets "*"
    # because Caddy is the only client that can reach this port.
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
