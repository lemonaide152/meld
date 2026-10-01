"""Open Graph / Twitter cards, and the /m/{code} unfurl gate.

Marketing pages advertise the working-context handoff.
Capability URLs must not put the meld body into card tags or preview HTML.
"""
import re
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

import preview_meta  # noqa: E402
from og_png import PNG as OG_PNG  # noqa: E402
import worker  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

passed = total = 0
SECRET = "UNFURL-SECRET-context-a-plaintext-9f3c2a"
CODE = "zzcanarycode99"


def ok(name, cond, detail=""):
    global passed, total
    total += 1
    if cond:
        passed += 1
        print(f"  OK {name}")
    else:
        print(f"  FAIL {name} — {detail}")


def meta_blob(html: str) -> str:
    head = html.split("</head>", 1)[0]
    tags = re.findall(r"<title>.*?</title>|<meta\b[^>]*>", head, flags=re.I | re.S)
    return "\n".join(tags)


def content_of(html: str, attr: str, name: str) -> str:
    """Return the content attribute of a meta tag identified by property or name."""
    pattern = (
        rf'<meta\b[^>]*\b{attr}="{re.escape(name)}"[^>]*\bcontent="([^"]*)"'
        rf'|<meta\b[^>]*\bcontent="([^"]*)"[^>]*\b{attr}="{re.escape(name)}"'
    )
    m = re.search(pattern, html, flags=re.I)
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


def assert_clean(label, text):
    low = text.lower()
    for bad in preview_meta.FORBIDDEN_CARD_CLAIMS:
        ok(f"{label} omits {bad}", bad not in low, bad)


print("og asset")
file_png = (DEPLOY / "static" / "og.png").read_bytes()
ok("png file matches worker bytes", file_png == OG_PNG, f"{len(file_png)} vs {len(OG_PNG)}")
ok("png signature", file_png.startswith(b"\x89PNG\r\n\x1a\n"))
ok("png IHDR", file_png[12:16] == b"IHDR")
width = int.from_bytes(file_png[16:20], "big")
height = int.from_bytes(file_png[20:24], "big")
ok("png 1200x630", (width, height) == (1200, 630), f"{width}x{height}")

print("card copy")
ok(
    "marketing title is the card job",
    preview_meta.MARKETING_TITLE == "A temporary resource to align context.",
)
for phrase in (
    "A temporary resource to align context.",
    "Pick 3 minutes, 1 hour, or 1 day.",
    "When the clock ends, the link dies.",
    "Not for secrets.",
):
    ok(f"marketing description has {phrase}", phrase in preview_meta.MARKETING_DESCRIPTION)
low_desc = preview_meta.MARKETING_DESCRIPTION.lower()
ok("marketing description omits host-readable", "host-readable" not in low_desc and "host can read" not in low_desc)
ok("marketing title omits host-readable", "host" not in preview_meta.MARKETING_TITLE.lower())
ok("image alt matches the four lines", preview_meta.OG_IMAGE_ALT == preview_meta.MARKETING_DESCRIPTION)
ok("capability title", preview_meta.CAPABILITY_TITLE == "meld — this bridge expires")
ok(
    "capability description",
    preview_meta.CAPABILITY_DESCRIPTION
    == "This link expires. The exchange is not included in this preview.",
)
assert_clean("marketing title", preview_meta.MARKETING_TITLE)
assert_clean("marketing description", preview_meta.MARKETING_DESCRIPTION)
assert_clean("capability title", preview_meta.CAPABILITY_TITLE)
assert_clean("capability description", preview_meta.CAPABILITY_DESCRIPTION)
assert_clean("image alt", preview_meta.OG_IMAGE_ALT)

preview = preview_meta.capability_preview_document()
assert_clean("preview document", preview)
ok("preview has no script", "<script" not in preview.lower())
ok("preview has no context_a", "context_a" not in preview)
ok("preview has generic og title", content_of(preview, "property", "og:title") == preview_meta.CAPABILITY_TITLE)
ok(
    "preview has generic og description",
    content_of(preview, "property", "og:description") == preview_meta.CAPABILITY_DESCRIPTION,
)
ok("preview has no og:image", 'property="og:image"' not in preview)
ok("preview twitter card is summary", content_of(preview, "name", "twitter:card") == "summary")
ok("preview has no twitter:image", 'name="twitter:image"' not in preview)

print("docs")
trust = (ROOT / "TRUST.md").read_text()
readme = (ROOT / "README.md").read_text()
for label, text in (("TRUST.md", trust), ("README.md", readme), ("TRUST_MD", worker.TRUST_MD)):
    ok(f"{label} notes generic card", "generic card" in text)
    ok(f"{label} names the expires-only title", "this bridge expires" in text)
assert_clean("TRUST.md link previews", trust.split("## Link previews", 1)[-1])
assert_clean("TRUST_MD link previews", worker.TRUST_MD.split("## Link previews", 1)[-1])

print("routes")
client = TestClient(worker.app, raise_server_exceptions=False)
db_calls = []


def tracking_db(request):
    db_calls.append(request.url.path)
    raise RuntimeError("capability preview must not read the meld")


worker.db = tracking_db

home = client.get("/")
ok("home 200", home.status_code == 200, str(home.status_code))
home_meta = meta_blob(home.text)
ok("home og:title", content_of(home_meta, "property", "og:title") == preview_meta.MARKETING_TITLE)
ok(
    "home og:description",
    content_of(home_meta, "property", "og:description") == preview_meta.MARKETING_DESCRIPTION,
)
ok("home twitter:title", content_of(home_meta, "name", "twitter:title") == preview_meta.MARKETING_TITLE)
ok(
    "home twitter:description",
    content_of(home_meta, "name", "twitter:description") == preview_meta.MARKETING_DESCRIPTION,
)
ok("home twitter card", content_of(home_meta, "name", "twitter:card") == "summary_large_image")
ok("home og:image", content_of(home_meta, "property", "og:image") == preview_meta.OG_IMAGE_URL)
ok("home twitter:image", content_of(home_meta, "name", "twitter:image") == preview_meta.OG_IMAGE_URL)
ok("home title is the locked line", f"<title>{preview_meta.MARKETING_TITLE}</title>" in home.text)
assert_clean("home meta", home_meta)
ok("home meta slot filled", preview_meta.META_SLOT not in home.text)
ok("home meta has no mint-next", "mint-next" not in home_meta.lower() and "mint next" not in home_meta.lower())

for path in ("/agents", "/trust"):
    page = client.get(path)
    ok(f"{path} 200", page.status_code == 200, str(page.status_code))
    blob = meta_blob(page.text)
    ok(f"{path} og:title", content_of(blob, "property", "og:title") == preview_meta.MARKETING_TITLE)
    ok(
        f"{path} og:description",
        content_of(blob, "property", "og:description") == preview_meta.MARKETING_DESCRIPTION,
    )
    ok(f"{path} og:image", content_of(blob, "property", "og:image") == preview_meta.OG_IMAGE_URL)
    ok(f"{path} twitter:image", content_of(blob, "name", "twitter:image") == preview_meta.OG_IMAGE_URL)
    assert_clean(f"{path} meta", blob)

image = client.get("/og.png")
ok("og.png 200", image.status_code == 200, str(image.status_code))
ok("og.png content-type", image.headers.get("content-type", "").startswith("image/png"), image.headers.get("content-type"))
cache = image.headers.get("cache-control", "")
ok("og.png cache", "public" in cache and "max-age=" in cache, cache)
ok("og.png body", image.content == OG_PNG, str(len(image.content)))

human = client.get(f"/m/{CODE}", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"})
ok("human meld 200", human.status_code == 200, str(human.status_code))
ok("human meld did not read db", db_calls == [], str(db_calls))
human_meta = meta_blob(human.text)
ok("human meld og:title generic", content_of(human_meta, "property", "og:title") == preview_meta.CAPABILITY_TITLE)
ok(
    "human meld og:description generic",
    content_of(human_meta, "property", "og:description") == preview_meta.CAPABILITY_DESCRIPTION,
)
ok(
    "human meld twitter:description generic",
    content_of(human_meta, "name", "twitter:description") == preview_meta.CAPABILITY_DESCRIPTION,
)
ok("human meld no og:image", 'property="og:image"' not in human_meta)
ok("human meld no twitter:image", 'name="twitter:image"' not in human_meta)
ok("human meld meta has no context_a", "context_a" not in human_meta and "context_b" not in human_meta)
ok("human meld meta has no secret", SECRET not in human_meta)
ok("human meld html has no secret", SECRET not in human.text)
ok("human meld html does not echo code", CODE not in human.text)
ok("human meld title tag is generic", "<title>meld — this bridge expires</title>" in human.text)
assignments = re.findall(r"document\.title\s*=\s*(['\"][^'\"]*['\"])", human.text)
ok(
    "human title assignments stay generic",
    bool(assignments) and all("context_" not in item and SECRET not in item for item in assignments),
    str(assignments),
)
ok("spa still boots for the holder", "function meldPage" in human.text)
assert_clean("human meld meta", human_meta)

for ua in ("Slackbot-LinkExpanding 1.0", "Twitterbot/1.0", "Discordbot/2.0"):
    db_calls.clear()
    bot = client.get(
        f"/m/{CODE}",
        headers={
            "Accept": "application/json, text/html",
            "User-Agent": ua,
        },
    )
    ok(f"{ua} 200", bot.status_code == 200, str(bot.status_code))
    ok(f"{ua} did not read db", db_calls == [], str(db_calls))
    ok(f"{ua} is html", "text/html" in bot.headers.get("content-type", ""), bot.headers.get("content-type"))
    ok(f"{ua} generic title", content_of(bot.text, "property", "og:title") == preview_meta.CAPABILITY_TITLE)
    ok(
        f"{ua} generic description",
        content_of(bot.text, "property", "og:description") == preview_meta.CAPABILITY_DESCRIPTION,
    )
    ok(f"{ua} no script", "<script" not in bot.text.lower())
    ok(f"{ua} no context_a", "context_a" not in bot.text and "context_b" not in bot.text)
    ok(f"{ua} no secret", SECRET not in bot.text)
    ok(f"{ua} no code echo", CODE not in bot.text)
    ok(f"{ua} no marketing image", 'property="og:image"' not in bot.text)
    assert_clean(ua, bot.text)

db_calls.clear()
api = client.get(f"/m/{CODE}", headers={"Accept": "application/json", "User-Agent": "curl/8.0"})
ok("json accept still reaches the meld read", api.status_code == 500 and db_calls == [f"/m/{CODE}"], f"{api.status_code} {db_calls}")

print(f"\n{passed}/{total} passed")
raise SystemExit(0 if passed == total else 1)
