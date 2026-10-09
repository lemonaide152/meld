"""Storage for meld. Same async interface for memory (self-host) and D1 (hosted).

A store holds a bridge only while it is live. Expired rows are deleted by
sweep() and are treated as missing on every read and reply before that.
Nothing records that a code existed.
"""
from __future__ import annotations

import threading
from typing import Optional

from meld_spec import idle_expiry, ts, REPLY_CAP


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
            meld = self._melds.get(code)
            if meld is None:
                return None
            if meld["expires_at"] <= now_ts:
                del self._melds[code]
                return None
            return _copy(meld)

    async def reply(self, code: str, content: str, now) -> Optional[dict]:
        now_ts = ts(now)
        with self._lock:
            meld = self._melds.get(code)
            if meld is None:
                return None
            if meld["expires_at"] <= now_ts:
                del self._melds[code]
                return None
            if meld["reply_count"] >= REPLY_CAP:
                return None
            meld["replies"].append({"content": content, "created_at": now_ts})
            meld["reply_count"] += 1
            meld["expires_at"] = idle_expiry(now)
            return _copy(meld)

    async def sweep(self, now_ts: str) -> int:
        with self._lock:
            dead = [c for c, m in self._melds.items() if m["expires_at"] <= now_ts]
            for c in dead:
                del self._melds[c]
            return len(dead)

    async def count(self) -> int:
        with self._lock:
            return len(self._melds)


def _copy(meld: dict) -> dict:
    out = dict(meld)
    out["replies"] = [dict(r) for r in meld["replies"]]
    return out


def _py(value):
    """D1 results arrive as JS proxies on Workers. Convert to Python."""
    to_py = getattr(value, "to_py", None)
    return to_py() if callable(to_py) else value


def _changes(result) -> int:
    result = _py(result)
    try:
        return int(result["meta"]["changes"])
    except Exception:
        try:
            return int(result.meta.changes)
        except Exception:
            return -1


def _rows(result) -> list:
    result = _py(result)
    if result is None:
        return []
    if isinstance(result, dict):
        return [dict(_py(r)) for r in (result.get("results") or [])]
    rows = getattr(result, "results", None)
    if rows is not None:
        return [dict(_py(r)) for r in rows]
    return [dict(r) for r in result]


class D1Store:
    """Cloudflare D1. The hosted pilot persists a bridge only while it is live."""

    def __init__(self, db) -> None:
        self.db = db

    async def create(self, code, note, created_at, expires_at) -> bool:
        res = await self.db.prepare(
            "INSERT OR IGNORE INTO melds (code, note, created_at, expires_at, reply_count)"
            " VALUES (?, ?, ?, ?, 0)"
        ).bind(code, note, created_at, expires_at).run()
        return _changes(res) != 0

    async def get(self, code, now_ts) -> Optional[dict]:
        row = _py(await self.db.prepare(
            "SELECT code, note, created_at, expires_at, reply_count FROM melds WHERE code = ?"
        ).bind(code).first())
        if not row:
            return None
        meld = dict(row)
        if meld["expires_at"] <= now_ts:
            await self._delete(code)
            return None
        got = await self.db.prepare(
            "SELECT content, created_at FROM replies WHERE code = ? ORDER BY id"
        ).bind(code).all()
        meld["replies"] = [{"content": r["content"], "created_at": r["created_at"]} for r in _rows(got)]
        return meld

    async def reply(self, code, content, now) -> Optional[dict]:
        now_ts = ts(now)
        # One conditional UPDATE decides live + under cap + new clock atomically.
        res = await self.db.prepare(
            "UPDATE melds SET reply_count = reply_count + 1, expires_at = ?"
            " WHERE code = ? AND expires_at > ? AND reply_count < ?"
        ).bind(idle_expiry(now), code, now_ts, REPLY_CAP).run()
        if _changes(res) == 0:
            return None
        await self.db.prepare(
            "INSERT INTO replies (code, content, created_at) VALUES (?, ?, ?)"
        ).bind(code, content, now_ts).run()
        return await self.get(code, now_ts)

    async def _delete(self, code) -> None:
        await self.db.prepare("DELETE FROM replies WHERE code = ?").bind(code).run()
        await self.db.prepare("DELETE FROM melds WHERE code = ?").bind(code).run()

    async def sweep(self, now_ts) -> int:
        await self.db.prepare(
            "DELETE FROM replies WHERE code IN (SELECT code FROM melds WHERE expires_at <= ?)"
        ).bind(now_ts).run()
        res = await self.db.prepare("DELETE FROM melds WHERE expires_at <= ?").bind(now_ts).run()
        # Replies whose meld is already gone (a crash between the two deletes).
        await self.db.prepare(
            "DELETE FROM replies WHERE code NOT IN (SELECT code FROM melds)"
        ).bind().run()
        return max(0, _changes(res))
