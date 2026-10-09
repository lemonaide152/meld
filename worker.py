"""Hosted entry for Cloudflare Python Workers. Memory-only, as SPEC.md requires.

Same meld_app as the self-host server. Bridges live in MemoryStores inside
Durable Objects (BridgeShard), in Python memory only. Nothing here uses a database,
KV, or the Durable Object storage API. A Durable Object restart, eviction, or
deploy drops the links it held; they then return the uniform 404, the same as
a self-host restart.

- Routing: a code picks one of SHARDS objects by SHA-256. Changing SHARDS
  remaps codes, which drops live links the same way a restart does.
- Residency: while a shard holds a live bridge it keeps one pending in-memory
  timer (setTimeout). A pending timer keeps a Durable Object in memory, and
  each tick runs the sweep on that shard. An empty shard lets its timer lapse.
- Sweep: every tick (KEEPALIVE_SECONDS), plus the 5-minute cron, plus a lazy
  check on each read and reply.
- Memory guard: each shard caps its live bridges and their text bytes
  (SHARD_MAX_BRIDGES, SHARD_MAX_BYTES). At the cap, create and reply get the
  one 503 body and nothing is stored, instead of the isolate running out of
  memory and dropping every link it holds.
- Pilot counters and a deployment's extension state live in one MeldMeta
  object, in memory only. A restart resets them.
- Logging: nothing here prints or logs. Bridge text never leaves the
  objects except in the HTTP response to the link holder.
"""
from __future__ import annotations

import hashlib
import json

from meld_app import build_app
from meld_spec import parse_ts, ts, utcnow
from meld_store import MemoryStore, StoreFull
from pilot import FunnelHooks, MemoryCounters

try:
    from workers import DurableObject
except ImportError:  # not on Workers (tests import this module directly)
    class DurableObject:  # type: ignore[no-redef]
        def __init__(self, ctx=None, env=None) -> None:
            self.ctx, self.env = ctx, env

SHARDS = 4
KEEPALIVE_SECONDS = 60
SHARD_MAX_BRIDGES = 1000
SHARD_MAX_BYTES = 8 * 1024 * 1024
BRIDGES_BINDING = "BRIDGES"
META_BINDING = "META"

class _EnvHolder:
    """The Worker env. It is the same object for every request in an isolate,
    so a plain holder is used instead of a contextvar: under Pyodide stack
    switching, contextvars can leak between concurrent tasks."""

    def __init__(self):
        self.value = None

    def get(self):
        return self.value

    def set(self, env):
        if env is not None:
            self.value = env


_env = _EnvHolder()


def _set_timer(callback, seconds: float) -> bool:
    """One in-memory JS timer. Returns False where there is no JS runtime."""
    try:
        from js import setTimeout
        from pyodide.ffi import create_once_callable
    except ImportError:
        return False
    setTimeout(create_once_callable(callback), int(seconds * 1000))
    return True


class _Resident:
    """Keep the object in memory while it holds state, using a pending timer only."""

    timer = staticmethod(_set_timer)  # tests swap this

    def _arm(self) -> None:
        if getattr(self, "_armed", False):
            return
        self._armed = bool(self.timer(self._tick, KEEPALIVE_SECONDS))

    def _tick(self, *_args) -> None:
        self._armed = False
        try:
            self.on_tick()
        except Exception:
            pass
        if self.holds_state():
            self._arm()


class BridgeShard(_Resident, DurableObject):
    """Live bridges for the codes that hash to this shard. Memory only."""

    def __init__(self, ctx=None, env=None) -> None:
        super().__init__(ctx, env)
        self.store = MemoryStore(max_bridges=SHARD_MAX_BRIDGES, max_bytes=SHARD_MAX_BYTES)
        self._armed = False

    async def op(self, payload: str) -> str:
        req = json.loads(payload)
        kind, s = req.get("op"), self.store
        try:
            out = await self._op(kind, req, s)
        except StoreFull:
            out = {"full": True}
        if s.size():
            self._arm()
        return json.dumps(out)

    async def _op(self, kind, req, s):
        if kind == "create":
            out = await s.create(req["code"], req["note"], req["created_at"], req["expires_at"])
        elif kind == "get":
            out = await s.get(req["code"], req["now_ts"])
        elif kind == "reply":
            out = await s.reply(req["code"], req["content"], parse_ts(req["now_ts"]))
        elif kind == "sweep":
            out = s.sweep_now(req["now_ts"])
        elif kind == "used":
            out = s.used()
        else:
            raise ValueError("unknown op")
        return out

    def holds_state(self) -> bool:
        return self.store.size() > 0

    def on_tick(self) -> None:
        self.store.sweep_now(ts(utcnow()))


class MeldMeta(_Resident, DurableObject):
    """Pilot counters and extension state. Memory only; a restart resets them."""

    def __init__(self, ctx=None, env=None) -> None:
        super().__init__(ctx, env)
        self.counters = MemoryCounters()
        self.sets: dict[str, dict] = {}
        self._armed = False

    async def op(self, payload: str) -> str:
        req = json.loads(payload)
        kind = req.get("op")
        if kind == "incr":
            self.counters.incr(req["day"], req["event"], int(req.get("n", 1)))
            out = None
        elif kind == "rows":
            out = self.counters.rows()
        elif kind == "put":  # sets[name][key] = value
            self.sets.setdefault(req["name"], {})[req["key"]] = req["value"]
            out = None
        elif kind == "items":
            out = dict(self.sets.get(req["name"], {}))
        elif kind == "drop":
            got = self.sets.get(req["name"], {})
            for key in req["keys"]:
                got.pop(key, None)
            out = None
        else:
            raise ValueError("unknown op")
        self._arm()
        return json.dumps(out)

    def holds_state(self) -> bool:
        return True

    def on_tick(self) -> None:
        pass


async def _call(stub, req: dict):
    out = json.loads(str(await stub.op(json.dumps(req))))
    if isinstance(out, dict) and out.get("full") is True and len(out) == 1:
        raise StoreFull()
    return out


def shard_name(code: str) -> str:
    return f"bridges-{int(hashlib.sha256(code.encode()).hexdigest()[:8], 16) % SHARDS}"


class DOStore:
    """meld_store interface over the BridgeShard objects."""

    def __init__(self, ns_getter) -> None:
        self.ns_getter = ns_getter

    def _stub(self, name: str):
        ns = self.ns_getter()
        if ns is None:
            raise RuntimeError("BRIDGES binding missing")
        return ns.getByName(name)

    async def create(self, code, note, created_at, expires_at) -> bool:
        return bool(await _call(self._stub(shard_name(code)), {
            "op": "create", "code": code, "note": note, "created_at": created_at, "expires_at": expires_at}))

    async def get(self, code, now_ts):
        return await _call(self._stub(shard_name(code)), {"op": "get", "code": code, "now_ts": now_ts})

    async def reply(self, code, content, now):
        return await _call(self._stub(shard_name(code)), {
            "op": "reply", "code": code, "content": content, "now_ts": ts(now)})

    async def sweep(self, now_ts) -> int:
        n = 0
        for i in range(SHARDS):
            n += int(await _call(self._stub(f"bridges-{i}"), {"op": "sweep", "now_ts": now_ts}))
        return n


class MetaClient:
    """Counters and extension state in the MeldMeta object."""

    def __init__(self, ns_getter) -> None:
        self.ns_getter = ns_getter

    def available(self) -> bool:
        return self.ns_getter() is not None

    async def call(self, op: str, **fields):
        ns = self.ns_getter()
        if ns is None:
            return None
        return await _call(ns.getByName("meta"), dict(fields, op=op))

    async def incr(self, day: str, key: str) -> None:
        await self.call("incr", day=day, event=key)


def _binding(name: str):
    env = _env.get()
    return getattr(env, name, None) if env is not None else None


def _notices(request) -> tuple:
    """Extra trust lines from the TRUST_NOTICES var, separated by '|'."""
    env = _env.get()
    raw = str(getattr(env, "TRUST_NOTICES", "") or "") if env is not None else ""
    return tuple(n.strip() for n in raw.split("|") if n.strip())


def _origin(request) -> str:
    env = _env.get()
    return str(getattr(env, "SHARE_ORIGIN", "") or "").strip() if env is not None else ""


try:  # optional static card image; bytes only, no behavior
    from assets import ASSETS
except ImportError:
    ASSETS = {}

store = DOStore(lambda: _binding(BRIDGES_BINDING))
meta = MetaClient(lambda: _binding(META_BINDING))
_meld_app = build_app(store, hooks=FunnelHooks(lambda: meta if meta.available() else None),
                      assets=ASSETS, origin_from=_origin, notices_from=_notices)
_meld_app.meta = meta
core = _meld_app.meld

# Optional deployment extension (outside SPEC.md). If a pilot_ext module sits
# next to this file, it may wrap the ASGI app and add work to the sweep. It
# must not change create, read, reply, or 404; its own tests enforce that.
try:
    import pilot_ext
except ImportError:
    pilot_ext = None
if pilot_ext is not None:
    _meld_app = pilot_ext.wrap(_meld_app)


async def app(scope, receive, send):
    _env.set(scope.get("env"))
    await _meld_app(scope, receive, send)


async def scheduled_sweep(env) -> int:
    _env.set(env)
    n = await core.sweep()
    if pilot_ext is not None:
        try:
            await pilot_ext.sweep(env)
        except Exception:
            pass
    return n


async def serve(asgi_app, scope: dict, body: bytes):
    """Run one buffered HTTP request through an ASGI app inside the caller's
    own task. Returns (status, [(name, value)], body).

    The SDK adapter starts two extra asyncio tasks per request (lifespan and
    the app). Under a concurrent burst those extra Pyodide stack switches hit
    "Cannot enter a promising task from inside another running promising
    task", which wedges the isolate. meld never streams and has no lifespan
    work on Workers, so one task per request is enough."""
    sent = {"status": 500, "headers": [], "body": []}
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if pending:
            return pending.pop()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = int(msg["status"])
            sent["headers"] = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in msg.get("headers", [])]
        elif msg["type"] == "http.response.body":
            sent["body"].append(bytes(msg.get("body", b"")))

    await asgi_app(scope, receive, send)
    return sent["status"], sent["headers"], b"".join(sent["body"])


try:
    from workers import asgi
except ImportError:  # not on Workers
    asgi = None

if asgi is not None:
    from workers import WorkerEntrypoint

    _NULL_BODY = frozenset({101, 103, 204, 205, 304})

    class Default(WorkerEntrypoint):
        async def fetch(self, request):
            from js import Response
            from pyodide.ffi import create_proxy
            from workers.utils import _to_js_headers

            scope = asgi.request_to_scope(request, self.env)
            body = b""
            if request.method not in ("GET", "HEAD"):
                body = (await request.buffer()).to_bytes()
            status, headers, out = await serve(app, scope, body)
            if status in _NULL_BODY or request.method == "HEAD" or not out:
                return Response.new(None, status=status, headers=_to_js_headers(headers))
            px = create_proxy(out)
            buf = px.getBuffer()
            px.destroy()
            return Response.new(buf.data, status=status, headers=_to_js_headers(headers))

        async def scheduled(self, controller, env=None, ctx=None):
            await scheduled_sweep(self.env)
else:
    Default = None
