"""meld reference application. The self-host server and the hosted Worker both run this.

Behavior is SPEC.md. Storage is pluggable: meld_store.MemoryStore, or the Worker's Durable Object store; both memory only.
Pilot instrumentation, if any, attaches through `hooks` and cannot change a
response: hook errors are swallowed and hooks receive no request body.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

import meld_docs
import meld_ui
from meld_store import StoreFull
from meld_spec import (
    CAPACITY_BODY, CAPACITY_STATUS, CREATE_FIELDS, LEGACY_FIELDS, MAX_CHARS, MAX_CODE_LEN, NOT_FOUND_BODY, NOT_FOUND_STATUS,
    REJECTED_FIELDS, REPLY_FIELD, SWEEP_MINUTES, VERSION, new_code, open_expiry, ts, utcnow,
)

# Four 100,000-character strings with \\uXXXX escapes, plus an envelope.
MAX_BODY_BYTES = MAX_CHARS * 6 * 4 + 8192
MCP_MAX_BATCH = 10
MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

LINK_PREVIEW_BOTS = (
    "twitterbot", "slackbot", "slack-imgproxy", "discordbot", "facebookexternalhit", "facebot",
    "linkedinbot", "whatsapp", "telegrambot", "embedly", "iframely", "redditbot", "pinterest",
    "vkshare", "quora link preview", "skypeuripreview", "applebot", "mastodon", "google-pagerenderer",
)
AGENT_UA_MARKERS = ("curl/", "python-requests", "httpx", "go-http-client", "axios", "node-fetch",
                    "undici", "python-urllib", "aiohttp", "wget")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Accept, X-Meld-Surface, Mcp-Session-Id, Mcp-Protocol-Version",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Max-Age": "86400",
}


class MeldError(Exception):
    def __init__(self, status: int, detail: str, reason: str = "") -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.reason = reason


def _bad(reason: str, detail: str) -> MeldError:
    return MeldError(400, detail, reason)


def _missing() -> MeldError:
    return MeldError(NOT_FOUND_STATUS, NOT_FOUND_BODY["detail"], "not_found")


def _full() -> MeldError:
    return MeldError(CAPACITY_STATUS, CAPACITY_BODY["detail"], "capacity")


def _error_response(err: MeldError) -> JSONResponse:
    if err.status == CAPACITY_STATUS:
        return JSONResponse(dict(CAPACITY_BODY), status_code=CAPACITY_STATUS, headers={"Retry-After": "60"})
    if err.status == NOT_FOUND_STATUS:
        return JSONResponse(dict(NOT_FOUND_BODY), status_code=NOT_FOUND_STATUS)
    return JSONResponse({"detail": err.detail}, status_code=err.status)


# ── validation (shared by HTTP and MCP) ──────────────────────────────────

def parse_json_object(raw: bytes) -> dict:
    if len(raw) > MAX_BODY_BYTES:
        raise _bad("body_too_large", "Body too large")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        raise _bad("body_invalid", "Body must be a JSON object")
    if not isinstance(body, dict):
        raise _bad("body_invalid", "Body must be a JSON object")
    return body


def _text(value: Any, field: str, reason_prefix: str) -> str:
    if not isinstance(value, str):
        raise _bad(f"{reason_prefix}_invalid", f"{field} must be a string")
    if not value.strip():
        raise _bad(f"{reason_prefix}_missing", f"{field} must be non-empty")
    if len(value) > MAX_CHARS:
        raise _bad(f"{reason_prefix}_too_long", f"{field} too large ({MAX_CHARS:,} characters max)")
    return value


def validate_create(body: dict) -> str:
    """The one note from a create body. Raises a 400 MeldError otherwise."""
    for field in REJECTED_FIELDS:
        if field in body:
            raise _bad(f"{field}_rejected",
                       f"{field} is not accepted. A bridge is one note on a fixed clock with no other options.")
    supplied = {k: body[k] for k in CREATE_FIELDS + LEGACY_FIELDS if body.get(k) is not None}
    for key, value in supplied.items():
        if not isinstance(value, str):
            raise _bad(f"{key}_invalid", f"{key} must be a string")
    if not any(k in supplied for k in CREATE_FIELDS):
        raise _bad("context_missing", "Send one note as a string in note or context")
    if len(set(supplied.values())) > 1:
        raise _bad("context_mismatch",
                   "note, context, for, and not_for must be identical when more than one is sent")
    return _text(next(iter(supplied.values())), "note", "context")


def validate_reply(body: dict) -> str:
    if body.get(REPLY_FIELD) is None:
        raise _bad("context_missing", "Send the reply as a string in context")
    return _text(body[REPLY_FIELD], "context", "context")


def _code_ok(code: Any) -> bool:
    return isinstance(code, str) and 0 < len(code) <= MAX_CODE_LEN


# ── request helpers ──────────────────────────────────────────────────────

def _is_preview_bot(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    return any(bot in ua for bot in LINK_PREVIEW_BOTS)


def _wants_html(request: Request) -> bool:
    """HTML only for a browser that asks for it. Everything else gets JSON."""
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" not in accept:
        return False
    if (request.headers.get("sec-fetch-dest") or "").lower() == "document":
        return True
    ua = (request.headers.get("user-agent") or "").lower()
    if not ua or any(m in ua for m in AGENT_UA_MARKERS):
        return False
    return "mozilla/" in ua


def _surface(request: Request) -> str:
    """ui when the web page says so, else api. MCP is set by the server."""
    return "ui" if (request.headers.get("x-meld-surface") or "").strip().lower() == "ui" else "api"


def _html(body: str, nonce: str, status: int = 200, head: bool = False) -> HTMLResponse:
    encoded = body.encode("utf-8")
    return HTMLResponse(
        content=b"" if head else encoded, status_code=status,
        headers={
            "Content-Length": str(len(encoded)),
            "Content-Security-Policy": (
                f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"),
            "Cache-Control": "no-store",
        },
    )


class _Headers:
    """Pure ASGI: security headers, CORS, and OPTIONS preflight. Leaves bodies alone."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            resp = Response(status_code=204, headers=CORS)
            await resp(scope, receive, self._wrap(send))
            return
        await self.app(scope, receive, self._wrap(send))

    @staticmethod
    def _wrap(send):
        async def wrapped(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                names = {k.lower() for k, _ in headers}
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"access-control-allow-origin": b"*",
                }
                if b"content-security-policy" not in names:
                    extra[b"content-security-policy"] = b"default-src 'none'; frame-ancestors 'none'"
                for k, v in extra.items():
                    if k not in names:
                        headers.append((k, v))
                message = dict(message, headers=headers)
            await send(message)
        return wrapped


# ── the app ──────────────────────────────────────────────────────────────

class Meld:
    """Create, read, reply, sweep. HTTP routes and MCP tools both call these."""

    def __init__(self, store, hooks=None) -> None:
        self.store = store
        self.hooks = hooks

    async def emit(self, event: str, **fields) -> None:
        if self.hooks is None:
            return
        try:
            await self.hooks.event(event, **fields)
        except Exception:
            pass  # instrumentation never changes a response

    async def create(self, body: dict, base: str, path: str) -> dict:
        try:
            note = validate_create(body)
        except MeldError as err:
            await self.emit("create_400", reason=err.reason, path=path)
            raise
        now = utcnow()
        created_at, expires_at = ts(now), open_expiry(now)
        for _ in range(4):
            code = new_code()
            try:
                made = await self.store.create(code, note, created_at, expires_at)
            except StoreFull:
                await self.emit("capacity", path=path)
                raise _full()
            if made:
                await self.emit("created", path=path)
                return {"code": code, "url": f"{base}/m/{code}", "expires_at": expires_at}
        raise MeldError(500, "Could not allocate a code", "alloc")

    async def read(self, code: str, base: str) -> dict:
        if not _code_ok(code):
            raise _missing()
        meld = await self.store.get(code, ts(utcnow()))
        if meld is None:
            raise _missing()
        return public(meld, base)

    async def reply(self, code: str, body: dict, base: str, path: str) -> dict:
        content = validate_reply(body)  # 400 before any lookup
        if not _code_ok(code):
            raise _missing()
        try:
            meld = await self.store.reply(code, content, utcnow())
        except StoreFull:
            await self.emit("capacity", path=path)
            raise _full()
        if meld is None:
            raise _missing()
        await self.emit("replied", path=path)
        return public(meld, base)

    async def sweep(self) -> int:
        return await self.store.sweep(ts(utcnow()))


def public(meld: dict, base: str) -> dict:
    return {
        "code": meld["code"],
        "url": f"{base}/m/{meld['code']}",
        "note": meld["note"],
        "created_at": meld["created_at"],
        "expires_at": meld["expires_at"],
        "reply_count": int(meld["reply_count"]),
        "replies": [{"content": r["content"], "created_at": r["created_at"]} for r in meld["replies"]],
    }


def untrusted(meld: dict) -> list:
    return [meld["note"]] + [r["content"] for r in meld["replies"]]


def build_app(store, *, public_url: Optional[str] = None, hooks=None, assets: Optional[dict] = None,
              background_sweep: bool = False, origin_from=None, notices_from=None) -> Any:
    """The meld ASGI app.

    public_url    fixed origin for links (else the request's own origin)
    hooks         object with `async event(name, **fields)`; pilot instrumentation only
    assets        {path: (bytes, media_type)} static files such as /og.png
    background_sweep  run the sweep every SWEEP_MINUTES in-process (self-host)
    origin_from   callable(request) -> origin, for hosts that read it from env
    notices_from  callable(request) -> tuple of extra trust lines a deployment must show
                  (trust.md and the page), for hosts that read them from env
    """
    assets = dict(assets or {})
    core = Meld(store, hooks)
    app = FastAPI(title="meld", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.meld = core

    if background_sweep:
        @app.on_event("startup")
        async def _start_sweep():
            async def loop():
                while True:
                    await asyncio.sleep(SWEEP_MINUTES * 60)
                    try:
                        await core.sweep()
                    except Exception:
                        pass
            app.state.sweeper = asyncio.create_task(loop())

    def base(request: Request) -> str:
        if public_url:
            return public_url.rstrip("/")
        if origin_from is not None:
            got = origin_from(request)
            if got:
                return got.rstrip("/")
        return str(request.base_url).rstrip("/")

    def notices(request: Request) -> tuple:
        if notices_from is None:
            return ()
        try:
            return tuple(n for n in notices_from(request) if n)
        except Exception:
            return ()

    async def body_of(request: Request) -> dict:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise _bad("body_too_large", "Body too large")
        return parse_json_object(await request.body())

    # API
    @app.post("/api/melds")
    async def create(request: Request):
        path = _surface(request)
        try:
            body = await body_of(request)
        except MeldError as err:
            await core.emit("create_400", reason=err.reason, path=path)
            return _error_response(err)
        try:
            return JSONResponse(await core.create(body, base(request), path))
        except MeldError as err:
            return _error_response(err)

    @app.get("/api/melds/{code}")
    async def read(code: str, request: Request):
        try:
            return JSONResponse(await core.read(code, base(request)))
        except MeldError as err:
            return _error_response(err)

    @app.post("/api/melds/{code}/resolve")
    async def resolve(code: str, request: Request):
        try:
            body = await body_of(request)
            return JSONResponse(await core.reply(code, body, base(request), _surface(request)))
        except MeldError as err:
            return _error_response(err)

    @app.get("/m/{code}")
    async def capability(code: str, request: Request):
        if _is_preview_bot(request):
            og = f"{base(request)}/og.png" if "/og.png" in assets else None
            # Not a read: the meld is never looked up.
            return HTMLResponse(meld_ui.preview_card(og), headers={"Cache-Control": "no-store"})
        nonce = secrets.token_hex(16)
        try:
            meld = await core.read(code, base(request))
        except MeldError as err:
            if _wants_html(request):
                return _html(meld_ui.not_found(nonce), nonce, status=NOT_FOUND_STATUS)
            return _error_response(err)
        if _wants_html(request):
            return _html(meld_ui.bridge(meld, nonce, notices(request)), nonce)
        return JSONResponse(meld)

    @app.get("/health")
    async def health():
        return {"ok": True}

    # Web UI
    @app.api_route("/", methods=["GET", "HEAD"])
    async def home(request: Request):
        nonce = secrets.token_hex(16)
        head_extra = ""
        if "/og.png" in assets:
            og = f"{base(request)}/og.png"
            head_extra = (f'<meta property="og:title" content="meld">'
                          f'<meta property="og:description" content="{meld_ui.RULE_SHORT} Not for secrets.">'
                          f'<meta property="og:image" content="{og}"><meta name="twitter:card" content="summary_large_image">')
        return _html(meld_ui.home(nonce, head_extra, notices(request)), nonce, head=request.method == "HEAD")

    # Docs, all generated from meld_spec
    def doc(name: str, media: str):
        async def handler(request: Request):
            gen = meld_docs.GENERATED[name]
            text = meld_docs.render(gen(notices(request)) if name == "TRUST.md" else gen(), base(request))
            return Response(text, media_type=media, headers={"Cache-Control": "public, max-age=300"})
        return handler

    app.add_api_route("/llms.txt", doc("llms.txt", "text/plain; charset=utf-8"), methods=["GET"])
    app.add_api_route("/agents.md", doc("agents.md", "text/markdown; charset=utf-8"), methods=["GET"])
    app.add_api_route("/skill.md", doc("skill.md", "text/markdown; charset=utf-8"), methods=["GET"])
    app.add_api_route("/trust.md", doc("TRUST.md", "text/markdown; charset=utf-8"), methods=["GET"])
    app.add_api_route("/openapi.json", doc("openapi.json", "application/json"), methods=["GET"])
    app.add_api_route("/.well-known/mcp.json", doc("mcp.json", "application/json"), methods=["GET"])
    app.add_api_route("/.well-known/mcp/server-card.json", doc("mcp.json", "application/json"), methods=["GET"])
    app.add_api_route("/.well-known/agent.json", doc("agent.json", "application/json"), methods=["GET"])

    @app.get("/robots.txt")
    async def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /m/\nDisallow: /api/\n")

    for asset_path, (data, media) in assets.items():
        def make(data=data, media=media):
            async def serve(request: Request):
                return Response(b"" if request.method == "HEAD" else data, media_type=media,
                                headers={"Content-Length": str(len(data)), "Cache-Control": "public, max-age=86400"})
            return serve
        app.add_api_route(asset_path, make(), methods=["GET", "HEAD"])

    # MCP: streamable HTTP, JSON response mode
    async def call_tool(name: str, args: dict, request: Request) -> dict:
        b = base(request)
        await core.emit("mcp_call", tool=name if name in meld_docs_tool_names else "unknown")
        if name == "meld_create":
            return await core.create(args, b, "mcp")
        if name == "meld_resolve":
            body = {k: v for k, v in args.items() if k != "code"}
            meld = await core.reply(args.get("code"), body, b, "mcp")
            return dict(meld, untrusted_content=untrusted(meld))
        if name == "meld_read":
            meld = await core.read(args.get("code"), b)
            return dict(meld, untrusted_content=untrusted(meld))
        raise MeldError(400, f"Unknown tool: {name}", "unknown_tool")

    async def handle(msg: dict, request: Request):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        mid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method is None or str(method).startswith("notifications/"):
            return None
        if method == "initialize":
            want = params.get("protocolVersion") or ""
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": want if want in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "meld", "version": VERSION},
                "instructions": f"{meld_docs.TRUST_LINE} {meld_docs.RULE} {meld_docs.UNTRUSTED_LINE}",
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": meld_docs.MCP_TOOL_LIST}}
        if method == "tools/call":
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            try:
                result = await call_tool(params.get("name"), args, request)
                text = json.dumps(result, indent=2, ensure_ascii=False)
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text}], "structuredContent": result}}
            except MeldError as err:
                detail = NOT_FOUND_BODY["detail"] if err.status == NOT_FOUND_STATUS else err.detail
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"Error {err.status}: {detail}"}], "isError": True}}
        if mid is not None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
        return None

    async def mcp(request: Request):
        # Size and batch limits come before parsing tools, so before any lookup.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Body too large"}},
                                status_code=413)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Body too large"}},
                                status_code=413)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                                status_code=400)
        batch = payload if isinstance(payload, list) else [payload]
        if len(batch) > MCP_MAX_BATCH:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": f"Batch too large (max {MCP_MAX_BATCH})"}},
                                status_code=400)
        if not batch:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                                status_code=400)
        out = [r for r in [await handle(m, request) for m in batch] if r is not None]
        if not out:
            return Response(status_code=202)
        return JSONResponse(out if isinstance(payload, list) else out[0])

    app.add_api_route("/mcp", mcp, methods=["POST"])
    app.add_api_route("/", mcp, methods=["POST"])  # MCP registry remotes that use the root URL

    @app.get("/mcp")
    async def mcp_get():
        return Response(status_code=405, headers={"Allow": "POST, OPTIONS"})

    wrapped = _Headers(app)
    wrapped.meld = core
    wrapped.fastapi = app
    return wrapped


meld_docs_tool_names = {t["name"] for t in meld_docs.MCP_TOOL_LIST}
