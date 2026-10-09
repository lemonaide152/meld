"""Storage for meld: memory only, on every deployment (SPEC.md §2, §7).

A store holds a bridge only while it is live. Expired bridges are dropped by
sweep() and are treated as missing on every read and reply before that.
Nothing records that a code existed. A restart drops every live link.

The self-host server keeps one MemoryStore in its process. The hosted Worker
keeps MemoryStores inside Durable Objects (worker.py), in memory only.
"""
from __future__ import annotations

import threading
from typing import Optional

from meld_spec import idle_expiry, ts, REPLY_CAP

# SPEC.md §4 data model. check_spec.py compares these with the meld-spec block.
MELD_FIELDS = ("code", "note", "created_at", "expires_at", "reply_count")
REPLY_FIELDS = ("id", "code", "content", "created_at")


class MemoryStore:
    """Memory-only. A restart drops every live link."""

    def __init__(self) -> None:
        self._melds: dict[str, dict] = {}
        self._lock = threading.Lock()

    async def create(self, code: str, note: str, created_at: str, expires_at: str) -> bool:
        with self._lock:
            if code in self._melds:
                return False
            self._melds[code] = {
                "code": code, "note": note, "created_at": created_at,
                "expires_at": expires_at, "reply_count": 0, "replies": [],
            }
            return True

    async def get(self, code: str, now_ts: str) -> Optional[dict]:
        with self._lock:
            meld = self._live(code, now_ts)
            return None if meld is None else _copy(meld)

    async def reply(self, code: str, content: str, now) -> Optional[dict]:
        now_ts = ts(now)
        with self._lock:
            meld = self._live(code, now_ts)
            if meld is None or meld["reply_count"] >= REPLY_CAP:
                return None
            meld["reply_count"] += 1
            meld["replies"].append({"id": meld["reply_count"], "code": code,
                                    "content": content, "created_at": now_ts})
            meld["expires_at"] = idle_expiry(now)
            return _copy(meld)

    async def sweep(self, now_ts: str) -> int:
        return self.sweep_now(now_ts)

    def sweep_now(self, now_ts: str) -> int:
        """Drop every expired bridge. Synchronous so a timer callback can call it."""
        with self._lock:
            dead = [c for c, m in self._melds.items() if m["expires_at"] <= now_ts]
            for c in dead:
                del self._melds[c]
            return len(dead)

    async def count(self) -> int:
        return self.size()

    def size(self) -> int:
        with self._lock:
            return len(self._melds)

    def _live(self, code: str, now_ts: str) -> Optional[dict]:
        meld = self._melds.get(code)
        if meld is not None and meld["expires_at"] <= now_ts:
            del self._melds[code]
            return None
        return meld


def _copy(meld: dict) -> dict:
    out = dict(meld)
    out["replies"] = [dict(r) for r in meld["replies"]]
    return out
