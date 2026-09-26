"""Reject empty/whitespace-only context on create + resolve (worker.py)."""
import asyncio
import sys
import types
from pathlib import Path

DEPLOY = str(Path(__file__).resolve().parent / "deploy")
sys.path.insert(0, DEPLOY)

workers_mod = types.ModuleType("workers")
asgi_mod = types.ModuleType("workers.asgi")
asgi_mod.asgi = lambda app, **kw: app
asgi_mod.entrypoint = lambda app, **kw: app
workers_mod.asgi = asgi_mod
sys.modules["workers"] = workers_mod
sys.modules["workers.asgi"] = asgi_mod
for _name, _attr in [("spa_content", "_SPA_HTML"), ("app_content", "APP_HTML"),
                     ("agents_content", "AGENTS_HTML")]:
    _m = types.ModuleType(_name)
    setattr(_m, _attr, "<html></html>")
    sys.modules[_name] = _m

import worker  # noqa: E402
from fastapi import HTTPException  # noqa: E402

passed = total = 0

def ok(name, cond, detail=""):
    global passed, total
    total += 1
    if cond:
        passed += 1
        print(f"  OK {name}")
    else:
        print(f"  FAIL {name} — {detail}")

def expect_400(label, value, msg_substr="non-empty"):
    try:
        worker._require_context(value)
        ok(label, False, "expected HTTPException")
    except HTTPException as e:
        ok(label, e.status_code == 400 and msg_substr in str(e.detail), f"{e.status_code} {e.detail}")

print("empty-context unit")
expect_400("empty string", "")
expect_400("spaces", "   ")
expect_400("whitespace mix", " \n\t ")
try:
    worker._require_context(None)
    ok("non-string None", False, "expected HTTPException")
except HTTPException as e:
    ok("non-string None", e.status_code == 400 and "string" in str(e.detail), f"{e.status_code} {e.detail}")
try:
    worker._require_context(123)
    ok("non-string int", False, "expected HTTPException")
except HTTPException as e:
    ok("non-string int", e.status_code == 400 and "string" in str(e.detail), f"{e.status_code} {e.detail}")

got = worker._require_context("hello")
ok("non-empty accepted", got == "hello", repr(got))
got2 = worker._require_context("  keep padding  ")
ok("padded non-empty accepted (verbatim)", got2 == "  keep padding  ", repr(got2))

try:
    worker._require_context("x" * (worker.MAX_CONTEXT + 1))
    ok("too large", False, "expected HTTPException")
except HTTPException as e:
    ok("too large", e.status_code == 400 and "too large" in str(e.detail).lower(), f"{e.status_code} {e.detail}")

print(f"\n{passed}/{total} passed")
raise SystemExit(0 if passed == total else 1)
