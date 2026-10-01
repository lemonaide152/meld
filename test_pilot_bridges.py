"""Pilot timed bridges: fixed 1 hour, mint-next is a new hop, no payment wall."""
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
    async def all(self):
        return list(self.d1.conn.execute(self.sql, self._b).fetchall())


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
    print("ttl is one hour")
    d1 = fresh_db()
    before = datetime.datetime.now(datetime.timezone.utc)
    missing = run(create(d1, "10.8.0.1", {"context": "hello"}))
    ok("missing ttl defaults to 1hr", missing[0] == "ok" and missing[1]["ttl"] == "1hr", str(missing)[:180])
    if missing[0] == "ok":
        delta = (datetime.datetime.fromisoformat(missing[1]["expires_at"]) - before).total_seconds()
        ok("missing ttl expires in about 1 hour", abs(delta - 3600) < 5, f"delta={delta}")
    for label, value in (("1h alias", "1h"), ("7d", "7d"), ("numeric", 180), ("3m", "3m"), ("1d", "1d")):
        bad = run(create(d1, "10.8.0.1", {"context": "hello", "ttl": value}))
        ok(f"{label} rejected", bad[0] == "err" and bad[1] == 400 and "1 hour" in str(bad[2]), str(bad))
    before = datetime.datetime.now(datetime.timezone.utc)
    got = run(create(d1, "10.8.1.1", {"context": "body-1hr", "ttl": "1hr"}))
    ok("1hr admitted", got[0] == "ok" and got[1]["ttl"] == "1hr", str(got)[:180])
    if got[0] == "ok":
        exp = datetime.datetime.fromisoformat(got[1]["expires_at"])
        delta = (exp - before).total_seconds()
        ok("1hr expiry within 5s of 3600", abs(delta - 3600) < 5, f"delta={delta}")
        token = got[1]["owner_token"]
        ok("owner token is 256-bit hex",
           isinstance(token, str) and len(token) == 64 and all(c in "0123456789abcdef" for c in token),
           token[:12])
        ok("capability code stays 12 chars", len(got[1]["code"]) == 12, got[1]["code"])


def test_resolve_keeps_chosen_ttl():
    print("resolve keeps ttl")
    d1 = fresh_db()
    got = run(create(d1, "10.8.2.1", {"context": "keep", "ttl": "1hr"}))
    code = got[1]["code"]
    before = d1.q("SELECT expires_at FROM melds WHERE code=?", code)[0]["expires_at"]
    req = FakeRequest(d1, {"context": "reply"}, {"x-forwarded-for": "10.8.2.2"})
    run(worker.resolve_meld(code, req))
    after = d1.q("SELECT expires_at, resolved FROM melds WHERE code=?", code)[0]
    ok("resolve does not shrink expires_at", after["expires_at"] == before, f"{before} -> {after['expires_at']}")
    ok("resolve still records the reply", after["resolved"] == 1)


def test_mint_next_is_a_new_hour():
    print("mint-next")
    d1 = fresh_db()
    root = run(create(d1, "10.9.0.1", {"context": "root-body"}))
    ok("root created", root[0] == "ok", str(root)[:160])
    if root[0] != "ok":
        return
    root_code = root[1]["code"]
    root_exp = root[1]["expires_at"]
    before = datetime.datetime.now(datetime.timezone.utc)
    hop = run(create(d1, "10.9.0.2", {"context": "hop-body", "ttl": "1hr", "prev_code": root_code}))
    ok("mint-next creates a bearer", hop[0] == "ok" and hop[1]["code"] != root_code, str(hop)[:180])
    if hop[0] != "ok":
        return
    delta = (datetime.datetime.fromisoformat(hop[1]["expires_at"]) - before).total_seconds()
    ok("hop has its own hour", abs(delta - 3600) < 5, f"delta={delta}")
    parent = d1.q("SELECT expires_at, thread_id FROM melds WHERE code=?", root_code)[0]
    ok("parent expiry is unchanged", parent["expires_at"] == root_exp, parent["expires_at"])
    ok("hop shares the root thread", hop[1]["thread_id"] == parent["thread_id"] == root_code, str(hop[1]["thread_id"]))
    ok("hop records prev_code", hop[1]["prev_code"] == root_code)
    req = FakeRequest(d1, None, {"x-forwarded-for": "10.9.0.3"})
    chain = run(worker.get_chain(hop[1]["code"], req))
    texts = " ".join((n["context_a"] or "") for n in chain["nodes"])
    ok("live chain includes both hops", "root-body" in texts and "hop-body" in texts, texts)
    ok("chain marks each hop 1hr", all(n["ttl"] == "1hr" for n in chain["nodes"]))
    past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)).isoformat()
    d1.conn.execute("UPDATE melds SET expires_at=? WHERE code=?", (past, root_code))
    chain2 = run(worker.get_chain(hop[1]["code"], req))
    texts2 = " ".join((n["context_a"] or "") for n in chain2["nodes"])
    ok("expired hop is not on the chain", "root-body" not in texts2 and "hop-body" in texts2, texts2)
    ok("expired hop row is deleted", d1.q("SELECT code FROM melds WHERE code=?", root_code) == [])
    dead = run(create(d1, "10.9.0.4", {"context": "should-not-store", "prev_code": root_code}))
    ok("mint from a gone hop is 404", dead[0] == "err" and dead[1] == 404, str(dead))
    missing = run(create(d1, "10.9.0.5", {"context": "should-not-store", "prev_code": "missingcode1"}))
    ok("mint from a missing hop is 404", missing[0] == "err" and missing[1] == 404, str(missing))
    ok("failed hops store no body", d1.q("SELECT code FROM melds WHERE context_a=?", "should-not-store") == [])
    fresh = fresh_db()
    live = run(create(fresh, "10.9.1.1", {"context": "still-live"}))
    code = live[1]["code"]
    past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)).isoformat()
    fresh.conn.execute("UPDATE melds SET expires_at=? WHERE code=?", (past, code))
    try:
        run(worker.get_chain(code, FakeRequest(fresh, None, {"x-forwarded-for": "10.9.1.3"})))
        ok("expired current link is 410", False, "chain returned")
    except worker.HTTPException as e:
        ok("expired current link is 410", e.status_code == 410 and "still-live" not in str(e.detail), str(e.detail))
    ok("expired chain read deletes the row", fresh.q("SELECT code FROM melds WHERE code=?", code) == [])
    again = run(create(fresh, "10.9.1.4", {"context": "again-live"}))
    again_code = again[1]["code"]
    fresh.conn.execute("UPDATE melds SET expires_at=? WHERE code=?", (past, again_code))
    expired = run(create(fresh, "10.9.1.2", {"context": "should-not-store", "prev_code": again_code}))
    ok("mint from an expired hop is 410", expired[0] == "err" and expired[1] == 410, str(expired))
    ok("410 does not keep the expired row", fresh.q("SELECT code FROM melds WHERE code=?", again_code) == [])
    ok("410 does not create the next body", fresh.q("SELECT code FROM melds WHERE context_a=?", "should-not-store") == [])


def test_mcp_selector_is_one_hour():
    print("mcp selector")
    tool = next(t for t in worker._MCP_TOOLS if t["name"] == "meld_create")
    schema = tool["inputSchema"]
    ok("ttl is optional", schema["required"] == ["context"], str(schema["required"]))
    ok("ttl enum is exactly 1hr", schema["properties"]["ttl"]["enum"] == ["1hr"], str(schema["properties"]["ttl"]))
    ok("ttl schema defaults to 1hr", schema["properties"]["ttl"].get("default") == "1hr")
    ok("prev_code is the mint-next field", "prev_code" in schema["properties"])
    card = next(t for t in worker.MCP_SERVER_CARD["tools"] if t["name"] == "meld_create")
    ok("server card requires context only", card["inputSchema"]["required"] == ["context"])
    d1 = fresh_db()
    req = FakeRequest(d1, {}, {"x-forwarded-for": "10.8.3.1"})
    made = run(worker._mcp_call_tool("meld_create", {"context": "no time"}, req))
    ok("mcp create without ttl is 1hr", made.get("ttl") == "1hr" and made.get("share_link"), str(made)[:180])
    try:
        run(worker._mcp_call_tool("meld_create", {"context": "bad time", "ttl": "3m"}, req))
        ok("mcp create with another ttl fails", False, "returned a bridge")
    except RuntimeError as e:
        ok("mcp create with another ttl says 1 hour", "1 hour" in str(e) and "3m" not in str(e), str(e))


def test_share_preview_hides_body():
    print("share preview")
    secret = "SECRET-BODY-SHOULD-NOT-UNFURL"
    d1 = fresh_db()
    got = run(create(d1, "10.8.4.1", {"context": secret, "ttl": "1hr"}))
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
       and "This link lives 1 hour, then it dies." in home
       and "3 minutes" not in home and "1 day" not in home)
    lowered = home.lower()
    ok("home offers mint-next as a new link and no pay upsell",
       "mint next" in lowered and "not an extend" in lowered
       and "type=\"email\"" not in lowered
       and "end-to-end" not in lowered and "e2e" not in lowered
       and "stripe" not in lowered and "$3.33" not in lowered and "hop-line" not in lowered)


def test_receiver_job_and_soft_poll():
    print("receiver job and soft poll")
    html = worker.APP_HTML
    ok("opener has their own job rail",
       'id="receiver-rail"' in html and "Your job" in html
       and ">Read<" in html and ">Reply<" in html and ">Done<" in html)
    ok("creator rail stays a thin progress stepper",
       'id="creator-rail"' in html and ">Write<" in html and ">Share<" in html
       and ">Reply<" in html and ">Next<" in html and ">Time<" not in html)
    receiver = html.split("function receiver(", 1)[1].split("function resolved(", 1)[0]
    landing = html.split("function landing(", 1)[1].split("async function createBridge(", 1)[0]
    created = html.split("function showCreated(", 1)[1].split("async function copyLink(", 1)[0]
    ok("reply page does not render the needs grid", "needsBlock" not in receiver and 'class="needs"' not in receiver)
    ok("landing does not teach with needs cards", "needsBlock" not in landing and 'class="needs"' not in landing)
    ok("share step is the bearer url without needs cards",
       "Pass this bearer URL" in created and "Copy link" in created
       and "Check for the reply" in created and 'class="needs"' not in created)
    check = html.split("async function checkResult", 1)[1].split("function dissolved(", 1)[0]
    paint = html.split("async function paintChain", 1)[1].split("async function mintNext", 1)[0]
    ok("waiting page polls the live chain",
       "function startWatch(" in html and "scheduleWatch(10000)" in html
       and "paintChain()" in check and "/api/melds/" in paint and "/chain" in paint)
    ok("mint-next posts prev_code on a new create",
       "Mint next" in html and "prev_code" in html and 'ttl: "1hr"' in html)
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
    ok("upgrade.md still served",
       "402" in text and "X402_PAY_TO" in text and "1hr" in text
       and "3m" not in text and "not for secrets" in text.lower(),
       text[:180])


def test_mobile_first_human_ui():
    print("mobile-first human ui")
    html = worker.APP_HTML
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    base, _, enhanced = css.partition("@media")
    flat_base = "".join(base.split())
    ok("phone base has no max-width breakpoint",
       "@media (max-width" not in css and "@media(max-width" not in css)
    ok("phone base stacks the page and has no three-up time picker",
       ".layout{display:grid;grid-template-columns:1fr" in flat_base
       and ".times{" not in flat_base and 'class="times"' not in html
       and 'role="radiogroup"' not in html)
    ok("phone base hides the hero visual", ".hero-visual{display:none}" in flat_base)
    ok("wider screens may show the card beside the what-line",
       "min-width:50rem" in enhanced and 'url("/og.png")' in enhanced)
    ok("primary controls declare a 44px tap target",
       "min-height:44px" in css)
    ok("waiting still pauses while the tab is hidden",
       "visibilitychange" in html and "document.hidden" in html
       and "clearTimeout(watchTimer)" in html and "scheduleWatch(10000)" in html)
    ok("reply flow keeps the full trust bullets and one fixed hour",
       "Read this before you put text on the bridge." in html
       and "Not for secrets, credentials, or regulated data." in html
       and "The host can read it while it is live." in html
       and "This link lives 1 hour, then it dies." in html
       and "3 minutes" not in html and "1 day" not in html)
    landing = html.split("function landing(", 1)[1].split("async function createBridge(", 1)[0]
    ctx_at = landing.find('id="ctx"')
    life_at = landing.find('class="life"')
    create_at = landing.find('id="create"')
    trust_at = landing.find('class="trust-line"')
    ok("pour order is textarea, one-hour line, create, then one trust line",
       0 < ctx_at < life_at < create_at < trust_at, f"{ctx_at, life_at, create_at, trust_at}")
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
        "1 hour",
        "1hr",
        "not an extend",
        "https://meld.mergeinc.workers.dev/mcp",
        "Human → agent",
        "Agent → agent",
        "Pilot creates are free",
        "mint-next",
    )
    banned = (
        "human-to-human",
        "human→human",
        "human to human",
    )
    for label, text in (("agents.md", agents), ("skill.md", skill), ("AGENTS.md", root)):
        for phrase in locked:
            found = phrase.lower() in text.lower() if phrase == "mint-next" else phrase in text
            ok(f"{label} has {phrase}", found, phrase)
        low = text.lower()
        for bad in banned:
            ok(f"{label} omits {bad}", bad not in low, bad)
        # install surfaces stay free of payment jargon except agents.md Paying section
        if label != "agents.md":
            ok(f"{label} omits x402", "x402" not in low, "x402")
        else:
            ok(f"{label} documents x402 wall", "402" in text and "x402" in low, text[-80:])
        ok(f"{label} does not offer 3m", "3m" not in text)
        ok(f"{label} does not offer 1d", "1d" not in text)

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


def test_shipped_docs_describe_mint_next():
    print("docs")
    import re
    blob = "\n".join([
        worker.AGENTS_MD, worker.AGENTS_ROOT_MD, worker.LLMS_TXT, worker.SKILL_MD,
        worker.RECIPES_MD, worker.TRUST_MD, worker.UPGRADE_MD, worker.TRUST_HTML,
        worker.APP_HTML,
    ])
    lowered = blob.lower()
    readable = re.sub(r"data:image/png;base64,[A-Za-z0-9+/=]+", "", lowered)
    ok("product surfaces describe mint-next", "mint-next" in lowered or "mint next" in lowered)
    ok("product surfaces say a hop is not an extend", "not an extend" in lowered)
    ok("product copy does not offer 3m", "3m" not in readable)
    ok("product copy does not offer 1d", "1d" not in readable)
    ok("upgrade doc names the x402 secrets",
       "X402_PAY_TO" in worker.UPGRADE_MD and "X402_FACILITATOR_URL" in worker.UPGRADE_MD
       and "no subscription" in worker.UPGRADE_MD.lower())
    ok("pricing header is the agent x402 wall",
       worker.PRICING_HEADER == "humans-free; agents-key-or-quota-or-x402")


for t in (test_ttl_required_and_enforced, test_resolve_keeps_chosen_ttl,
          test_mint_next_is_a_new_hour,
          test_mcp_selector_is_one_hour, test_share_preview_hides_body,
          test_receiver_job_and_soft_poll, test_mobile_first_human_ui,
          test_card_homepage, test_legacy_template_paths_404,
          test_agent_install_surface,
          test_shipped_docs_describe_mint_next):
    t()

print(f"\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)
