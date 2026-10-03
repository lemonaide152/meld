"""Base-case server: one bridge, kept replies, silence timer, public-tree hygiene."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import server

ROOT = Path(__file__).resolve().parent
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

# Assembled so this file does not contain prod ids or secret-shaped literals.
BANNED = (
    "zero" + "-knowledge",
    "zero " + "knowledge",
    "sk" + "_",
    "wh" + "sec",
    "FAC" + "ILITATOR",
    "pay" + "To",
    "STRIPE" + "_",
    "x" + "402",
    "price" + "_",
)


def fail(message: str) -> None:
    print(f"FAIL {message}")
    raise SystemExit(1)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok {name}")
        return
    fail(f"{name} {detail}")


def reset(clock: dict) -> None:
    server._melds.clear()
    server._tombstones.clear()
    server._now = lambda: clock["t"]


def test_create_resolve_read() -> None:
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        created = client.post("/api/melds", json={"context": "side a", "ttl": "24h"})
        check("create 200", created.status_code == 200, created.text)
        body = created.json()
        code = body["code"]
        check("ttl is 24h", body["ttl"] == "24h")
        check("create is dormant", body["expires_at"] is None)
        check("capability url", body["url"].endswith(f"/m/{code}"))
        check("no owner token", "owner_token" not in body)
        check("no next-link fields", "prev_code" not in body and "thread_id" not in body)
        same = client.get(f"/m/{code}")
        api = client.get(f"/api/melds/{code}")
        check("capability read", same.status_code == 200 and same.json()["context_a"] == "side a")
        check("api read matches", api.json() == same.json())
        check("GET leaves the clock dormant", same.json()["expires_at"] is None)
        check("no replies yet", api.json()["replies"] == [] and api.json()["resolved"] is False)
        resolved = client.post(f"/api/melds/{code}/resolve", json={"context": "side b"})
        check("resolve", resolved.status_code == 200 and resolved.json()["resolved"] is True, resolved.text)
        first_exp = datetime.fromisoformat(resolved.json()["expires_at"])
        check("first reply starts 24h", first_exp == T0 + timedelta(seconds=server.TTL_SECONDS))
        check("first reply kept", resolved.json()["replies"] == ["side b"])
        clock["t"] = T0 + timedelta(minutes=10)
        peeked = client.get(f"/api/melds/{code}").json()
        check("read shows the thread", peeked["context_a"] == "side a" and peeked["replies"] == ["side b"])
        check("read does not reset 24h", peeked["expires_at"] == resolved.json()["expires_at"])
        later = client.post(f"/api/melds/{code}/resolve", json={"context": "side c"})
        check("later reply appends", later.status_code == 200 and later.json()["replies"] == ["side b", "side c"], later.text)
        later_exp = datetime.fromisoformat(later.json()["expires_at"])
        check("later reply resets 24h", later_exp == clock["t"] + timedelta(seconds=server.TTL_SECONDS))
        stored = client.get(f"/api/melds/{code}").json()
        check("stored thread", stored["context_a"] == "side a" and stored["replies"] == ["side b", "side c"])


def test_public_url() -> None:
    clock = {"t": T0}
    reset(clock)
    os.environ["MELD_PUBLIC_URL"] = "https://example.test/"
    try:
        with TestClient(server.app) as client:
            created = client.post("/api/melds", json={"context": "named host"}).json()
            check("public url", created["url"].startswith("https://example.test/m/"))
    finally:
        os.environ.pop("MELD_PUBLIC_URL", None)


def test_ttl_lock() -> None:
    clock = {"t": T0}
    reset(clock)
    check("pilot ttl is 24h", server.TTL_SECONDS == 24 * 60 * 60 and server.TTL_KEY == "24h")
    with TestClient(server.app) as client:
        omitted = client.post("/api/melds", json={"context": "a"})
        check("omit ttl", omitted.status_code == 200 and omitted.json()["ttl"] == "24h", omitted.text)
        upper = client.post("/api/melds", json={"context": "a", "ttl": " 24H "})
        check("24H accepted", upper.status_code == 200, upper.text)
        for bad in ("1hr", "1d", "3m", "60", 3600, ""):
            # Empty string is omit. Skip it here.
            if bad == "":
                continue
            got = client.post("/api/melds", json={"context": "a", "ttl": bad})
            check(f"reject ttl {bad!r}", got.status_code == 400 and "24h" in got.text, got.text)
        empty = client.post("/api/melds", json={"context": "   "})
        check("blank context", empty.status_code == 400, empty.text)
        missing = client.get("/api/melds/no-such-code")
        check("unknown 404", missing.status_code == 404, missing.text)


def test_silence_closes_same_bridge() -> None:
    """24h of silence closes the bridge. A reply on the same link resets 24h."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        stale = client.post("/api/melds", json={"context": "stale-body"}).json()
        live = client.post("/api/melds", json={"context": "live-body"}).json()
        client.post(f"/api/melds/{stale['code']}/resolve", json={"context": "stale-reply"})
        client.post(f"/api/melds/{live['code']}/resolve", json={"context": "live-reply"})
        clock["t"] = T0 + timedelta(minutes=30)
        again = client.post(f"/api/melds/{live['code']}/resolve", json={"context": "live-again"})
        check("same bridge", again.status_code == 200 and again.json()["code"] == live["code"], again.text)
        check("both replies kept", again.json()["replies"] == ["live-reply", "live-again"])
        reset_exp = datetime.fromisoformat(again.json()["expires_at"])
        check("reply resets 24h", reset_exp == clock["t"] + timedelta(seconds=server.TTL_SECONDS))
        clock["t"] = T0 + timedelta(hours=23)
        check("still open inside 24h", client.get(f"/api/melds/{stale['code']}").status_code == 200)
        nxt = client.post("/api/melds", json={"context": "nope", "prev_code": live["code"]})
        check("no next link", nxt.status_code == 400 and "no next link" in nxt.text, nxt.text)
        check("next link did not allocate", len(server._melds) == 2)

        clock["t"] = T0 + timedelta(hours=24, seconds=1)
        late = client.post(f"/api/melds/{stale['code']}/resolve", json={"context": "too late"})
        check("reply on quiet bridge is 410", late.status_code == 410, late.text)
        check("quiet bridge still 410", client.get(f"/api/melds/{stale['code']}").status_code == 410)
        check("quiet plaintext absent", "stale-body" not in late.text and "stale-reply" not in late.text)
        still = client.get(f"/api/melds/{live['code']}")
        check("reset bridge still live", still.status_code == 200, still.text)
        check("live thread intact", still.json()["replies"] == ["live-reply", "live-again"])

        clock["t"] = reset_exp + timedelta(seconds=1)
        gone = client.get(f"/m/{live['code']}")
        check("quiet after reset is 410", gone.status_code == 410, gone.text)
        check("live plaintext not kept", "live-body" not in gone.text and "live-again" not in gone.text)
        check("row deleted", live["code"] not in server._melds)
        check(
            "tombstone is code only",
            live["code"] in server._tombstones and server._tombstones[live["code"]] is None,
        )
        missing = client.post(f"/api/melds/missing-code/resolve", json={"context": "x"})
        check("resolve unknown is 404", missing.status_code == 404, missing.text)


def test_dissolved_stays_410_unknown_stays_404() -> None:
    """Purge and chain deletes must keep serving 410. Never-existed stays 404."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        purged = client.post("/api/melds", json={"context": "purge-me"}).json()
        client.post(f"/api/melds/{purged['code']}/resolve", json={"context": "purge-reply"})
        clock["t"] = T0 + timedelta(hours=24, seconds=1)
        fresh = client.post("/api/melds", json={"context": "still-here"})
        check("create after expiry", fresh.status_code == 200, fresh.text)
        check("purge removed the row", purged["code"] not in server._melds)
        gone = client.get(f"/api/melds/{purged['code']}")
        check("purged code 410", gone.status_code == 410, gone.text)
        check("purge body has no plaintext", "purge-me" not in gone.text)
        check("purged capability 410", client.get(f"/m/{purged['code']}").status_code == 410)
        check(
            "resolve dissolved is 410",
            client.post(f"/api/melds/{purged['code']}/resolve", json={"context": "b"}).status_code == 410,
        )
        live = client.get(f"/api/melds/{fresh.json()['code']}")
        check("live path unchanged", live.status_code == 200 and live.json()["context_a"] == "still-here")
        check("dormant create has no timer", live.json()["expires_at"] is None)
        missing = client.get("/api/melds/never-existed")
        check("never existed 404", missing.status_code == 404, missing.text)
        check("never existed capability 404", client.get("/m/never-existed").status_code == 404)
        check(
            "tombstones store no payload",
            all(value is None for value in server._tombstones.values())
            and purged["code"] in server._tombstones,
        )


def test_tombstone_cap_drops_oldest() -> None:
    clock = {"t": T0}
    reset(clock)
    previous = server.TOMBSTONE_CAP
    server.TOMBSTONE_CAP = 2
    try:
        with TestClient(server.app) as client:
            first = client.post("/api/melds", json={"context": "first"}).json()
            second = client.post("/api/melds", json={"context": "second"}).json()
            third = client.post("/api/melds", json={"context": "third"}).json()
            for row in (first, second, third):
                client.post(f"/api/melds/{row['code']}/resolve", json={"context": "reply"})
            clock["t"] = T0 + timedelta(hours=24, seconds=1)
            check("oldest dissolve 410", client.get(f"/m/{first['code']}").status_code == 410)
            check("middle dissolve 410", client.get(f"/m/{second['code']}").status_code == 410)
            check("newest dissolve 410", client.get(f"/m/{third['code']}").status_code == 410)
            check("cap held", len(server._tombstones) == 2)
            check("oldest forgotten is 404", client.get(f"/api/melds/{first['code']}").status_code == 404)
            check("middle still 410", client.get(f"/m/{second['code']}").status_code == 410)
            check("newest still 410", client.get(f"/api/melds/{third['code']}").status_code == 410)
            check("never existed stays 404", client.get("/m/never-existed-xx").status_code == 404)
            check(
                "reply on forgotten is 404",
                client.post(f"/api/melds/{first['code']}/resolve", json={"context": "x"}).status_code == 404,
            )
            check(
                "reply on remembered is 410",
                client.post(f"/api/melds/{second['code']}/resolve", json={"context": "x"}).status_code == 410,
            )
            live = client.post("/api/melds", json={"context": "live-after"})
            check("live create", live.status_code == 200, live.text)
            check(
                "live path after cap",
                client.get(f"/m/{live.json()['code']}").status_code == 200
                and client.get(f"/m/{live.json()['code']}").json()["context_a"] == "live-after",
            )
    finally:
        server.TOMBSTONE_CAP = previous


def test_thread_and_clock_reset() -> None:
    """Replies accumulate. Each one resets 24h. A read does not."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        created = client.post("/api/melds", json={"context": "opening"}).json()
        code = created["code"]
        clock["t"] = T0 + timedelta(hours=5)
        dormant = client.get(f"/api/melds/{code}")
        check("dormant survives a long wait", dormant.status_code == 200 and dormant.json()["expires_at"] is None)
        check("long wait did not start the clock", server._melds[code]["expires_at"] is None)

        preview = client.get(
            f"/m/{code}",
            headers={"User-Agent": "Slackbot-LinkExpanding 1.0", "Accept": "application/json"},
        )
        check("slack preview", preview.status_code == 200 and "opening" not in preview.text, preview.text)
        check("slack left the clock dormant", server._melds[code]["expires_at"] is None)
        check("head is not a body read", client.head(f"/m/{code}").status_code == 405)
        check("head left the clock dormant", server._melds[code]["expires_at"] is None)

        first = client.post(f"/api/melds/{code}/resolve", json={"context": "one"})
        check("first reply", first.status_code == 200 and first.json()["replies"] == ["one"], first.text)
        first_exp = datetime.fromisoformat(first.json()["expires_at"])
        check("first reply starts 24h", first_exp == clock["t"] + timedelta(seconds=server.TTL_SECONDS))

        clock["t"] = clock["t"] + timedelta(minutes=20)
        peeked = client.get(f"/m/{code}", headers={"User-Agent": "Mozilla/5.0"})
        check("body read returns the thread", peeked.status_code == 200 and peeked.json()["replies"] == ["one"])
        check("body read does not reset", peeked.json()["expires_at"] == first.json()["expires_at"])

        # Keep talking. Each reply resets 24h. There is no maximum lifetime.
        replies = ["one"]
        expected = first_exp
        for minute, text in ((50, "two"), (100, "three"), (150, "four")):
            clock["t"] = T0 + timedelta(hours=5, minutes=minute)
            got = client.post(f"/api/melds/{code}/resolve", json={"context": text})
            replies.append(text)
            check(f"reply {text} kept", got.status_code == 200 and got.json()["replies"] == replies, got.text)
            expected = clock["t"] + timedelta(seconds=server.TTL_SECONDS)
            check(
                f"reply {text} resets 24h",
                datetime.fromisoformat(got.json()["expires_at"]) == expected,
                got.json()["expires_at"],
            )
            check(f"reply {text} extends past the first 24h", expected > first_exp)
        seen = client.get(f"/api/melds/{code}").json()
        check("full thread", seen["context_a"] == "opening" and seen["replies"] == replies)
        check("read after resets does not move the timer", seen["expires_at"] == expected.isoformat())

        clock["t"] = expected + timedelta(seconds=1)
        closed = client.get(f"/api/melds/{code}")
        check("24h of silence closes it", closed.status_code == 410, closed.text)
        check("thread plaintext is gone", "opening" not in closed.text and "four" not in closed.text)
        check("unknown stays 404", client.get("/api/melds/never-existed").status_code == 404)
        check("chain route is gone", client.get(f"/api/melds/{code}/chain").status_code == 404)


def test_preview_does_not_flip_410_or_404() -> None:
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        row = client.post("/api/melds", json={"context": "gone-secret"}).json()
        client.post(f"/api/melds/{row['code']}/resolve", json={"context": "gone-reply"})
        clock["t"] = T0 + timedelta(hours=24, seconds=1)
        check("dissolve on read", client.get(f"/api/melds/{row['code']}").status_code == 410)
        preview = client.get(f"/m/{row['code']}", headers={"User-Agent": "Twitterbot/1.0"})
        check(
            "preview of a tombstone",
            preview.status_code == 200 and "gone-secret" not in preview.text and "gone-reply" not in preview.text,
            preview.text,
        )
        still_gone = client.get(f"/m/{row['code']}")
        check("tombstone stays 410", still_gone.status_code == 410 and "gone-secret" not in still_gone.text, still_gone.text)
        check("unknown stays 404", client.get("/api/melds/never-existed").status_code == 404)
        missing_preview = client.get("/m/never-existed", headers={"User-Agent": "facebookexternalhit/1.1"})
        check(
            "preview of an unknown code",
            missing_preview.status_code == 200 and "never-existed" not in missing_preview.text,
            missing_preview.text,
        )
        check("unknown still 404", client.get("/api/melds/never-existed").status_code == 404)
        check("unknown capability still 404", client.get("/m/never-existed").status_code == 404)
        check("preview did not mint a row", "never-existed" not in server._melds and "never-existed" not in server._tombstones)


def test_surface() -> None:
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        home = client.get("/")
        check("home", home.status_code == 200 and "Not for secrets" in home.text, home.text)
        lowered_home = home.text.lower()
        check(
            "home has no zk",
            ("zero" + "-knowledge") not in lowered_home and ("zero " + "knowledge") not in lowered_home,
        )
        check("health", client.get("/health").json() == {"ok": True})
        for path in ("/api/stripe/webhook", "/v1/melds", "/api/checkout"):
            got = client.post(path, json={})
            check(f"{path} absent", got.status_code == 404, got.text)


def test_public_tree() -> None:
    allowed = {
        ".dockerignore",
        ".env.example",
        ".gitignore",
        "Caddyfile",
        "Dockerfile",
        "LICENSE",
        "README.md",
        "TRUST.md",
        "docker-compose.yml",
        "requirements.txt",
        "server.py",
        "test_server.py",
    }
    skip = {".git", "__pycache__", ".venv", ".pytest_cache", ".env"}
    present = {path.name for path in ROOT.iterdir() if path.name not in skip}
    check("root is the self-host set", present == allowed, str(sorted(present ^ allowed)))
    check("no deploy directory", not (ROOT / "deploy").exists())
    caddy = (ROOT / "Caddyfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    check("caddy proxies the server", "reverse_proxy meld:8080" in caddy)
    check("compose image name", "ghcr.io/lemonaide152/meld:latest" in compose)
    check("compose runs caddy", "caddy:2" in compose)
    readme = (ROOT / "README.md").read_text()
    trust = (ROOT / "TRUST.md").read_text()
    check("readme hosted pointer", readme.count("https://meld.mergeinc.workers.dev") == 1)
    check("readme self-host", "python server.py" in readme and "docker compose up" in readme and "docker run" in readme)
    check("readme publish", "ghcr.io/lemonaide152/meld:latest" in readme and "docker push" in readme)
    check("readme host-readable", "Host-readable while live." in readme)
    check("readme not for secrets", "Not for secrets" in readme)
    check("readme tombstone cap", str(server.TOMBSTONE_CAP) in readme)
    check("readme distinguishes gone", "410" in readme and "404" in readme)
    check("readme says 24h", "24h" in readme)
    check("trust says 24h", "24h" in trust)
    check("trust host-readable", "Host-readable while live." in trust)
    check("trust not a vault", "not a vault" in trust)
    check("trust tombstone cap", str(server.TOMBSTONE_CAP) in trust)
    check("trust distinguishes gone", "410" in trust and "404" in trust)
    for phrase in ("mint-next", "mint next", "prev_code", "next hop"):
        check(f"readme has no {phrase}", phrase not in readme.lower())
        check(f"trust has no {phrase}", phrase not in trust.lower())
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "test_server.py":
            continue
        if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
            continue
        text = path.read_text(errors="replace")
        lowered = text.lower()
        for banned in BANNED:
            if banned.lower() in lowered:
                fail(f"{path.relative_to(ROOT)} contains {banned}")
        for found in re.findall(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            text,
        ):
            fail(f"{path.relative_to(ROOT)} contains unexpected id {found}")
    print("ok public tree")


def main() -> None:
    test_create_resolve_read()
    test_public_url()
    test_ttl_lock()
    test_silence_closes_same_bridge()
    test_dissolved_stays_410_unknown_stays_404()
    test_tombstone_cap_drops_oldest()
    test_thread_and_clock_reset()
    test_preview_does_not_flip_410_or_404()
    test_surface()
    test_public_tree()
    print("all passed")


if __name__ == "__main__":
    main()
