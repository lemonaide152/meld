"""Pilot instrumentation for the hosted pilot. Outside SPEC.md.

Daily aggregate counters in D1 table funnel_events(day, event, n). Each key is
a literal from this file; a request value only selects one of them. Nothing
here sees a note, reply, code, IP, User-Agent, or header, and nothing here can
change a response: meld_app swallows every hook error.

Keys (UTC day):
  created, created:<ui|api|mcp>
  replied, replied:<ui|api|mcp>
  create_400:<reason>, create_400:<reason>:<ui|api|mcp>
  mcp_call:<meld_create|meld_resolve|meld_read|unknown>

`ui` comes from the X-Meld-Surface header, so a caller can choose ui or api.
Read ui+api as one web-and-REST number. `mcp` is set by the server.
"""
from __future__ import annotations

from meld_spec import ts, utcnow

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
    if event in ("created", "replied"):
        return [event] + ([f"{event}:{path}"] if path else [])
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
        for key in keys_for(name, **fields):
            await db.prepare(
                "INSERT INTO funnel_events (day, event, n) VALUES (?, ?, 1) "
                "ON CONFLICT(day, event) DO UPDATE SET n = n + 1"
            ).bind(day, key).run()
