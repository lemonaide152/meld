"""meld base-case server.

In-memory capability URLs. Two parties talk on the same bridge. Create
leaves it dormant (expires_at None). The bridge stays open while the
context exchange is active. The pilot timer is 24h from the last reply.
The first reply starts it. Each later reply is kept and resets 24h.
There is no maximum lifetime. It closes only after 24h with no new reply.
A body read
does not start or reset the timer. A link-preview crawl does not read the
body. While the host still remembers a dissolved code, it serves 410. A
code that never existed, or one forgotten after the tombstone cap or a
restart, is 404.

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

TTL_KEY = "24h"
TTL_SECONDS = 24 * 60 * 60
CODE_LEN = 12
MAX_CONTEXT = 100_000
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
    """Each reply starts or resets the pilot 24h silence timer.

    The bridge stays open while the exchange is active. There is no
    maximum lifetime. 24h with no new reply closes it.
    A body read does not call this.
    """
    meld["expires_at"] = now + timedelta(seconds=TTL_SECONDS)


def _require_context(context) -> str:
    if not isinstance(context, str):
        raise HTTPException(400, "Context must be a string")
    if not context.strip():
        raise HTTPException(400, "Context must be non-empty")
    if len(context) > MAX_CONTEXT:
        raise HTTPException(400, "Context too large (100K max)")
    return context


def _require_ttl(value) -> tuple[str, int]:
    """Pilot TTL is 24h from the last reply. Omit ttl or send 24h. Any other value is rejected."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return TTL_KEY, TTL_SECONDS
    if isinstance(value, str) and value.strip().lower() == TTL_KEY:
        return TTL_KEY, TTL_SECONDS
    raise HTTPException(
        400,
        'Pilot TTL is 24h from the last reply. Send ttl "24h" or omit it. There is no other lifetime.',
    )


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
        "Pilot TTL is 24h from the last reply. The first reply starts it. Each later reply resets 24h. "
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
        raise HTTPException(400, "The conversation stays on this bridge. There is no next link.")
    context = _require_context(body.get("context", ""))
    ttl_key, _ttl_seconds = _require_ttl(body.get("ttl"))
    now = _now()
    base = _base(request)
    with _lock:
        code = _alloc_code()
        _melds[code] = {
            "code": code,
            "context_a": context,
            "replies": [],
            "resolved": False,
            "resolved_at": None,
            "created_at": now,
            "expires_at": None,
        }
        _purge(now)
        payload = {
            "code": code,
            "url": _url(base, code),
            "context_a": context,
            "replies": [],
            "resolved": False,
            "ttl": ttl_key,
            "expires_at": None,
        }
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
    """Append a reply. The first reply starts the pilot 24h. Each later reply resets 24h."""
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
        payload = {
            "code": code,
            "context_a": meld["context_a"],
            "replies": list(meld["replies"]),
            "resolved": True,
            "expires_at": meld["expires_at"].isoformat(),
        }
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
