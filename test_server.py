"""SPEC.md behavior, run against both stores (memory self-host and a D1 double).

    uv run --quiet --with fastapi --with pydantic --with httpx --with jsonschema python test_server.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import check_spec
import meld_app
import meld_spec
import pilot
from meld_app import build_app
from meld_store import D1Store, MemoryStore

ROOT = Path(__file__).resolve().parent
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
NF = json.dumps(meld_spec.NOT_FOUND_BODY, separators=(",", ":")).encode()
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"ok {name}")
    else:
        failed += 1
        print(f"FAIL {name} {detail}")


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


# ── D1 double over sqlite3 ───────────────────────────────────────────────
class _Stmt:
    def __init__(self, d1, sql):
        self.d1, self.sql, self.args = d1, sql, ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        cur = self.d1.conn.execute(self.sql, self.args)
        row = cur.fetchone()
        return dict(row) if row else None

    async def all(self):
        return {"results": [dict(r) for r in self.d1.conn.execute(self.sql, self.args).fetchall()]}

    async def run(self):
        cur = self.d1.conn.execute(self.sql, self.args)
        self.d1.conn.commit()
        return {"meta": {"changes": cur.rowcount}}


class FakeD1:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "schema.sql").read_text() + (ROOT / "pilot_schema.sql").read_text())
        self.prepared = 0

    def prepare(self, sql):
        self.prepared += 1
        return _Stmt(self, sql)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class Hooks:
    def __init__(self, boom=False):
        self.events, self.boom = [], boom

    async def event(self, name, **fields):
        self.events.append((name, fields))
        if self.boom:
            raise RuntimeError("instrumentation down")


def make(kind: str, hooks=None):
    clock = Clock()
    meld_app.utcnow = clock
    if kind == "memory":
        store, d1 = MemoryStore(), None
    else:
        d1 = FakeD1()
        store = D1Store(d1)
    client = TestClient(build_app(store, public_url="https://meld.test", hooks=hooks))
    return client, clock, store, d1


def create(c, **body):
    return c.post("/api/melds", json=body or {"note": "design review notes"})


def run_suite(kind: str) -> None:
    p = f"[{kind}] "
    c, clock, store, d1 = make(kind)

    # create
    r = create(c)
    j = r.json()
    check(p + "create 200", r.status_code == 200, r.text)
    check(p + "create returns exactly code, url, expires_at", set(j) == {"code", "url", "expires_at"}, str(j))
    check(p + "create url is /m/{code}", j["url"] == f"https://meld.test/m/{j['code']}")
    check(p + "open window is 36h", j["expires_at"] == meld_spec.ts(T0 + timedelta(hours=36)), j["expires_at"])
    check(p + "no token anywhere", "token" not in r.text)
    codes = {create(c).json()["code"] for _ in range(100)}
    check(p + "codes unique", len(codes) == 100)
    check(p + "code >= 128 bits", all(len(x) >= 22 for x in codes) and meld_spec.code_bits() >= 128)
    r = c.post("/api/melds", json={"context": "via context"})
    check(p + "context accepted", r.status_code == 200)
    check(p + "context stored as note", c.get(f"/api/melds/{r.json()['code']}").json()["note"] == "via context")
    r = c.post("/api/melds", json={"note": "same", "context": "same", "for": "same", "not_for": "same"})
    check(p + "identical note/context/for/not_for accepted", r.status_code == 200)

    for body, why in (
        ({"note": "a", "for": "b"}, "for differs"),
        ({"note": "a", "not_for": "b"}, "not_for differs"),
        ({"note": "a", "context": "b"}, "note and context differ"),
        ({"for": "a", "not_for": "a"}, "for/not_for without note"),
        ({}, "empty body"),
        ({"note": 5}, "non-string note"),
        ({"note": "a", "for": 5}, "non-string for"),
        ({"note": "   "}, "blank note"),
        ({"note": "x" * (meld_spec.MAX_CHARS + 1)}, "note over 100k"),
        ({"note": "a", "ttl": "24hr"}, "ttl"),
        ({"note": "a", "ttl": None}, "ttl null"),
        ({"note": "a", "email": "a@b.c"}, "email"),
        ({"note": "a", "pin": "1234"}, "pin"),
        ({"note": "a", "prev_code": "abc"}, "prev_code"),
    ):
        r = c.post("/api/melds", json=body)
        check(p + f"400 on {why}", r.status_code == 400 and r.json().get("detail"), f"{r.status_code} {r.text[:120]}")
    r = c.post("/api/melds", content=b"not json", headers={"content-type": "application/json"})
    check(p + "400 on non-JSON", r.status_code == 400)
    r = c.post("/api/melds", json=["a"])
    check(p + "400 on JSON array", r.status_code == 400)
    r = c.post("/api/melds", json={"note": "x" * meld_spec.MAX_CHARS})
    check(p + "exactly 100,000 chars accepted", r.status_code == 200)
    r = c.post("/api/melds", json={"note": "a", "learn": True})
    check(p + "learn is not part of create (ignored, same response shape)",
          r.status_code == 200 and set(r.json()) == {"code", "url", "expires_at"})

    # clock
    code = create(c).json()["code"]
    created = c.get(f"/api/melds/{code}").json()
    clock.advance(hours=10)
    for _ in range(5):
        c.get(f"/api/melds/{code}")
        c.get(f"/m/{code}")
    check(p + "reads do not move the clock", c.get(f"/api/melds/{code}").json()["expires_at"] == created["expires_at"])
    r = c.post(f"/api/melds/{code}/resolve", json={"context": "first reply"})
    j = r.json()
    check(p + "reply 200", r.status_code == 200, r.text)
    check(p + "first reply sets 24h from now", j["expires_at"] == meld_spec.ts(clock.now + timedelta(hours=24)))
    check(p + "reply listed with timestamp",
          j["replies"] == [{"content": "first reply", "created_at": meld_spec.ts(clock.now)}] and j["reply_count"] == 1)
    clock.advance(hours=23)
    j = c.post(f"/api/melds/{code}/resolve", json={"context": "second"}).json()
    check(p + "later reply resets 24h", j["expires_at"] == meld_spec.ts(clock.now + timedelta(hours=24)))
    for _ in range(6):  # no max lifetime: keep it alive for 6 more days
        clock.advance(hours=23)
        c.post(f"/api/melds/{code}/resolve", json={"context": "still here"})
    check(p + "no max lifetime once replies started", c.get(f"/api/melds/{code}").status_code == 200)
    check(p + "every reply kept", c.get(f"/api/melds/{code}").json()["reply_count"] == 8)
    clock.advance(hours=24)
    r = c.get(f"/api/melds/{code}")
    check(p + "24h of silence closes it (uniform 404)", r.status_code == 404 and r.content == NF, r.text)

    # open window without replies
    code2 = create(c).json()["code"]
    clock.advance(hours=35, minutes=59)
    check(p + "live at 35h59m", c.get(f"/api/melds/{code2}").status_code == 200)
    clock.advance(minutes=1)
    check(p + "closed at 36h", c.get(f"/api/melds/{code2}").content == NF)
    r = c.post(f"/api/melds/{code2}/resolve", json={"context": "late"})
    check(p + "reply to expired is the same 404", r.status_code == 404 and r.content == NF)

    # uniform not-found
    bodies = set()
    statuses = set()
    for path in ("/api/melds/never-existed", "/m/never-existed", f"/api/melds/{'a' * 200}"):
        r = c.get(path)
        bodies.add(r.content)
        statuses.add(r.status_code)
    r = c.post("/api/melds/never-existed/resolve", json={"context": "x"})
    bodies.add(r.content)
    statuses.add(r.status_code)
    check(p + "unknown codes: one status, one body", statuses == {404} and bodies == {NF}, f"{statuses} {bodies}")
    rs = [c.get("/api/melds/never-existed").status_code for _ in range(80)]
    rs += [c.post("/api/melds/never-existed/resolve", json={"context": "x"}).status_code for _ in range(40)]
    check(p + "no 429 or 410 under repeated misses", set(rs) == {404})
    r_live = c.post(f"/api/melds/{create(c).json()['code']}/resolve", json={"context": 5})
    r_dead = c.post("/api/melds/never-existed/resolve", json={"context": 5})
    check(p + "malformed reply is 400 on live and unknown alike (not an oracle)",
          r_live.status_code == r_dead.status_code == 400 and r_live.content == r_dead.content)
    r = c.post(f"/api/melds/{create(c).json()['code']}/resolve", json={"context": "x" * (meld_spec.MAX_CHARS + 1)})
    check(p + "reply over 100k is 400", r.status_code == 400)

    # reply cap
    capped = create(c).json()["code"]
    oks = [c.post(f"/api/melds/{capped}/resolve", json={"context": f"r{i}"}).status_code for i in range(meld_spec.REPLY_CAP)]
    check(p + "50 replies accepted", set(oks) == {200})
    r = c.post(f"/api/melds/{capped}/resolve", json={"context": "one too many"})
    check(p + "51st reply is the uniform 404", r.status_code == 404 and r.content == NF)
    check(p + "capped bridge still readable while live", c.get(f"/api/melds/{capped}").json()["reply_count"] == 50)

    # sweep
    live = create(c).json()["code"]
    c.post(f"/api/melds/{live}/resolve", json={"context": "keep"})
    clock.advance(hours=12)
    swept = asyncio.run(c.app.meld.sweep())
    check(p + "sweep deletes expired bridges", swept >= 1, str(swept))
    if d1 is not None:
        live_codes = {r[0] for r in d1.conn.execute("SELECT code FROM melds")}
        orphan = d1.conn.execute("SELECT COUNT(*) FROM replies WHERE code NOT IN (SELECT code FROM melds)").fetchone()[0]
        check(p + "no reply rows survive their meld", orphan == 0)
        check(p + "only live rows remain", live in live_codes and all(
            r[0] > meld_spec.ts(clock.now) for r in d1.conn.execute("SELECT expires_at FROM melds")))
        cols = [r[1] for r in d1.conn.execute("PRAGMA table_info(melds)")]
        check(p + "melds columns match spec", cols == ["code", "note", "created_at", "expires_at", "reply_count"])
        cols = [r[1] for r in d1.conn.execute("PRAGMA table_info(replies)")]
        check(p + "replies columns match spec", cols == ["id", "code", "content", "created_at"])
    else:
        check(p + "only live bridges remain in memory", asyncio.run(store.count()) >= 1 and all(
            m["expires_at"] > meld_spec.ts(clock.now) for m in store._melds.values()))
    clock.advance(hours=13)
    asyncio.run(c.app.meld.sweep())
    check(p + "swept code is the uniform 404", c.get(f"/api/melds/{live}").content == NF)

    # capability URL representations
    code3 = create(c, note="<b>hello</b> & bye").json()["code"]
    r = c.get(f"/m/{code3}", headers={"user-agent": "Slackbot-LinkExpanding 1.0"})
    r_unknown = c.get("/m/unknown-code", headers={"user-agent": "Slackbot-LinkExpanding 1.0"})
    check(p + "preview card is the same for live and unknown", r.status_code == r_unknown.status_code == 200
          and r.text == r_unknown.text)
    check(p + "preview card has no exchange", "hello" not in r.text and "expires" in r.text)
    browser = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8", "user-agent": "Mozilla/5.0 Chrome/130",
               "sec-fetch-dest": "document"}
    r = c.get(f"/m/{code3}", headers=browser)
    check(p + "browser gets the bridge page", r.status_code == 200 and "&lt;b&gt;hello&lt;/b&gt; &amp; bye" in r.text)
    check(p + "bridge page shows 36/24 and not for secrets", "36 hours" in r.text and "Not for secrets" in r.text)
    check(p + "bridge page: closes line before first reply", "unless someone replies" in r.text and "dissolve" not in r.text)
    check(p + "bridge page polls the spec read route, not /chain", "/api/melds/" in r.text and "/chain" not in r.text)
    check(p + "bridge page has nonce CSP", "nonce-" in r.headers.get("content-security-policy", ""))
    r = c.get("/m/unknown-code", headers=browser)
    check(p + "browser not-found is 404 with the not-live screen", r.status_code == 404
          and "This link isn't live. It may have closed, or the code is wrong." in r.text.replace("&#x27;", "'")
          and "Start a new bridge" in r.text and "doesn't exist" not in r.text)
    c.post(f"/api/melds/{code3}/resolve", json={"context": "r"})
    r = c.get(f"/m/{code3}", headers=browser)
    check(p + "bridge page: closes line after a reply", "if no one replies" in r.text)
    r = c.get(f"/m/{code3}", headers={"accept": "text/html", "user-agent": "curl/8"})
    check(p + "non-browser gets JSON on /m", r.json()["note"] == "<b>hello</b> & bye")

    # web UI
    r = c.get("/")
    t = r.text.replace("&#x27;", "'")
    check(p + "home: one note, create link, 36/24, not for secrets",
          r.text.count("<textarea") == 1 and "Create link" in r.text and "36 hours" in r.text
          and "24 hours" in r.text and "Not for secrets" in r.text)
    import meld_ui
    check(p + "home: hero text", meld_ui.HERO in t)
    check(p + "home: five trust bullets", all(b in t for b in meld_ui.TRUST_BULLETS) and len(meld_ui.TRUST_BULLETS) == 5)
    check(p + "home: no learn, pilot, 429 or too-many", not re.search(r"(?i)learn|pilot|429|too many", r.text))
    check(p + "home: maxlength 100000 and counter window 5,000", 'maxlength="100000"' in r.text and "WIN=5000" in r.text)
    check(p + "home: sends note, polls read route every 10s, open-until line",
          "JSON.stringify({note})" in r.text and "10000" in r.text and "for a first reply." in r.text)

    # MCP
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = r.json()["result"]["tools"]
    check(p + "MCP tools are exactly the three", [t["name"] for t in tools] == list(meld_spec.MCP_TOOLS))
    check(p + "MCP has no ttl or token argument",
          not any(k in t["inputSchema"]["properties"] for t in tools for k in ("ttl", "token", "owner_token")))

    def call(name, args):
        return c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": name, "arguments": args}}).json()["result"]

    res = call("meld_create", {"note": "from mcp"})
    check(p + "meld_create returns code/url/expires_at", set(res["structuredContent"]) == {"code", "url", "expires_at"})
    mcode = res["structuredContent"]["code"]
    res = call("meld_create", {"note": "x", "ttl": "1h"})
    check(p + "meld_create with ttl is an error", res.get("isError") is True and "ttl" in res["content"][0]["text"])
    res = call("meld_resolve", {"code": mcode, "context": "agent reply"})
    check(p + "meld_resolve appends and returns untrusted_content",
          res["structuredContent"]["untrusted_content"] == ["from mcp", "agent reply"])
    res = call("meld_read", {"code": mcode})
    check(p + "meld_read returns the thread", res["structuredContent"]["reply_count"] == 1)
    res = call("meld_read", {"code": "nope"})
    check(p + "meld_read unknown is the not-found error", res.get("isError") and "Meld not found" in res["content"][0]["text"])
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    check(p + "MCP initialize", r.json()["result"]["serverInfo"]["name"] == "meld")
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    check(p + "MCP notification 202", r.status_code == 202)

    # health and docs
    r = c.get("/health")
    check(p + "health is liveness only", r.json() == {"ok": True})
    for path in ("/llms.txt", "/agents.md", "/skill.md", "/trust.md", "/openapi.json",
                 "/.well-known/mcp.json", "/.well-known/agent.json"):
        r = c.get(path)
        check(p + f"{path} served with real origin", r.status_code == 200 and "{base}" not in r.text
              and ("https://meld.test" in r.text or path == "/trust.md"))
    r = c.options("/api/melds")
    check(p + "CORS preflight", r.status_code == 204 and r.headers.get("access-control-allow-origin") == "*")

    # timestamps
    j = c.get(f"/api/melds/{mcode}").json()
    stamps = [j["created_at"], j["expires_at"]] + [x["created_at"] for x in j["replies"]]
    check(p + "every timestamp is UTC ISO 8601 ms Z", all(meld_spec.TS_RE.match(s) for s in stamps), str(stamps))


def run_hooks() -> None:
    hooks = Hooks()
    c, clock, store, d1 = make("memory", hooks)
    code = create(c).json()["code"]
    c.post("/api/melds", json={"for": "x"}, headers={"x-meld-surface": "ui"})
    c.post(f"/api/melds/{code}/resolve", json={"context": "r"})
    c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "meld_create", "arguments": {"note": "m"}}})
    names = [(n, f.get("path") or f.get("tool")) for n, f in hooks.events]
    check("hooks see created/api, create_400/ui, replied, mcp_call, created/mcp",
          names == [("created", "api"), ("create_400", "ui"), ("replied", "api"),
                    ("mcp_call", "meld_create"), ("created", "mcp")], str(names))
    check("hooks get no note text", "design review" not in repr(hooks.events) and "context_missing" in repr(hooks.events))
    plain, _, _, _ = make("memory")
    boom, _, _, _ = make("memory", Hooks(boom=True))
    a, b = plain.post("/api/melds", json={"note": "n"}), boom.post("/api/melds", json={"note": "n"})
    check("a failing hook does not change the response", a.status_code == b.status_code == 200 and set(a.json()) == set(b.json()))
    a, b = plain.post("/api/melds", json={"note": "n", "ttl": "x"}), boom.post("/api/melds", json={"note": "n", "ttl": "x"})
    check("a failing hook does not change a 400", a.content == b.content)
    check("pilot keys are literals",
          pilot.keys_for("create_400", reason="context_missing", path="mcp") == ["create_400:context_missing", "create_400:context_missing:mcp"]
          and pilot.keys_for("created", path="evil'); DROP") == ["created"]
          and pilot.keys_for("create_400", reason="<script>", path="ui") == ["create_400:other", "create_400:other:ui"])
    d1 = FakeD1()
    fh = pilot.FunnelHooks(lambda: d1)
    asyncio.run(fh.event("created", path="mcp"))
    asyncio.run(fh.event("created", path="ui"))
    rows = dict(d1.conn.execute("SELECT event, n FROM funnel_events").fetchall())
    check("funnel counters write day/event/n only", rows == {"created": 2, "created:mcp": 1, "created:ui": 1}, str(rows))


def run_worker() -> None:
    import worker
    d1 = FakeD1()

    class Env:
        DB = d1
        SHARE_ORIGIN = "https://meld-staging.example"

    clock = Clock()
    meld_app.utcnow = clock

    async def asgi_call(method, path, body=None):
        sent = []
        payload = json.dumps(body).encode() if body is not None else b""
        scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
                 "headers": [(b"content-type", b"application/json"), (b"host", b"x")], "env": Env,
                 "scheme": "https", "server": ("x", 443), "client": ("1.2.3.4", 1), "root_path": "",
                 "http_version": "1.1"}
        done = False

        async def receive():
            nonlocal done
            if done:
                return {"type": "http.disconnect"}
            done = True
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(m):
            sent.append(m)
        await worker.app(scope, receive, send)
        status = sent[0]["status"]
        data = b"".join(m.get("body", b"") for m in sent[1:])
        return status, json.loads(data) if data else None

    s, j = asyncio.run(asgi_call("POST", "/api/melds", {"note": "hosted"}))
    check("worker: create on D1 with SHARE_ORIGIN", s == 200 and j["url"].startswith("https://meld-staging.example/m/"), str(j))
    code = j["code"]
    s, j = asyncio.run(asgi_call("GET", f"/api/melds/{code}"))
    check("worker: read", s == 200 and j["note"] == "hosted")
    check("worker: pilot counter written", d1.conn.execute("SELECT n FROM funnel_events WHERE event='created'").fetchone()[0] == 1)
    clock.advance(hours=37)
    n = asyncio.run(worker.scheduled_sweep(Env))
    check("worker: scheduled sweep deletes with no traffic", n == 1 and d1.count("melds") == 0)
    s, j = asyncio.run(asgi_call("GET", f"/api/melds/{code}"))
    check("worker: swept code is uniform 404", s == 404 and j == meld_spec.NOT_FOUND_BODY)


def run_spec_checks() -> None:
    text = (ROOT / "SPEC.md").read_text()
    usage = text.split("## Usage assumption", 1)
    check("SPEC.md has Usage assumption right after Purpose",
          len(usage) == 2 and "## 1. Purpose" in usage[0] and usage[1].lstrip().startswith("Each party keeps its own state.")
          and usage[1].index("## 2. Canonical spec") > 0)
    check_spec.errors.clear()
    block = check_spec.spec_block()
    check_spec.check_constants(dict(block, open_hours="35"))
    check("check_spec fails on drift", any("open_hours" in e for e in check_spec.errors))
    check_spec.errors.clear()
    check_spec._walk_schemas({"x": {"schema": {}}}, "#")
    check("check_spec fails on empty schema", bool(check_spec.errors))
    check_spec.errors.clear()
    check_spec._check_ts({"created_at": "2026-10-01T12:00:00+00:00"}, "x")
    check("check_spec fails on a mixed timestamp", bool(check_spec.errors))
    check_spec.errors.clear()
    check("check_spec passes on this tree", check_spec.main([]) == 0)
    banned = ("zero" + "-knowledge", "sk" + "_live", "wh" + "sec", "STRIPE" + "_", "price" + "_1")
    leaks = [p.name for p in ROOT.glob("*") if p.is_file() and p.suffix in (".py", ".md", ".txt", ".json", ".toml", ".sql", ".yml")
             and p.name != "test_server.py" and any(b in p.read_text(errors="ignore") for b in banned)]
    check("public tree has no payment or secret-shaped strings", not leaks, str(leaks))


if __name__ == "__main__":
    run_suite("memory")
    run_suite("d1")
    run_hooks()
    run_worker()
    run_spec_checks()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
