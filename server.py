"""meld base-case server.

In-memory capability URLs. Each link lives 1 hour, then the host serves 410.
Mint-next (prev_code) creates a new bearer with its own hour. That is not an extend.

The host can read a live meld. Anyone with the link can read it.
Not for secrets. No accounts. Dissolved links are deleted and not archived.
"""

from __future__ import annotations

import logging
import os
import secrets
import string
import threading
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("meld")

app = FastAPI(title="meld", version="1.0.0", docs_url=None, redoc_url=None)

TTL_KEY = "1hr"
HOUR_SECONDS = 60 * 60
CODE_LEN = 12
MAX_CONTEXT = 100_000

_melds: dict[str, dict] = {}
_lock = threading.Lock()


def _wall_now() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> datetime:
    return _wall_now()


def _require_context(context) -> str:
    if not isinstance(context, str):
        raise HTTPException(400, "Context must be a string")
    if not context.strip():
        raise HTTPException(400, "Context must be non-empty")
    if len(context) > MAX_CONTEXT:
        raise HTTPException(400, "Context too large (100K max)")
    return context


def _require_ttl(value) -> tuple[str, int]:
    """Every meld lives 1 hour. Omit ttl or send 1hr. Any other value is rejected."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return TTL_KEY, HOUR_SECONDS
    if isinstance(value, str) and value.strip().lower() == TTL_KEY:
        return TTL_KEY, HOUR_SECONDS
    raise HTTPException(
        400,
        'This link lives 1 hour. Send ttl "1hr" or omit it. There is no other lifetime.',
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
        if code not in _melds:
            return code
    raise HTTPException(500, "Could not allocate a code")


def _purge(now: datetime) -> None:
    dead = [code for code, meld in _melds.items() if meld["expires_at"] <= now]
    for code in dead:
        _melds.pop(code, None)
        _log.info("dissolved code=%s", code)


def _get_live(code: str, now: datetime):
    """Return (meld, 'live'|'missing'|'expired'). Expired rows are deleted."""
    meld = _melds.get(code)
    if meld is None:
        return None, "missing"
    if meld["expires_at"] <= now:
        _melds.pop(code, None)
        _log.info("dissolved code=%s", code)
        return None, "expired"
    return meld, "live"


def _require_live(code: str, now: datetime, *, missing: str, expired: str) -> dict:
    meld, state = _get_live(code, now)
    if state == "missing":
        raise HTTPException(404, missing)
    if state == "expired":
        raise HTTPException(410, expired)
    return meld


def _hop_parent(prev, now: datetime) -> tuple[str | None, str | None]:
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
    _require_live(
        prev,
        now,
        missing="Previous meld not found",
        expired="Previous meld has expired",
    )
    parent = _melds[prev]
    return prev, parent["thread_id"]


def _remaining(meld: dict, now: datetime) -> int:
    return max(0, int((meld["expires_at"] - now).total_seconds()))


def _read_payload(meld: dict, now: datetime) -> dict:
    return {
        "code": meld["code"],
        "resolved": meld["resolved"],
        "resolved_at": meld["resolved_at"],
        "expires_at": meld["expires_at"].isoformat(),
        "seconds_remaining": _remaining(meld, now),
        "context_a": meld["context_a"],
        "context_b": meld["context_b"],
    }


def _url(base: str, code: str) -> str:
    return f"{base}/m/{code}"


@app.get("/", response_class=PlainTextResponse)
async def root():
    return (
        "meld base-case server. POST /api/melds with {context}. "
        "Each link lives 1 hour, then it dissolves. "
        "Host-readable while live. Anyone with the link can read it. Not for secrets.\n"
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/melds")
async def create_meld(request: Request):
    body = await _body(request)
    context = _require_context(body.get("context", ""))
    ttl_key, ttl_seconds = _require_ttl(body.get("ttl"))
    now = _now()
    base = _base(request)
    with _lock:
        prev_code, thread_id = _hop_parent(body.get("prev_code"), now)
        code = _alloc_code()
        if thread_id is None:
            thread_id = code
        expiry = now + timedelta(seconds=ttl_seconds)
        _melds[code] = {
            "code": code,
            "context_a": context,
            "context_b": None,
            "resolved": False,
            "resolved_at": None,
            "created_at": now,
            "expires_at": expiry,
            "prev_code": prev_code,
            "thread_id": thread_id,
        }
        _purge(now)
        payload = {
            "code": code,
            "url": _url(base, code),
            "context_a": context,
            "resolved": False,
            "ttl": ttl_key,
            "expires_at": expiry.isoformat(),
            "prev_code": prev_code,
            "thread_id": thread_id,
        }
    _log.info("created code=%s", code)
    return payload


@app.get("/api/melds/{code}")
@app.get("/m/{code}")
async def get_meld(code: str):
    now = _now()
    with _lock:
        meld = _require_live(
            code,
            now,
            missing="Meld not found",
            expired="This meld has expired",
        )
        return _read_payload(meld, now)


@app.get("/api/melds/{code}/chain")
async def get_chain(code: str, request: Request):
    """Live hops that share this link's thread. Expired plaintext is not returned."""
    now = _now()
    base = _base(request)
    with _lock:
        meld = _require_live(
            code,
            now,
            missing="Meld not found",
            expired="This meld has expired",
        )
        thread_id = meld["thread_id"]
        expired = [
            item["code"]
            for item in _melds.values()
            if item["thread_id"] == thread_id and item["expires_at"] <= now
        ]
        for item_code in expired:
            _melds.pop(item_code, None)
            _log.info("dissolved code=%s", item_code)
        nodes = [
            item
            for item in _melds.values()
            if item["thread_id"] == thread_id and item["expires_at"] > now
        ]
        nodes.sort(key=lambda item: item["created_at"])
        return {
            "thread_id": thread_id,
            "nodes": [
                {
                    "code": item["code"],
                    "url": _url(base, item["code"]),
                    "context_a": item["context_a"],
                    "context_b": item["context_b"],
                    "resolved": item["resolved"],
                    "expires_at": item["expires_at"].isoformat(),
                    "seconds_remaining": _remaining(item, now),
                    "ttl": TTL_KEY,
                    "prev_code": item["prev_code"],
                }
                for item in nodes
            ],
        }


@app.post("/api/melds/{code}/resolve")
async def resolve_meld(code: str, request: Request):
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
        if meld["resolved"]:
            if meld["context_b"] == context:
                return {
                    "code": code,
                    "context_a": meld["context_a"],
                    "context_b": meld["context_b"],
                    "resolved": True,
                    "retry": True,
                }
            raise HTTPException(409, "Already resolved with a different answer")
        meld["context_b"] = context
        meld["resolved"] = True
        meld["resolved_at"] = now.isoformat()
        # Resolve does not move expires_at. The hour chosen at create stands.
        payload = {
            "code": code,
            "context_a": meld["context_a"],
            "context_b": context,
            "resolved": True,
            "expires_at": meld["expires_at"].isoformat(),
        }
    _log.info("resolved code=%s", code)
    return payload


def main() -> None:
    import uvicorn

    host = os.getenv("MELD_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
