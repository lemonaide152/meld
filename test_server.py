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
    server._now = lambda: clock["t"]


READ_KEYS = {
    "code",
    "resolved",
    "resolved_at",
    "expires_at",
    "seconds_remaining",
    "note",
    "context_a",
    "for",
    "not_for",
    "replies",
}


def pour(note, **extra):
    """One note. The older wire keys carry that same text."""
    body = {"context": note, "for": note, "not_for": note}
    body.update(extra)
    return body


def test_create_resolve_read() -> None:
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        created = client.post("/api/melds", json=pour("side a"))
        check("create 200", created.status_code == 200, created.text)
        body = created.json()
        code = body["code"]
        check(
            "one note on the wire",
            body["note"] == "side a"
            and body["context_a"] == "side a"
            and body["for"] == "side a"
            and body["not_for"] == "side a",
        )
        check("36h from create", datetime.fromisoformat(body["expires_at"]) == T0 + timedelta(seconds=server.OPEN_SECONDS))
        check("capability url", body["url"].endswith(f"/m/{code}"))
        check("creation fields", set(body) == READ_KEYS | {"url"})
        same = client.get(f"/m/{code}")
        api = client.get(f"/api/melds/{code}")
        check("capability read", same.status_code == 200 and same.json()["context_a"] == "side a")
        check("api read matches", api.json() == same.json())
        check("GET does not move the 36h window", same.json()["expires_at"] == body["expires_at"])
        check("no replies yet", api.json()["replies"] == [] and api.json()["resolved"] is False)
        resolved = client.post(f"/api/melds/{code}/resolve", json={"context": "side b"})
        check("resolve", resolved.status_code == 200 and resolved.json()["resolved"] is True and set(resolved.json()) == READ_KEYS, resolved.text)
        first_exp = datetime.fromisoformat(resolved.json()["expires_at"])
        check("first reply sets 24 hours", first_exp == T0 + timedelta(seconds=server.TTL_SECONDS))
        check("first reply kept", resolved.json()["replies"] == ["side b"])
        clock["t"] = T0 + timedelta(minutes=10)
        peeked = client.get(f"/api/melds/{code}").json()
        check("read shows the thread", peeked["context_a"] == "side a" and peeked["replies"] == ["side b"])
        check("read does not reset 24 hours", peeked["expires_at"] == resolved.json()["expires_at"])
        check("read fields", set(peeked) == READ_KEYS)
        later = client.post(f"/api/melds/{code}/resolve", json={"context": "side c"})
        check("later reply appends", later.status_code == 200 and later.json()["replies"] == ["side b", "side c"], later.text)
        later_exp = datetime.fromisoformat(later.json()["expires_at"])
        check("later reply resets 24 hours", later_exp == clock["t"] + timedelta(seconds=server.TTL_SECONDS))
        stored = client.get(f"/api/melds/{code}").json()
        check("stored thread", stored["context_a"] == "side a" and stored["replies"] == ["side b", "side c"])


def test_public_url() -> None:
    clock = {"t": T0}
    reset(clock)
    os.environ["MELD_PUBLIC_URL"] = "https://example.test/"
    try:
        with TestClient(server.app) as client:
            created = client.post("/api/melds", json=pour("named host")).json()
            check("public url", created["url"].startswith("https://example.test/m/"))
    finally:
        os.environ.pop("MELD_PUBLIC_URL", None)


def test_ttl_lock() -> None:
    clock = {"t": T0}
    reset(clock)
    check("windows are 36h then 24h", server.OPEN_SECONDS == 36 * 60 * 60 and server.TTL_SECONDS == 24 * 60 * 60)
    with TestClient(server.app) as client:
        opened = client.post("/api/melds", json=pour("a"))
        check("create opens 36 hours", opened.status_code == 200, opened.text)
        check(
            "create expiry is 36 hours",
            datetime.fromisoformat(opened.json()["expires_at"]) == T0 + timedelta(hours=36),
        )
        for bad in ("1hr", "24h", "36h", "1d", "3m", "60", 3600):
            got = client.post("/api/melds", json=pour("a", ttl=bad))
            check(f"reject ttl {bad!r}", got.status_code == 400 and "36 hours" in got.text and "24 hour" in got.text, got.text)
        empty = client.post("/api/melds", json=pour("   "))
        check("blank note", empty.status_code == 400 and "note" in empty.text.lower(), empty.text)
        missing = client.get("/api/melds/no-such-code")
        check("unknown 404", missing.status_code == 404, missing.text)


def test_silence_closes_same_bridge() -> None:
    """24h of silence closes the bridge. A reply on the same link resets 24h."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        stale = client.post("/api/melds", json=pour("stale-body")).json()
        live = client.post("/api/melds", json=pour("live-body")).json()
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
        nxt = client.post("/api/melds", json=pour("nope", prev_code=live["code"]))
        check("stays on this bridge", nxt.status_code == 400 and "stays on this bridge" in nxt.text, nxt.text)
        check("same links only", len(server._melds) == 2)

        clock["t"] = T0 + timedelta(hours=24, seconds=1)
        unknown_reply = client.post("/api/melds/missing-code/resolve", json={"context": "x"})
        late = client.post(f"/api/melds/{stale['code']}/resolve", json={"context": "too late"})
        check("reply on quiet bridge is 404", late.status_code == 404, late.text)
        check("quiet reply matches unknown", late.text == unknown_reply.text)
        check("quiet plaintext absent", "stale-body" not in late.text and "stale-reply" not in late.text)
        quiet = client.get(f"/api/melds/{stale['code']}")
        unknown_get = client.get("/api/melds/missing-code")
        check("quiet bridge is 404", quiet.status_code == 404 and quiet.text == unknown_get.text, quiet.text)
        still = client.get(f"/api/melds/{live['code']}")
        check("reset bridge still live", still.status_code == 200, still.text)
        check("live thread intact", still.json()["replies"] == ["live-reply", "live-again"])

        clock["t"] = reset_exp + timedelta(seconds=1)
        closed = client.get(f"/m/{live['code']}")
        unknown_cap = client.get("/m/missing-code")
        check("quiet after reset is 404", closed.status_code == 404 and closed.text == unknown_cap.text, closed.text)
        check("live plaintext not kept", "live-body" not in closed.text and "live-again" not in closed.text)
        check("row deleted", live["code"] not in server._melds and stale["code"] not in server._melds)
        check("resolve unknown is 404", unknown_reply.status_code == 404, unknown_reply.text)


def test_dissolved_matches_unknown() -> None:
    """A dissolved code and an unknown code return the same 404."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        purged = client.post("/api/melds", json=pour("purge-me")).json()
        client.post(f"/api/melds/{purged['code']}/resolve", json={"context": "purge-reply"})
        clock["t"] = T0 + timedelta(hours=24, seconds=1)
        fresh = client.post("/api/melds", json=pour("still-here"))
        check("create after expiry", fresh.status_code == 200, fresh.text)
        check("purge removed the row", purged["code"] not in server._melds)
        closed = client.get(f"/api/melds/{purged['code']}")
        missing = client.get("/api/melds/never-existed")
        check("purged code 404", closed.status_code == 404, closed.text)
        check("purge body has no plaintext", "purge-me" not in closed.text and "purge-reply" not in closed.text)
        check("dissolved matches unknown", closed.text == missing.text and closed.status_code == missing.status_code)
        closed_cap = client.get(f"/m/{purged['code']}")
        missing_cap = client.get("/m/never-existed")
        check("capability matches unknown", closed_cap.status_code == 404 and closed_cap.text == missing_cap.text)
        closed_reply = client.post(f"/api/melds/{purged['code']}/resolve", json={"context": "b"})
        missing_reply = client.post("/api/melds/never-existed/resolve", json={"context": "b"})
        check("resolve matches unknown", closed_reply.status_code == 404 and closed_reply.text == missing_reply.text)
        live = client.get(f"/api/melds/{fresh.json()['code']}")
        check("live path unchanged", live.status_code == 200 and live.json()["context_a"] == "still-here")
        check(
            "fresh create is 36 hours",
            datetime.fromisoformat(live.json()["expires_at"]) == clock["t"] + timedelta(hours=36),
        )
        check("no record of the dead code", purged["code"] not in server._melds)


def test_dissolve_keeps_no_record() -> None:
    """Each dissolve deletes that bridge. A later create still works."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        rows = [client.post("/api/melds", json=pour(label)).json() for label in ("first", "second", "third")]
        for row in rows:
            client.post(f"/api/melds/{row['code']}/resolve", json={"context": "reply"})
        clock["t"] = T0 + timedelta(hours=24, seconds=1)
        unknown = client.get("/api/melds/never-existed-xx")
        for row in rows:
            got = client.get(f"/api/melds/{row['code']}")
            check(f"{row['context_a']} is 404", got.status_code == 404 and got.text == unknown.text, got.text)
            check(f"{row['context_a']} plaintext absent", row["context_a"] not in got.text and "reply" not in got.text)
            check(f"{row['context_a']} row deleted", row["code"] not in server._melds)
            cap = client.get(f"/m/{row['code']}")
            check(f"{row['context_a']} capability matches unknown", cap.text == client.get("/m/never-existed-xx").text)
            reply = client.post(f"/api/melds/{row['code']}/resolve", json={"context": "x"})
            other = client.post("/api/melds/never-existed-xx/resolve", json={"context": "x"})
            check(f"{row['context_a']} reply matches unknown", reply.status_code == 404 and reply.text == other.text)
        live = client.post("/api/melds", json=pour("live-after"))
        check("live create", live.status_code == 200, live.text)
        check(
            "live path after dissolve",
            client.get(f"/m/{live.json()['code']}").status_code == 200
            and client.get(f"/m/{live.json()['code']}").json()["context_a"] == "live-after",
        )
        check("only the live row remains", set(server._melds) == {live.json()["code"]})


def test_thread_and_clock_reset() -> None:
    """Replies accumulate. Each one resets 24h. A read does not."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        created = client.post("/api/melds", json=pour("opening")).json()
        code = created["code"]
        opened = created["expires_at"]
        clock["t"] = T0 + timedelta(hours=5)
        waiting = client.get(f"/api/melds/{code}")
        check("still open inside 36 hours", waiting.status_code == 200 and waiting.json()["expires_at"] == opened)
        check("read did not move the 36h window", server._melds[code]["expires_at"] == datetime.fromisoformat(opened))

        preview = client.get(
            f"/m/{code}",
            headers={"User-Agent": "Slackbot-LinkExpanding 1.0", "Accept": "application/json"},
        )
        check("slack preview", preview.status_code == 200 and "opening" not in preview.text, preview.text)
        check("slack did not move the timer", server._melds[code]["expires_at"] == datetime.fromisoformat(opened))
        check("head is not a body read", client.head(f"/m/{code}").status_code == 405)
        check("head did not move the timer", server._melds[code]["expires_at"] == datetime.fromisoformat(opened))

        first = client.post(f"/api/melds/{code}/resolve", json={"context": "one"})
        check("first reply", first.status_code == 200 and first.json()["replies"] == ["one"], first.text)
        first_exp = datetime.fromisoformat(first.json()["expires_at"])
        check("first reply sets 24 hours", first_exp == clock["t"] + timedelta(seconds=server.TTL_SECONDS))

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
        unknown = client.get("/api/melds/never-existed")
        check("24h of silence closes it", closed.status_code == 404 and closed.text == unknown.text, closed.text)
        check("thread plaintext is deleted", "opening" not in closed.text and "four" not in closed.text)
        check("unknown stays 404", unknown.status_code == 404)


def test_36h_until_first_reply_then_24h() -> None:
    """No reply closes at 36 hours from create. The first reply sets 24 hours."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        quiet = client.post("/api/melds", json=pour("quiet-body")).json()
        active = client.post("/api/melds", json=pour("active-body")).json()
        check(
            "create stores one note",
            active["note"] == "active-body"
            and active["context_a"] == "active-body"
            and active["for"] == "active-body"
            and active["not_for"] == "active-body",
        )
        check("create window is 36 hours", quiet["expires_at"] == active["expires_at"])
        clock["t"] = T0 + timedelta(hours=10)
        seen = client.get(f"/api/melds/{active['code']}").json()
        check("read does not start or reset", seen["expires_at"] == active["expires_at"])
        check("read fields during the open window", set(seen) == READ_KEYS)
        clock["t"] = T0 + timedelta(hours=30)
        first = client.post(f"/api/melds/{active['code']}/resolve", json={"context": "from B"})
        first_exp = datetime.fromisoformat(first.json()["expires_at"])
        check("B's first reply sets 24 hours", first_exp == clock["t"] + timedelta(hours=24), first.text)
        check("24h sliding passes the 36h mark", first_exp > datetime.fromisoformat(active["expires_at"]))
        clock["t"] = T0 + timedelta(hours=36, seconds=1)
        closed = client.get(f"/api/melds/{quiet['code']}")
        unknown = client.get("/api/melds/no-such-quiet")
        check(
            "no reply closes at 36 hours",
            closed.status_code == 404 and closed.text == unknown.text and "quiet-body" not in closed.text,
            closed.text,
        )
        check("replied bridge stays open past 36 hours", client.get(f"/api/melds/{active['code']}").status_code == 200)
        clock["t"] = first_exp - timedelta(minutes=30)
        again = client.post(f"/api/melds/{active['code']}/resolve", json={"context": "more"})
        check("later replies are kept", again.status_code == 200 and again.json()["replies"] == ["from B", "more"], again.text)
        reset_at = clock["t"] + timedelta(hours=24)
        check("later reply resets 24 hours", datetime.fromisoformat(again.json()["expires_at"]) == reset_at)
        held = again.json()["expires_at"]
        clock["t"] = clock["t"] + timedelta(hours=2)
        check("another read does not reset", client.get(f"/m/{active['code']}").json()["expires_at"] == held)


def test_preview_does_not_change_404() -> None:
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        row = client.post("/api/melds", json=pour("hidden-secret")).json()
        client.post(f"/api/melds/{row['code']}/resolve", json={"context": "hidden-reply"})
        clock["t"] = T0 + timedelta(hours=24, seconds=1)
        dissolved = client.get(f"/api/melds/{row['code']}")
        unknown = client.get("/api/melds/never-existed")
        check("dissolve on read", dissolved.status_code == 404 and dissolved.text == unknown.text, dissolved.text)
        preview = client.get(f"/m/{row['code']}", headers={"User-Agent": "Twitterbot/1.0"})
        check(
            "preview of a dissolved code",
            preview.status_code == 200 and "hidden-secret" not in preview.text and "hidden-reply" not in preview.text,
            preview.text,
        )
        still = client.get(f"/m/{row['code']}")
        unknown_cap = client.get("/m/never-existed")
        check("dissolved stays 404", still.status_code == 404 and still.text == unknown_cap.text and "hidden-secret" not in still.text, still.text)
        missing_preview = client.get("/m/never-existed", headers={"User-Agent": "facebookexternalhit/1.1"})
        check(
            "preview of an unknown code",
            missing_preview.status_code == 200 and missing_preview.text == preview.text and "never-existed" not in missing_preview.text,
            missing_preview.text,
        )
        check("unknown still 404", client.get("/api/melds/never-existed").text == unknown.text)
        check("unknown capability still 404", client.get("/m/never-existed").text == unknown_cap.text)
        check("preview did not create a row", "never-existed" not in server._melds)
        check("dissolve left no record", row["code"] not in server._melds)


def test_one_note_create_body() -> None:
    """Creation is one note. The clock is 36 hours from create, and a read does not extend it."""
    clock = {"t": T0}
    reset(clock)
    note = "For a design review. Not for passwords or customer data."
    with TestClient(server.app) as client:
        only = client.post("/api/melds", json={"note": note})
        check("note field creates", only.status_code == 200, only.text)
        body = only.json()
        check(
            "note is the only text",
            body["note"] == note
            and body["context_a"] == note
            and body["for"] == note
            and body["not_for"] == note,
        )
        opened = datetime.fromisoformat(body["expires_at"])
        check("note opens 36 hours", opened == T0 + timedelta(hours=36))
        check("note is not a 1 hour link", opened != T0 + timedelta(hours=1))
        clock["t"] = T0 + timedelta(hours=35)
        peeked = client.get(f"/api/melds/{body['code']}")
        check("read inside 36 hours", peeked.status_code == 200 and peeked.json()["expires_at"] == body["expires_at"], peeked.text)
        check("read did not extend the clock", peeked.json()["seconds_remaining"] == 60 * 60)
        clock["t"] = T0 + timedelta(hours=36, seconds=1)
        closed = client.get(f"/api/melds/{body['code']}")
        unknown = client.get("/api/melds/no-such-note")
        check("quiet note is 404", closed.status_code == 404 and closed.text == unknown.text, closed.text)
        check("quiet note plaintext absent", note not in closed.text)

        wire = client.post("/api/melds", json={"context": note, "for": note, "not_for": note})
        check("same text on the wire", wire.status_code == 200 and wire.json()["note"] == note, wire.text)
        check(
            "wire echoes one note",
            wire.json()["context_a"] == note and wire.json()["for"] == note and wire.json()["not_for"] == note,
        )

        context_only = client.post("/api/melds", json={"context": note})
        check(
            "context alone is the note",
            context_only.status_code == 200
            and context_only.json()["note"] == note
            and context_only.json()["for"] == note
            and context_only.json()["not_for"] == note,
            context_only.text,
        )

        mismatch = client.post(
            "/api/melds",
            json={"context": "the working dump", "for": "a handoff", "not_for": "secrets"},
        )
        check("split fields are rejected", mismatch.status_code == 400 and "same text" in mismatch.text, mismatch.text)
        absent = client.post("/api/melds", json={})
        check("note is required", absent.status_code == 400 and "note" in absent.text.lower(), absent.text)
        long_note = "For the review. Not for secrets. " + ("detail " * 400)
        check("long note is still one field", len(long_note) > 2000)
        long = client.post("/api/melds", json={"note": long_note})
        check("long note accepted", long.status_code == 200 and long.json()["for"] == long_note, long.text)
        huge = client.post("/api/melds", json={"note": "x" * (server.MAX_CONTEXT + 1)})
        check("note cap", huge.status_code == 400, huge.text)
        extra = client.post("/api/melds", json={"note": note, "learn": True})
        check("learn is not a self-host field", extra.status_code == 200 and "learn" not in extra.json(), extra.text)


def test_surface() -> None:
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        home = client.get("/")
        check("home", home.status_code == 200 and "Not for secrets" in home.text, home.text)
        check(
            "home one note",
            "one note" in home.text.lower()
            and "what the exchange is for and what it is not for" in home.text,
            home.text,
        )
        check(
            "root says 36 hours then 24 hours",
            "36 hours from create" in home.text
            and "sets a 24 hour timer" in home.text
            and "resets that 24 hours" in home.text,
            home.text,
        )
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
    check("readme has no hosted try-now", "workers.dev" not in readme and "Hosted try-now" not in readme)
    check("readme self-host", "python server.py" in readme and "docker compose up" in readme and "docker run" in readme)
    check("readme publish", "ghcr.io/lemonaide152/meld:latest" in readme and "docker push" in readme)
    check("readme host-readable", "Host-readable while live." in readme)
    check("readme not for secrets", "Not for secrets" in readme)
    check("readme says 36h then 24h", "36 hours" in readme and "24 hours" in readme and "privately" in readme)
    check("trust says 36h then 24h", "36 hours" in trust and "24 hours" in trust and "privately" in trust)
    check("trust host-readable", "Host-readable while live." in trust)
    check("trust not a vault", "not a vault" in trust)
    check("readme says creation", "creates the link" in readme and "Creation." in readme and "mint" not in readme.lower())
    check("trust says creation", "creates the link" in trust and "mint" not in trust.lower())
    check(
        "readme one note",
        "One note says what the exchange is for and what it is not for." in readme
        and '"note":"For a design review. Not for passwords or customer data."' in readme,
    )
    check(
        "readme wire is the same note",
        readme.count("For a design review. Not for passwords or customer data.") >= 2,
    )
    check("trust one note", "One note says what the exchange is for and what it is not for." in trust)
    check("readme no split declaration", "The declaration says what the bridge is for." not in readme)
    check("trust no split declaration", "The declaration says what the bridge is for." not in trust)
    server_text = (ROOT / "server.py").read_text()
    check("server says creation", "creates a link" in server_text and "mint" not in server_text.lower())
    check("server one note", "def _one_note" in server_text and "MAX_DECLARATION" not in server_text)
    check("server clock defaults", "OPEN_SECONDS = 36 * 60 * 60" in server_text and "TTL_SECONDS = 24 * 60 * 60" in server_text)
    check("server has no 1hr default", "1hr" not in server_text and "one hour" not in server_text.lower() and "1 hour" not in server_text.lower())
    for label, text in (
        ("readme", readme),
        ("trust", trust),
        ("caddy", caddy),
        ("compose", compose),
        ("dockerfile", (ROOT / "Dockerfile").read_text()),
        ("env", (ROOT / ".env.example").read_text()),
    ):
        lowered = text.lower()
        check(f"{label} has no mint", "mint" not in lowered)
        check(f"{label} has no one hour", "one hour" not in lowered and "1 hour" not in lowered)
        check(f"{label} has no workers.dev", "workers.dev" not in lowered)
    owner_token = "owner" + "_token"
    for blob in (server_text, readme, trust, caddy, compose):
        lowered = blob.lower()
        if owner_token in lowered or "x-owner" in lowered:
            fail("owner token leaked into the self-host tree")
    check("readme same 404", "never existed is **404**" in readme and "expired code is **404**" in readme)
    check("trust same 404", "dissolved code is 404" in trust and "expired code is 404" in trust)
    dead_status = "41" + "0"
    dead_word = "tomb" + "stone"
    closed_word = "go" + "ne"
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
            continue
        text = path.read_text(errors="replace")
        lowered = text.lower()
        if dead_status in text or dead_word in lowered or closed_word in lowered:
            fail(f"{path.relative_to(ROOT)} keeps a dead-code promise")
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
    test_one_note_create_body()
    test_silence_closes_same_bridge()
    test_dissolved_matches_unknown()
    test_dissolve_keeps_no_record()
    test_thread_and_clock_reset()
    test_36h_until_first_reply_then_24h()
    test_preview_does_not_change_404()
    test_surface()
    test_public_tree()
    print("all passed")


if __name__ == "__main__":
    main()
