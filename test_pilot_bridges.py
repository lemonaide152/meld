"""Pilot timed bridges: required 3m/1hr/1d, no payment wall, no body unfurl."""
import asyncio
import datetime
import json
import sqlite3
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEPLOY = ROOT / "deploy"
sys.path.insert(0, str(DEPLOY))

workers_mod = types.ModuleType("workers")
asgi_mod = types.ModuleType("workers.asgi")
asgi_mod.asgi = lambda app, **kw: app
asgi_mod.entrypoint = lambda app, **kw: app
workers_mod.asgi = asgi_mod
sys.modules["workers"] = workers_mod
sys.modules["workers.asgi"] = asgi_mod

import worker  # noqa: E402

SCHEMA = (DEPLOY / "schema.sql").read_text() + "\n" + (DEPLOY / "schema_api.sql").read_text() + """
CREATE TABLE IF NOT EXISTS funnel_events (
  day TEXT NOT NULL, event TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, event));
CREATE TABLE IF NOT EXISTS free_counts (
  ip TEXT PRIMARY KEY, window_key TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ip_throttle (
  ip TEXT PRIMARY KEY, offense_count INTEGER NOT NULL DEFAULT 0,
  wall_hits INTEGER NOT NULL DEFAULT 0, hit_window TEXT, offense_window TEXT,
  banned_until TEXT, permanent INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
"""

passed = total = 0

def ok(name, cond, detail=""):
    global passed, total
    total += 1
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" — {detail}"))
    if cond:
        passed += 1


class Res:
    def __init__(self, changes):
        self.meta = {"changes": changes}
    def __getitem__(self, k):
        return self.meta[k]


class Stmt:
    def __init__(self, d1, sql):
        self.d1, self.sql, self._b = d1, sql, ()
    def bind(self, *a):
        self._b = a
        return self
    async def first(self):
        return self.d1.conn.execute(self.sql, self._b).fetchone()
    async def run(self):
        cur = self.d1.conn.execute(self.sql, self._b)
        return Res(max(cur.rowcount, 0))


class D1:
    def __init__(self, conn):
        self.conn = conn
    def prepare(self, sql):
        return Stmt(self, sql)
    def q(self, sql, *a):
        return self.conn.execute(sql, a).fetchall()


def fresh_db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return D1(conn)


class FakeRequest:
    def __init__(self, d1, body=None, headers=None, path="/api/melds"):
        self._body = json.dumps(body or {}).encode()
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in self.headers.items()],
            "client": ("127.0.0.1", 0),
            "server": ("test", 80),
            "env": types.SimpleNamespace(DB=d1),
        }
        self.url = types.SimpleNamespace(path=path, scheme="http")
        self.client = None
    async def json(self):
        return json.loads(self._body)


def run(coro):
    return asyncio.run(coro)


def unwrap(result):
    raw = result.body
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw).decode()
    return json.loads(raw)


async def create(d1, ip, body, headers=None):
    hdrs = {"x-forwarded-for": ip}
    if headers:
        hdrs.update(headers)
    req = FakeRequest(d1, body, hdrs)
    try:
        return ("ok", unwrap(await worker.create_meld(req)))
    except worker.HTTPException as e:
        return ("err", e.status_code, e.detail)


def test_ttl_required_and_enforced():
    print("ttl choices")
    d1 = fresh_db()
    missing = run(create(d1, "10.8.0.1", {"context": "hello"}))
    ok("missing ttl is 400", missing[0] == "err" and missing[1] == 400, str(missing))
    bad = run(create(d1, "10.8.0.1", {"context": "hello", "ttl": "1h"}))
    ok("1h alias rejected", bad[0] == "err" and bad[1] == 400, str(bad))
    other = run(create(d1, "10.8.0.1", {"context": "hello", "ttl": "7d"}))
    ok("7d rejected", other[0] == "err" and other[1] == 400, str(other))
    numeric = run(create(d1, "10.8.0.1", {"context": "hello", "ttl": 180}))
    ok("numeric ttl rejected", numeric[0] == "err" and numeric[1] == 400, str(numeric))

    expect = {"3m": 180, "1hr": 3600, "1d": 86400}
    for i, (ttl, seconds) in enumerate(expect.items()):
        before = datetime.datetime.now(datetime.timezone.utc)
        got = run(create(d1, f"10.8.1.{i}", {"context": f"body-{ttl}", "ttl": ttl}))
        ok(f"{ttl} admitted", got[0] == "ok" and got[1]["ttl"] == ttl, str(got)[:180])
        if got[0] != "ok":
            continue
        exp = datetime.datetime.fromisoformat(got[1]["expires_at"])
        delta = (exp - before).total_seconds()
        ok(f"{ttl} expiry within 5s of {seconds}", abs(delta - seconds) < 5, f"delta={delta}")
        token = got[1]["owner_token"]
        ok(f"{ttl} owner token is 256-bit hex",
           isinstance(token, str) and len(token) == 64 and all(c in "0123456789abcdef" for c in token),
           token[:12])
        ok(f"{ttl} capability code stays 12 chars", len(got[1]["code"]) == 12, got[1]["code"])


def test_resolve_keeps_chosen_ttl():
    print("resolve keeps ttl")
    d1 = fresh_db()
    got = run(create(d1, "10.8.2.1", {"context": "keep", "ttl": "1d"}))
    code = got[1]["code"]
    before = d1.q("SELECT expires_at FROM melds WHERE code=?", code)[0]["expires_at"]
    req = FakeRequest(d1, {"context": "reply"}, {"x-forwarded-for": "10.8.2.2"})
    run(worker.resolve_meld(code, req))
    after = d1.q("SELECT expires_at, resolved FROM melds WHERE code=?", code)[0]
    ok("resolve does not shrink expires_at", after["expires_at"] == before, f"{before} -> {after['expires_at']}")
    ok("resolve still records the reply", after["resolved"] == 1)


def test_mcp_selector_has_no_default():
    print("mcp selector")
    tool = next(t for t in worker._MCP_TOOLS if t["name"] == "meld_create")
    schema = tool["inputSchema"]
    ok("ttl is required", schema["required"] == ["context", "ttl"], str(schema["required"]))
    ok("ttl enum is exactly 3m/1hr/1d",
       schema["properties"]["ttl"]["enum"] == ["3m", "1hr", "1d"],
       str(schema["properties"]["ttl"]))
    ok("ttl schema has no default", "default" not in schema["properties"]["ttl"])
    card = next(t for t in worker.MCP_SERVER_CARD["tools"] if t["name"] == "meld_create")
    ok("server card requires ttl", card["inputSchema"]["required"] == ["context", "ttl"])
    d1 = fresh_db()
    req = FakeRequest(d1, {}, {"x-forwarded-for": "10.8.3.1"})
    try:
        run(worker._mcp_call_tool("meld_create", {"context": "no time"}, req))
        ok("mcp create without ttl fails", False, "returned a bridge")
    except RuntimeError as e:
        ok("mcp create without ttl names the three choices",
           "3m" in str(e) and "1hr" in str(e) and "1d" in str(e), str(e))
    made = run(worker._mcp_call_tool(
        "meld_create", {"context": "with time", "ttl": "3m"}, req))
    ok("mcp create with 3m returns the ttl", made.get("ttl") == "3m" and made.get("share_link"), str(made)[:180])


def test_share_preview_hides_body():
    print("share preview")
    secret = "SECRET-BODY-SHOULD-NOT-UNFURL"
    d1 = fresh_db()
    got = run(create(d1, "10.8.4.1", {"context": secret, "ttl": "3m"}))
    req = FakeRequest(d1, None, {"accept": "text/html", "x-forwarded-for": "10.8.4.2"}, path=f"/m/{got[1]['code']}")
    resp = run(worker.serve_meld(req, got[1]["code"]))
    html = bytes(resp.body).decode()
    ok("share html does not contain the meld body", secret not in html)
    ok("share title is expires-only", "<title>meld — this bridge expires</title>" in html)
    ok("og:title does not carry the body",
       'property="og:title" content="meld — this bridge expires"' in html and secret not in html)
    ok("og:description is generic",
       'property="og:description" content="This link expires. The exchange is not included in this preview."' in html)
    home = worker._render_page(share=False)
    ok("home is the task workspace",
       "A temporary resource to align context." in home
       and "Create the timed link" in home
       and "Create the bridge" in home
       and "3 minutes" in home and "1 hour" in home and "1 day" in home)
    lowered = home.lower()
    ok("home has no mint-next, email field, e2e, or pay upsell",
       "mint-next" not in lowered and "type=\"email\"" not in lowered
       and "end-to-end" not in lowered and "e2e" not in lowered
       and "stripe" not in lowered and "$3.33" not in lowered and "hop-line" not in lowered)


def test_receiver_job_and_soft_poll():
    print("receiver job and soft poll")
    html = worker.APP_HTML
    ok("opener has their own job rail",
       'id="receiver-rail"' in html and "Your job" in html
       and ">Read<" in html and ">Reply<" in html and ">Done<" in html)
    ok("creator rail stays a thin progress stepper",
       'id="creator-rail"' in html and ">Write<" in html and ">Time<" in html
       and ">Share<" in html and ">Reply<" in html)
    receiver = html.split("function receiver(", 1)[1].split("function resolved(", 1)[0]
    landing = html.split("function landing(", 1)[1].split("function timeBtn(", 1)[0]
    created = html.split("function showCreated(", 1)[1].split("function ttlLabel(", 1)[0]
    ok("reply page does not render the needs grid", "needsBlock" not in receiver and 'class="needs"' not in receiver)
    ok("landing does not teach with needs cards", "needsBlock" not in landing and 'class="needs"' not in landing)
    ok("share step is the bearer url without needs cards",
       "Pass this bearer URL" in created and "Copy link" in created
       and "Check for the reply" in created and 'class="needs"' not in created)
    ok("waiting page polls the existing view endpoint",
       "function startWatch(" in html and "scheduleWatch(10000)" in html
       and "/api/melds/" in html.split("async function checkResult", 1)[1])
    ok("rate-limit backoff stays on the same read",
       "scheduleWatch(45000)" in html and "visibilitychange" in html and "document.hidden" in html)
    ok("receiver page does not start the watch",
       "stopWatch();" in html.split("function meldPage(", 1)[1].split("function remainingText(", 1)[0]
       and "startWatch()" not in html.split("function meldPage(", 1)[1].split("function remainingText(", 1)[0])


def test_legacy_template_paths_404():
    print("legacy template paths")
    from fastapi import HTTPException
    for path in ("upgrade", "success", "cancel", "error", "app/templates/index.html", "templates/upgrade.html"):
        try:
            run(worker.serve_page(path))
            ok(f"{path} is not served", False, "returned a page")
        except HTTPException as e:
            ok(f"{path} is not served", e.status_code == 404, str(e.status_code))
    home = run(worker.serve_page("anything-else"))
    body = bytes(home.body).decode()
    ok("unknown product paths still get the workspace",
       "A temporary resource to align context." in body and "Create the bridge" in body)
    upgrade = run(worker.upgrade_md())
    text = bytes(upgrade.body).decode()
    ok("upgrade.md still served", "pilot" in text.lower() and "$3.33" not in text)


def test_mobile_first_human_ui():
    print("mobile-first human ui")
    html = worker.APP_HTML
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    base, _, enhanced = css.partition("@media")
    flat_base = "".join(base.split())
    ok("phone base has no max-width breakpoint",
       "@media (max-width" not in css and "@media(max-width" not in css)
    ok("phone base stacks the page and keeps three bridge times in one row",
       ".layout{display:grid;grid-template-columns:1fr" in flat_base
       and ".times{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))" in flat_base)
    ok("phone base hides the hero visual", ".hero-visual{display:none}" in flat_base)
    ok("wider screens may show the card beside the what-line",
       "min-width:50rem" in enhanced and 'url("/og.png")' in enhanced)
    ok("primary controls declare a 44px tap target",
       "min-height:44px" in css)
    ok("waiting still pauses while the tab is hidden",
       "visibilitychange" in html and "document.hidden" in html
       and "clearTimeout(watchTimer)" in html and "scheduleWatch(10000)" in html)
    ok("reply flow keeps the full trust bullets and create keeps the three times",
       "Read this before you put text on the bridge." in html
       and "Not for secrets, credentials, or regulated data." in html
       and "The host can read it while it is live." in html
       and "3 minutes" in html and "1 hour" in html and "1 day" in html)
    landing = html.split("function landing(", 1)[1].split("function timeBtn(", 1)[0]
    ctx_at = landing.find('id="ctx"')
    times_at = landing.find('class="times"')
    create_at = landing.find('id="create"')
    trust_at = landing.find('class="trust-line"')
    ok("pour order is textarea, bridge time, create, then one trust line",
       0 < ctx_at < times_at < create_at < trust_at, f"{ctx_at, times_at, create_at, trust_at}")
    ok("landing does not open with the four-bullet trust wall", "trustBlock()" not in landing)
    ok("landing focuses the context textarea", "autofocus" in landing and "ctx.focus" in landing)
    ok("trust one-liner points at /trust",
       "Anyone with the link can read it while live · Not for secrets · " in landing
       and 'href="/trust"' in landing)


def test_agent_install_surface():
    print("agent install docs")
    import hashlib
    import agents_content

    agents_res = run(worker.agents_md())
    skill_res = run(worker.skill_md_route())
    root_res = run(worker.agents_root_md())
    page_res = run(worker.agents_page())
    agents = bytes(agents_res.body).decode()
    skill = bytes(skill_res.body).decode()
    root = bytes(root_res.body).decode()
    page = bytes(page_res.body).decode()
    ok("agents.md 200", agents_res.status_code == 200, str(agents_res.status_code))
    ok("skill.md 200", skill_res.status_code == 200, str(skill_res.status_code))
    ok("AGENTS.md 200", root_res.status_code == 200, str(root_res.status_code))
    ok("/agents html 200", page_res.status_code == 200, str(page_res.status_code))

    skill_file = (ROOT / "recipes" / "SKILL.md").read_text()
    root_file = (DEPLOY / "AGENTS-root.md").read_text()
    ok("skill.md matches recipes/SKILL.md", skill == skill_file == worker.SKILL_MD)
    ok("AGENTS.md matches deploy/AGENTS-root.md", root == root_file == worker.AGENTS_ROOT_MD)
    ok("agents.md is agents_content.AGENTS_MD", agents == agents_content.AGENTS_MD == worker.AGENTS_MD)
    paste = "fetch https://meld.mergeinc.workers.dev/agents.md and set me up for meld"
    ok("/agents html shows the same install guide", paste in page and "Not for secrets" in page)

    locked = (
        "Not for secrets",
        "3m",
        "1hr",
        "1d",
        "https://meld.mergeinc.workers.dev/mcp",
        "Human → agent",
        "Agent → agent",
        "There is no default",
        "Pilot creates are free",
    )
    banned = (
        "mint-next",
        "mint next",
        "human-to-human",
        "human→human",
        "human to human",
        "x402",
    )
    for label, text in (("agents.md", agents), ("skill.md", skill), ("AGENTS.md", root)):
        for phrase in locked:
            ok(f"{label} has {phrase}", phrase in text, phrase)
        low = text.lower()
        for bad in banned:
            ok(f"{label} omits {bad}", bad not in low, bad)

    for link in ("/recipes.md", "/openapi.json", "/trust.md", "/llms.txt", "/skill.md"):
        ok(f"agents.md links {link}", link in agents, link)
    for client in ("Cursor", "Claude Code", "Codex", "streamable-http"):
        ok(f"agents.md has {client}", client in agents, client)
    ok("agents.md paste prompt", paste in agents)

    desc = skill.split("description:", 1)[1].split("\n", 1)[0].strip()
    entry = worker.SKILLS_INDEX["skills"][0]
    ok("skill index description", entry["description"] == desc, entry["description"])
    digest = "sha256:" + hashlib.sha256(skill.encode()).hexdigest()
    ok("skill index digest", entry["digest"] == digest, entry["digest"])


def test_card_homepage():
    print("card homepage")
    html = worker.APP_HTML
    what = (
        "meld is a temporary resource that aligns context between you and an agent, "
        "or between two agents — timed link, then it dies. Not for secrets."
    )
    ok("what-it-is is the locked line", what in html)
    ok("hero does not repeat the explanation stack",
       'class="sub"' not in html and 'class="secret"' not in html
       and "<h1>A temporary resource to align context.</h1>" not in html)
    ok("document title stays the card claim",
       'document.title = "A temporary resource to align context."' in html)
    css = html.split("<style>", 1)[1].split("</style>", 1)[0].lower()
    ok("accent is card purple", "#8b5cf6" in css)
    ok("background is near-black", "#05050a" in css)
    ok("mint accent is gone", "#7ee0c6" not in css)
    ok("wide hero reuses the og card", 'url("/og.png")' in html)
    header = html.split("<header", 1)[1].split("</header>", 1)[0]
    footer = html.split("<footer", 1)[1].split("</footer>", 1)[0]
    ok("primary nav is new bridge and trust",
       "New bridge" in header and 'href="/trust"' in header
       and "Agent API" not in header and "llms.txt" not in header)
    ok("agent api and llms.txt sit in the footer",
       "Agent API" in footer and 'href="/llms.txt"' in footer)
    lowered = html.lower()
    ok("homepage does not pitch another person", "another person" not in lowered)
    ok("what-it-is does not use they open the link", "they open the link" not in lowered)
    ok("host-readable warning stays off the one-liner",
       "host" not in what.lower() and "The host can read it while it is live." in html)


def test_shipped_docs_drop_mint_next():
    print("docs")
    blob = "\n".join([
        worker.AGENTS_MD, worker.AGENTS_ROOT_MD, worker.LLMS_TXT, worker.SKILL_MD,
        worker.RECIPES_MD, worker.TRUST_MD, worker.UPGRADE_MD, worker.TRUST_HTML,
        worker.APP_HTML,
    ]).lower()
    ok("worker product surfaces have no mint-next", "mint-next" not in blob and "mint next" not in blob)
    ok("upgrade doc does not sell a bridge", "$3.33" not in worker.UPGRADE_MD and "checkout" not in worker.UPGRADE_MD.lower())
    ok("pricing header is pilot-free", worker.PRICING_HEADER == "pilot-free")


for t in (test_ttl_required_and_enforced, test_resolve_keeps_chosen_ttl,
          test_mcp_selector_has_no_default, test_share_preview_hides_body,
          test_receiver_job_and_soft_poll, test_mobile_first_human_ui,
          test_card_homepage, test_legacy_template_paths_404,
          test_agent_install_surface,
          test_shipped_docs_drop_mint_next):
    t()

print(f"\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)
