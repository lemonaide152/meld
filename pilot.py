"""Pilot instrumentation for the hosted pilot. Outside SPEC.md.

Daily aggregate counters (day, event, n), kept in memory only (MemoryCounters).
A restart resets them, the same as live bridges. Each key is
a literal from this file; a request value only selects one of them. Nothing
here sees a note, reply, code, IP, User-Agent, or header, and nothing here can
change a response: meld_app swallows every hook error.

Keys (UTC day), continuing the #28 series:
  created, and exactly one of created:team | created:ext:learn<0|1>:<ui|api|mcp>
  resolved, and exactly one of resolved:team | resolved:ext
  create_400:<reason>, create_400:<reason>:<ui|api|mcp>
  mcp_call:<meld_create|meld_resolve|meld_read|unknown>
  capacity_503, capacity_503:<ui|api|mcp>   (memory cap hit; nothing stored)
  rate_429:<ui|api|mcp>                     (only when a deployment's limiter is on)

`ui` comes from the X-Meld-Surface header, so a caller can choose ui or api.
Read ui+api as one web-and-REST number. `mcp` is set by the server.
"""
from __future__ import annotations

import contextvars

from meld_spec import ts, utcnow

# Set per request by a deployment's extension (the hosted pilot_ext). Defaults:
# outside traffic, learn off. They only pick which literal key is bumped.
TEAM = contextvars.ContextVar("meld_pilot_team", default=False)
LEARN = contextvars.ContextVar("meld_pilot_learn", default=False)

PATHS = frozenset({"ui", "api", "mcp"})
REASONS = frozenset({
    "body_invalid", "body_too_large",
    "context_invalid", "context_missing", "context_too_long", "context_mismatch",
    "note_invalid", "for_invalid", "not_for_invalid",
    "ttl_rejected", "email_rejected", "pin_rejected", "prev_code_rejected",
})
TOOLS = frozenset({"meld_create", "meld_resolve", "meld_read", "unknown"})


def keys_for(event: str, **fields) -> list[str]:
    path = fields.get("path") if fields.get("path") in PATHS else None
    team = bool(fields.get("team"))
    if event == "created":
        if team:
            return ["created", "created:team"]
        bit = 1 if fields.get("learn") else 0
        return ["created", f"created:ext:learn{bit}:{path or 'api'}"]
    if event == "replied":
        return ["resolved", "resolved:team" if team else "resolved:ext"]
    if event == "create_400":
        reason = fields.get("reason") if fields.get("reason") in REASONS else "other"
        return [f"create_400:{reason}"] + ([f"create_400:{reason}:{path}"] if path else [])
    if event == "capacity":
        return ["capacity_503"] + ([f"capacity_503:{path}"] if path else [])
    if event == "rate_429":
        return [f"rate_429:{path or 'api'}"]
    if event == "mcp_call":
        tool = fields.get("tool") if fields.get("tool") in TOOLS else "unknown"
        return [f"mcp_call:{tool}"]
    return []


class MemoryCounters:
    """Daily counts in memory only: {(day, event): n}. Keeps the last KEEP_DAYS days."""

    KEEP_DAYS = 31

    def __init__(self) -> None:
        self._n: dict[tuple, int] = {}

    def incr(self, day: str, event: str, n: int = 1) -> None:
        self._n[(day, event)] = self._n.get((day, event), 0) + n
        days = sorted({d for d, _ in self._n})
        for old in days[:-self.KEEP_DAYS]:
            for key in [k for k in self._n if k[0] == old]:
                del self._n[key]

    def rows(self) -> list:
        return [{"day": d, "event": e, "n": n} for (d, e), n in sorted(self._n.items())]


class FunnelHooks:
    """meld_app hooks -> counters. sink_getter returns an object with
    `incr(day, key)` (sync or async), or None to count nothing."""

    def __init__(self, sink_getter) -> None:
        self.sink_getter = sink_getter

    async def event(self, name: str, **fields) -> None:
        sink = self.sink_getter()
        if sink is None:
            return
        day = ts(utcnow())[:10]
        fields = dict(fields, team=TEAM.get(), learn=LEARN.get())
        for key in keys_for(name, **fields):
            got = sink.incr(day, key)
            if hasattr(got, "__await__"):
                await got
