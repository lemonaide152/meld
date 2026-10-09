"""Pilot instrumentation for the hosted pilot. Outside SPEC.md.

Daily aggregate counters in D1 table funnel_events(day, event, n). Each key is
a literal from this file; a request value only selects one of them. Nothing
here sees a note, reply, code, IP, User-Agent, or header, and nothing here can
change a response: meld_app swallows every hook error.

Keys (UTC day), continuing the #28 series:
  created, and exactly one of created:team | created:ext:learn<0|1>:<ui|api|mcp>
  resolved, and exactly one of resolved:team | resolved:ext
  create_400:<reason>, create_400:<reason>:<ui|api|mcp>
  mcp_call:<meld_create|meld_resolve|meld_read|unknown>

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
    if event == "mcp_call":
        tool = fields.get("tool") if fields.get("tool") in TOOLS else "unknown"
        return [f"mcp_call:{tool}"]
    return []


class FunnelHooks:
    def __init__(self, db_getter) -> None:
        self.db_getter = db_getter

    async def event(self, name: str, **fields) -> None:
        db = self.db_getter()
        if db is None:
            return
        day = ts(utcnow())[:10]
        fields = dict(fields, team=TEAM.get(), learn=LEARN.get())
        for key in keys_for(name, **fields):
            await db.prepare(
                "INSERT INTO funnel_events (day, event, n) VALUES (?, ?, 1) "
                "ON CONFLICT(day, event) DO UPDATE SET n = n + 1"
            ).bind(day, key).run()
