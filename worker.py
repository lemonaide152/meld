"""Hosted entry for Cloudflare Python Workers. Memory-only, as SPEC.md requires.

Same meld_app as the self-host server. Bridges live in MemoryStores inside
Durable Objects (BridgeShard), in Python memory only. Nothing here uses a database,
KV, or the Durable Object storage API. A Durable Object restart, eviction, or
deploy drops the links it held; they then return the uniform 404, the same as
a self-host restart.

- Routing: a code picks one of SHARDS objects by SHA-256. Changing SHARDS
  remaps codes, which drops live links the same way a restart does. SHARDS is
  1 to cap residency cost: at most one bridge object is held in memory, and
  only while it holds a live bridge (about 10,800 GB-s a day at 128 MB if a
  live bridge exists all day, nothing while none does).
- Residency: while a bridge object holds a live (unexpired) bridge it keeps one pending in-memory timer
  (setTimeout). A pending timer alone does not keep a Durable Object in memory:
  an object with no incoming calls is evicted after a minute or two. So each
  tick also sends the object one "touch" call through its own stub, an
  incoming call with no payload, and runs the sweep. An empty object lets its
  timer lapse and is evicted as before. Each tick sweeps first, so an object
  whose last bridge expired stops touching on that tick. MeldMeta never holds
  itself in memory: its counters, sets and budgets reset when it is evicted.
  No storage API and no alarm.
- Sweep: every tick (KEEPALIVE_SECONDS), plus the 5-minute cron, plus a lazy
  check on each read and reply.
- Memory guard: each shard caps its live bridges and their text bytes
  (SHARD_MAX_BRIDGES, SHARD_MAX_BYTES). At the cap, create and reply get the
  one 503 body and nothing is stored, instead of the isolate running out of
  memory and dropping every link it holds.
- Per-client byte quota (hosted layer): the bridge shard also keeps a ledger
  of text bytes written per client key (an HMAC the hosted layer computes;
  never an IP), summed per key per hour and expiring once an hour's charges
  are a full bridge lifetime old (op "charge"); at most 37 entries per key and
  LEDGER_MAX_KEYS keys, so its memory is bounded whatever the write rate. It lives in the same object as the bridges it covers, so an
  eviction drops both together. LEDGER_SHARD is the only shard while SHARDS is 1.
- Pilot counters and a deployment's extension state live in one MeldMeta
  object, in memory only. A restart or an idle eviction resets them. Its
  "take_all" op checks every request-budget key in one call, all or nothing,
  and counts a refusal in the same call, so a 429 costs one object call.
- Logging: nothing here prints or logs. Bridge text never leaves the
  objects except in the HTTP response to the link holder.
"""
from __future__ import annotations

import hashlib
import json

from meld_app import build_app
from meld_spec import HOSTED_SHARE_ORIGINS, IDLE_HOURS, OPEN_HOURS, parse_ts, ts, utcnow
from meld_store import MemoryStore, StoreFull
from pilot import FunnelHooks, MemoryCounters

try:
    from workers import DurableObject
except ImportError:  # not on Workers (tests import this module directly)
    class DurableObject:  # type: ignore[no-redef]
        def __init__(self, ctx=None, env=None) -> None:
            self.ctx, self.env = ctx, env

SHARDS = 1
KEEPALIVE_SECONDS = 30
SHARD_MAX_BRIDGES = 1000
SHARD_MAX_BYTES = 8 * 1024 * 1024
LEDGER_SHARD = "bridges-0"  # holds the byte-quota ledger; the only shard while SHARDS is 1
LEDGER_MAX_KEYS = 10000  # a full table refuses new keys (fail closed); bounded memory, see test_server
LEDGER_TTL_SECONDS = max(OPEN_HOURS, IDLE_HOURS) * 3600
LEDGER_BUCKET_SECONDS = 3600  # charges are summed per key per hour: at most TTL/bucket + 1 entries per key

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


def _spawn(coro) -> None:
    """Run a coroutine from a timer callback; nothing is awaited or kept."""
    try:
        import asyncio
        asyncio.ensure_future(coro)
    except Exception:
        coro.close()


class _Resident:
    """Keep the object in memory while it holds state: a pending timer plus one
    incoming touch call per tick through the object's own stub. Memory only."""

    timer = staticmethod(_set_timer)  # tests swap this
    spawn = staticmethod(_spawn)  # tests swap this

    def own_namespace(self):
        return None

    def _own_stub(self):
        ns = self.own_namespace()
        ctx = self.ctx
        if ns is None or ctx is None:
            return None
        return ns.get(ctx.id)

    async def _touch(self) -> None:
        try:
            stub = self._own_stub()
            if stub is not None:
                await stub.op('{"op": "touch"}')
        except Exception:
            pass

    _armed = False

    def _arm(self) -> None:
        if self._armed:
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
            self.spawn(self._touch())


class BridgeShard(_Resident, DurableObject):
    """Live bridges for the codes that hash to this shard. Memory only."""

    def __init__(self, ctx=None, env=None) -> None:
        super().__init__(ctx, env)
        self.store = MemoryStore(max_bridges=SHARD_MAX_BRIDGES, max_bytes=SHARD_MAX_BYTES)
        self.ledger: dict[str, dict] = {}  # client key -> {hour bucket: bytes}; memory only
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
        elif kind == "charge":
            out = self.charge([(str(k), int(q)) for k, q in req["keys"]], int(req["n"]), float(req["now"]),
                              min(int(req.get("max_keys", LEDGER_MAX_KEYS)), LEDGER_MAX_KEYS))
        elif kind == "touch":
            out = None
        else:
            raise ValueError("unknown op")
        return out

    @staticmethod
    def _live(buckets: dict, now: float) -> dict:
        """Drop hour buckets whose every charge has expired (bucket end + TTL)."""
        floor = (now - LEDGER_TTL_SECONDS) // LEDGER_BUCKET_SECONDS  # buckets ending before now - TTL
        for b in [b for b in buckets if b < floor]:
            del buckets[b]
        return buckets

    def _purge(self, now: float) -> None:
        for key in list(self.ledger):
            if not self._live(self.ledger[key], now):
                del self.ledger[key]

    def charge(self, keys: list, n: int, now: float, max_keys: int) -> bool:
        """Charge n bytes to every (key, quota) pair, all or nothing. Charges
        are summed per key per hour bucket and a bucket expires once its last
        charge is LEDGER_TTL_SECONDS old, so a key holds at most
        TTL/bucket + 1 entries however many writes it makes. A full key table
        (max_keys, never above LEDGER_MAX_KEYS) refuses new keys. Expired keys
        are purged every tick. The ledger never holds the object in memory."""
        if n < 0:
            return False
        live = {k: self._live(self.ledger[k], now) for k, _ in keys if k in self.ledger}
        new = sum(1 for k, _ in keys if k not in live)
        if len(self.ledger) + new > max_keys:
            return False
        for k, quota in keys:
            if sum(live.get(k, {}).values()) + n > quota:
                return False
        bucket = int(now // LEDGER_BUCKET_SECONDS)
        for k, _ in keys:
            got = self.ledger.setdefault(k, {})
            got[bucket] = got.get(bucket, 0) + n
        return True

    def holds_state(self) -> bool:
        return self.store.size() > 0

    def own_namespace(self):
        return self.env.BRIDGES if self.env is not None else None

    def on_tick(self) -> None:
        self.store.sweep_now(ts(utcnow()))
        self._purge(utcnow().timestamp())


class MeldMeta(_Resident, DurableObject):
    """Pilot counters and extension state. Memory only; a restart resets them."""

    def __init__(self, ctx=None, env=None) -> None:
        super().__init__(ctx, env)
        self.counters = MemoryCounters()
        self.sets: dict[str, dict] = {}
        self.window = (-1, 0)  # (window number, window seconds) for "take"
        self.taken: dict[str, int] = {}
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
        elif kind == "take":  # fixed-window budget: True while key is under limit
            out = self.take(str(req["key"]), int(req["limit"]), int(req["window"]), float(req["now"]),
                            int(req.get("max_keys", 50000)))
        elif kind == "take_all":  # every key in one call, all or nothing; a refusal (and any
            # refusals the caller answered from its own cache, "count") is counted here
            ok = self.take_all([(str(k), int(n)) for k, n in req["keys"]], int(req["window"]), float(req["now"]),
                               int(req.get("max_keys", 50000)))
            for event in (req.get("count") or []) + ([] if ok else (req.get("deny_events") or [])):
                self.counters.incr(str(req["day"]), str(event), 1)
            out = ok
        elif kind == "drop":
            got = self.sets.get(req["name"], {})
            for key in req["keys"]:
                got.pop(key, None)
            if not got:
                self.sets.pop(req["name"], None)
            out = None
        elif kind == "touch":
            out = None
        else:
            raise ValueError("unknown op")
        if self.holds_state():
            self._arm()
        return json.dumps(out)

    def take(self, key: str, limit: int, window: int, now: float, max_keys: int) -> bool:
        """One unit of a fixed-window budget, in memory only. A new window
        clears every key. A full key table refuses new keys. Budgets do not
        hold the object in memory; an eviction just starts a fresh window."""
        w = (int(now // max(window, 1)), window)
        if w != self.window:
            self.window, self.taken = w, {}
        n = self.taken.get(key)
        if n is None and len(self.taken) >= max_keys:
            return False
        if (n or 0) >= limit:
            return False
        self.taken[key] = (n or 0) + 1
        return True

    def take_all(self, keys: list, window: int, now: float, max_keys: int) -> bool:
        """take() for several keys at once: consumes nothing unless every key
        is under its limit."""
        w = (int(now // max(window, 1)), window)
        if w != self.window:
            self.window, self.taken = w, {}
        new = sum(1 for k, _ in keys if k not in self.taken)
        if len(self.taken) + new > max_keys:
            return False
        if any(self.taken.get(k, 0) >= limit for k, limit in keys):
            return False
        for k, _ in keys:
            self.taken[k] = self.taken.get(k, 0) + 1
        return True

    def holds_state(self) -> bool:
        """MeldMeta never holds itself in memory (residency cost): counters,
        sets and budgets live only while requests keep the object in memory,
        and reset when it is evicted."""
        return False

    def own_namespace(self):
        return self.env.META if self.env is not None else None

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

    async def charge(self, keys: list, n: int, now: float) -> bool:
        """Byte quota for the hosted layer: True when n bytes fit under every
        (client key, quota) pair; nothing is charged otherwise."""
        return await _call(self._stub(LEDGER_SHARD), {"op": "charge", "keys": [list(k) for k in keys],
                                                      "n": int(n), "now": float(now)}) is True

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


# Hosted Worker SHARE_ORIGIN values (meld_spec.HOSTED_SHARE_ORIGINS, shared
# with the hosted layer). base() and fetch() refuse anything else and never
# fall back to the request Host header; fetch() also refuses a request whose
# URL is on a different approved origin (request_allowed).
APPROVED_SHARE_ORIGINS = frozenset(HOSTED_SHARE_ORIGINS)


def _bridges():
    env = _env.get()
    if env is None:
        return None
    try:
        return env.BRIDGES
    except AttributeError:
        return None


def _meta():
    env = _env.get()
    if env is None:
        return None
    try:
        return env.META
    except AttributeError:
        return None


def _notices(request) -> tuple:
    """Extra trust lines from the TRUST_NOTICES var, separated by '|'."""
    env = _env.get()
    raw = ""
    if env is not None:
        try:
            raw = str(env.TRUST_NOTICES or "")
        except AttributeError:
            raw = ""
    return tuple(n.strip() for n in raw.split("|") if n.strip())


def _origin(request) -> str:
    origin = _origin_of(_env.get())
    return origin if valid_origin(origin) else ""


try:  # optional static card image; bytes only, no behavior
    from assets import ASSETS
except ImportError:
    ASSETS = {}

store = DOStore(_bridges)
meta = MetaClient(_meta)
_meld_app = build_app(store, hooks=FunnelHooks(lambda: meta if meta.available() else None),
                      assets=ASSETS, origin_from=_origin, notices_from=_notices,
                      allowed_origins=APPROVED_SHARE_ORIGINS)
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


def valid_origin(origin) -> bool:
    """SHARE_ORIGIN must be exactly one of the hardcoded hosted origins."""
    return isinstance(origin, str) and origin in APPROVED_SHARE_ORIGINS


def _on(url: str, origin: str) -> bool:
    return url == origin or url.startswith(origin + "/") or url.startswith(origin + "?")


def request_allowed(url, origin) -> bool:
    """False when SHARE_ORIGIN is not approved, or the request URL is on a
    different approved origin: a production Worker configured with the
    staging origin (or the reverse) refuses every request on its own host.
    Other hosts that route to the same Worker (its workers.dev name) keep
    serving; links are always minted on SHARE_ORIGIN, never the request host."""
    if not isinstance(url, str) or not valid_origin(origin):
        return False
    return not any(_on(url, o) for o in APPROVED_SHARE_ORIGINS if o != origin)


def _origin_of(env) -> str:
    """SHARE_ORIGIN, or "" when env is None, it is unset, or it is not a string."""
    if env is None:
        return ""
    try:
        raw = env.SHARE_ORIGIN
    except AttributeError:
        return ""
    return raw.strip() if isinstance(raw, str) else ""


def _unavailable():
    body = json.dumps({"error": "unavailable", "message": "Service unavailable"}).encode()
    return 503, [("content-type", "application/json"), ("cache-control", "no-store"),
                 ("x-content-type-options", "nosniff")], body


async def _read_capped(request) -> bytes:
    """Read a request body, never holding more than MAX_BODY_BYTES + 1 bytes.
    A declared Content-Length over the cap is not read at all: the app sees
    the header and answers its own 400/413. A chunked body over the cap is cut
    at cap + 1 bytes, which the app also rejects."""
    from meld_app import MAX_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared is not None and str(declared).isdigit() and int(str(declared)) > MAX_BODY_BYTES:
        return b""
    stream = request.js_object.body
    if not stream:  # None, or JS null (no body, e.g. OPTIONS)
        return b""
    reader = stream.getReader()
    parts, got = [], 0
    try:
        while got <= MAX_BODY_BYTES:
            chunk = await reader.read()
            if chunk.done:
                break
            data = chunk.value.to_bytes()
            parts.append(data)
            got += len(data)
    finally:
        try:
            reader.cancel()
        except Exception:
            pass
    return b"".join(parts)[: MAX_BODY_BYTES + 1]


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
            origin = _origin_of(self.env)
            if not valid_origin(origin) or not request_allowed(str(request.url), origin):
                status, headers, out = _unavailable()
            else:
                body = b""
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    body = await _read_capped(request)
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
