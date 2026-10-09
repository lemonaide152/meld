"""Hosted pilot entry for Cloudflare Python Workers. Builds from this repo.

Same meld_app as the self-host server; only the store (D1) and the pilot
counters (pilot.py) differ. The scheduled handler runs the 5-minute sweep.
"""
from __future__ import annotations

import contextvars

from meld_app import build_app
from meld_store import D1Store
from pilot import FunnelHooks

_env = contextvars.ContextVar("meld_env", default=None)


def _db():
    env = _env.get()
    return getattr(env, "DB", None) if env is not None else None


class _EnvD1Store(D1Store):
    """D1Store whose binding comes from the current request's env."""

    def __init__(self) -> None:
        pass

    @property
    def db(self):
        return _db()


def _origin(request) -> str:
    env = _env.get()
    return str(getattr(env, "SHARE_ORIGIN", "") or "").strip() if env is not None else ""


try:  # optional static card image; bytes only, no behavior
    from assets import ASSETS
except ImportError:
    ASSETS = {}

_store = _EnvD1Store()
# memory_only stays False while this Worker stores bridges in D1. Flip it only when the
# hosted store is really memory-only; it switches /trust, the docs, and the page copy.
_meld_app = build_app(_store, hooks=FunnelHooks(_db), assets=ASSETS, origin_from=_origin, memory_only=False)
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
    token = _env.set(scope.get("env"))
    try:
        await _meld_app(scope, receive, send)
    finally:
        _env.reset(token)


async def scheduled_sweep(env) -> int:
    token = _env.set(env)
    try:
        n = await core.sweep()
        if pilot_ext is not None:
            try:
                await pilot_ext.sweep(env)
            except Exception:
                pass
        return n
    finally:
        _env.reset(token)


try:
    from workers import asgi
except ImportError:  # not on Workers (tests import this module directly)
    asgi = None

if asgi is not None:
    class Default(asgi.entrypoint(app)):
        async def scheduled(self, controller, env=None, ctx=None):
            await scheduled_sweep(self.env)
else:
    Default = None
