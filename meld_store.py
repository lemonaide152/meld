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


class StoreFull(Exception):
    """The store is at its bridge or byte cap. Nothing was written."""


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


class MemoryStore:
    """Memory-only. A restart drops every live link.

    max_bridges / max_bytes cap the live bridges and the UTF-8 bytes of their
    notes and replies. A write that would pass a cap raises StoreFull after
    expired bridges are dropped. None means no cap.
    """

    def __init__(self, max_bridges: Optional[int] = None, max_bytes: Optional[int] = None) -> None:
        self._melds: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.max_bridges, self.max_bytes = max_bridges, max_bytes
        self._bytes = 0

    def _room(self, add_bridges: int, add_bytes: int, now_ts: str) -> bool:
        def fits() -> bool:
            return ((self.max_bridges is None or len(self._melds) + add_bridges <= self.max_bridges)
                    and (self.max_bytes is None or self._bytes + add_bytes <= self.max_bytes))
        if fits():
            return True
        self._sweep_locked(now_ts)
        return fits()

    def used(self) -> dict:
        with self._lock:
            return {"bridges": len(self._melds), "bytes": self._bytes}

    async def create(self, code: str, note: str, created_at: str, expires_at: str) -> bool:
        with self._lock:
            if code in self._melds:
                return False
            if not self._room(1, _size(note), created_at):
                raise StoreFull()
            self._bytes += _size(note)
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
            if not self._room(0, _size(content), now_ts):
                raise StoreFull()
            self._bytes += _size(content)
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
            return self._sweep_locked(now_ts)

    def _sweep_locked(self, now_ts: str) -> int:
        dead = [c for c, m in self._melds.items() if m["expires_at"] <= now_ts]
        for c in dead:
            self._drop(c)
        return len(dead)

    def _drop(self, code: str) -> None:
        meld = self._melds.pop(code)
        self._bytes -= _size(meld["note"]) + sum(_size(r["content"]) for r in meld["replies"])

    async def count(self) -> int:
        return self.size()

    def size(self) -> int:
        with self._lock:
            return len(self._melds)

    def _live(self, code: str, now_ts: str) -> Optional[dict]:
        meld = self._melds.get(code)
        if meld is not None and meld["expires_at"] <= now_ts:
            self._drop(code)
            return None
        return meld


def _copy(meld: dict) -> dict:
    out = dict(meld)
    out["replies"] = [dict(r) for r in meld["replies"]]
    return out
