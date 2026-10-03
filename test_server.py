"""Base-case server: create, resolve, 1 hour dissolve, mint-next, public-tree hygiene."""

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
        created = client.post("/api/melds", json={"context": "side a", "ttl": "1hr"})
        check("create 200", created.status_code == 200, created.text)
        body = created.json()
        code = body["code"]
        check("ttl is 1hr", body["ttl"] == "1hr")
        check("create is dormant", body["expires_at"] is None)
        check("capability url", body["url"].endswith(f"/m/{code}"))
        check("no owner token", "owner_token" not in body)
        check("prev empty on root", body["prev_code"] is None)
        same = client.get(f"/m/{code}")
        api = client.get(f"/api/melds/{code}")
        check("capability read", same.status_code == 200 and same.json()["context_a"] == "side a")
        check("api read matches", api.json() == same.json())
        check("GET starts clock", same.json()["expires_at"] is not None)
        check("unresolved body", api.json()["context_b"] is None and api.json()["resolved"] is False)
        started = same.json()["expires_at"]
        resolved = client.post(f"/api/melds/{code}/resolve", json={"context": "side b"})
        check("resolve", resolved.status_code == 200 and resolved.json()["resolved"] is True, resolved.text)
        check("resolve keeps expiry", resolved.json()["expires_at"] == started)
        retry = client.post(f"/api/melds/{code}/resolve", json={"context": "side b"})
        check("same answer retries", retry.status_code == 200 and retry.json().get("retry") is True)
        clash = client.post(f"/api/melds/{code}/resolve", json={"context": "side c"})
        check("different answer 409", clash.status_code == 409, clash.text)
        clash_body = clash.json()
        check(
            "409 is a status",
            clash_body.get("detail") == "Already resolved with a different answer",
            clash.text,
        )
        check("409 echoes the winner", clash_body.get("context_b") == "side b", clash.text)
        check("409 omits the loser", "side c" not in clash.text, clash.text)
        both = client.get(f"/api/melds/{code}").json()
        check("both sides", both["context_a"] == "side a" and both["context_b"] == "side b")
        check("conflict did not overwrite", both["context_b"] == "side b" and server._melds[code]["context_b"] == "side b")


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
    check("hour is fixed", server.HOUR_SECONDS == 3600)
    with TestClient(server.app) as client:
        omitted = client.post("/api/melds", json={"context": "a"})
        check("omit ttl", omitted.status_code == 200 and omitted.json()["ttl"] == "1hr", omitted.text)
        upper = client.post("/api/melds", json={"context": "a", "ttl": " 1HR "})
        check("1HR accepted", upper.status_code == 200, upper.text)
        for bad in ("1d", "3m", "60", 3600, ""):
            # Empty string is omit. Skip it here.
            if bad == "":
                continue
            got = client.post("/api/melds", json={"context": "a", "ttl": bad})
            check(f"reject ttl {bad!r}", got.status_code == 400 and "1 hour" in got.text, got.text)
        empty = client.post("/api/melds", json={"context": "   "})
        check("blank context", empty.status_code == 400, empty.text)
        missing = client.get("/api/melds/no-such-code")
        check("unknown 404", missing.status_code == 404, missing.text)


def test_dissolve_and_mint_next() -> None:
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        stale = client.post("/api/melds", json={"context": "stale-body"}).json()
        parent = client.post("/api/melds", json={"context": "parent-body"}).json()
        # Start parent clock so we can later expire it independently of the hop.
        client.get(f"/api/melds/{parent['code']}")
        parent_exp = client.get(f"/api/melds/{parent['code']}").json()["expires_at"]
        stale_exp = client.get(f"/api/melds/{stale['code']}").json()["expires_at"]  # start at T0
        clock["t"] = T0 + timedelta(minutes=30)
        hop = client.post(
            "/api/melds",
            json={"context": "child-body", "prev_code": parent["code"]},
        )
        check("mint 200", hop.status_code == 200, hop.text)
        hop_body = hop.json()
        check("new code", hop_body["code"] != parent["code"])
        check("links parent", hop_body["prev_code"] == parent["code"])
        check("same thread", hop_body["thread_id"] == parent["thread_id"])
        check("child create is dormant", hop_body["expires_at"] is None)
        hop_live = client.get(f"/api/melds/{hop_body['code']}").json()
        check("child has its own expiry after GET", hop_live["expires_at"] != parent_exp)
        still = client.get(f"/api/melds/{parent['code']}").json()
        check("parent clock unchanged", still["expires_at"] == parent_exp)
        check("stale clock unchanged", client.get(f"/api/melds/{stale['code']}").json()["expires_at"] == stale_exp)

        clock["t"] = T0 + timedelta(hours=1, seconds=1)
        late = client.post("/api/melds", json={"context": "too late", "prev_code": stale["code"]})
        check("mint on expired parent is 410", late.status_code == 410, late.text)
        check("expired parent still 410", client.get(f"/api/melds/{stale['code']}").status_code == 410)
        check("mint after dissolve is 410", client.post(
            "/api/melds", json={"context": "still late", "prev_code": stale["code"]}
        ).status_code == 410)
        check("expired plaintext absent", "stale-body" not in late.text)
        gone = client.get(f"/api/melds/{parent['code']}")
        check("parent 410", gone.status_code == 410, gone.text)
        check("parent still 410", client.get(f"/api/melds/{parent['code']}").status_code == 410)
        check("parent plaintext absent", "parent-body" not in gone.text)
        child = client.get(f"/api/melds/{hop_body['code']}")
        check("child still live", child.status_code == 200, child.text)
        chain = client.get(f"/api/melds/{hop_body['code']}/chain")
        check("chain 200", chain.status_code == 200, chain.text)
        nodes = chain.json()["nodes"]
        check("chain drops dissolved parent", len(nodes) == 1 and nodes[0]["code"] == hop_body["code"])
        check("dissolved plaintext absent", "parent-body" not in chain.text and "stale-body" not in chain.text)

        clock["t"] = T0 + timedelta(minutes=30, hours=1, seconds=1)
        child_gone = client.get(f"/m/{hop_body['code']}")
        check("child 410", child_gone.status_code == 410, child_gone.text)
        child_again = client.get(f"/api/melds/{hop_body['code']}")
        check("child still 410", child_again.status_code == 410, child_again.text)
        check("child plaintext not kept", "child-body" not in child_again.text)
        check("child row deleted", hop_body["code"] not in server._melds)
        check(
            "child tombstone is code only",
            hop_body["code"] in server._tombstones and server._tombstones[hop_body["code"]] is None,
        )
        unknown = client.post("/api/melds", json={"context": "x", "prev_code": "missing-code"})
        check("mint on unknown is 404", unknown.status_code == 404, unknown.text)


def test_dissolved_stays_410_unknown_stays_404() -> None:
    """Purge and chain deletes must keep serving 410. Never-existed stays 404."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        purged = client.post("/api/melds", json={"context": "purge-me"}).json()
        client.get(f"/api/melds/{purged['code']}")  # start clock at T0
        clock["t"] = T0 + timedelta(hours=1, seconds=1)
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
        check(
            "chain on dissolved is 410",
            client.get(f"/api/melds/{purged['code']}/chain").status_code == 410,
        )
        check(
            "mint on dissolved is 410",
            client.post("/api/melds", json={"context": "x", "prev_code": purged["code"]}).status_code == 410,
        )
        live = client.get(f"/api/melds/{fresh.json()['code']}")
        check("live path unchanged", live.status_code == 200 and live.json()["context_a"] == "still-here")

        parent = client.post("/api/melds", json={"context": "chain-parent"}).json()
        client.get(f"/api/melds/{parent['code']}")  # start parent clock
        clock["t"] = T0 + timedelta(hours=1, minutes=20, seconds=1)
        child = client.post(
            "/api/melds",
            json={"context": "chain-child", "prev_code": parent["code"]},
        ).json()
        client.get(f"/api/melds/{child['code']}")  # start child clock at mint time
        clock["t"] = T0 + timedelta(hours=2, seconds=2)
        check("parent still stored before chain", parent["code"] in server._melds)
        chain = client.get(f"/api/melds/{child['code']}/chain")
        check("chain of live child", chain.status_code == 200, chain.text)
        check("chain drops dissolved plaintext", "chain-parent" not in chain.text)
        check("chain removed the row", parent["code"] not in server._melds)
        check("chain dissolve is 410", client.get(f"/m/{parent['code']}").status_code == 410)
        check("child still live", client.get(f"/m/{child['code']}").status_code == 200)
        missing = client.get("/api/melds/never-existed")
        check("never existed 404", missing.status_code == 404, missing.text)
        check("never existed capability 404", client.get("/m/never-existed").status_code == 404)
        check(
            "tombstones store no payload",
            all(value is None for value in server._tombstones.values())
            and purged["code"] in server._tombstones
            and parent["code"] in server._tombstones,
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
                client.get(f"/api/melds/{row['code']}")
            clock["t"] = T0 + timedelta(hours=1, seconds=1)
            check("oldest dissolve 410", client.get(f"/m/{first['code']}").status_code == 410)
            check("middle dissolve 410", client.get(f"/m/{second['code']}").status_code == 410)
            check("newest dissolve 410", client.get(f"/m/{third['code']}").status_code == 410)
            check("cap held", len(server._tombstones) == 2)
            check("oldest forgotten is 404", client.get(f"/api/melds/{first['code']}").status_code == 404)
            check("middle still 410", client.get(f"/m/{second['code']}").status_code == 410)
            check("newest still 410", client.get(f"/api/melds/{third['code']}").status_code == 410)
            check("never existed stays 404", client.get("/m/never-existed-xx").status_code == 404)
            check(
                "mint forgotten is 404",
                client.post("/api/melds", json={"context": "x", "prev_code": first["code"]}).status_code == 404,
            )
            check(
                "mint remembered is 410",
                client.post("/api/melds", json={"context": "x", "prev_code": second["code"]}).status_code == 410,
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


def test_clock_skips_preview_and_chain() -> None:
    """Body read or resolve starts the hour. Preview, HEAD, and chain do not."""
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        parent = client.post("/api/melds", json={"context": "parent-secret"}).json()
        child = client.post(
            "/api/melds",
            json={"context": "child-secret", "prev_code": parent["code"]},
        ).json()
        chain = client.get(f"/api/melds/{child['code']}/chain")
        check("chain before use", chain.status_code == 200, chain.text)
        nodes = chain.json()["nodes"]
        check("chain lists both hops", {node["code"] for node in nodes} == {parent["code"], child["code"]})
        for node in nodes:
            check(
                "chain metadata only",
                node["expires_at"] is None
                and node["seconds_remaining"] is None
                and node["has_reply"] is False
                and node["resolved"] is False
                and "context_a" not in node
                and "context_b" not in node,
                str(node),
            )
        check("chain has no plaintext", "parent-secret" not in chain.text and "child-secret" not in chain.text)
        check("chain left parent dormant", server._melds[parent["code"]]["expires_at"] is None)
        check("chain left child dormant", server._melds[child["code"]]["expires_at"] is None)

        preview = client.get(
            f"/m/{parent['code']}",
            headers={
                "User-Agent": "Slackbot-LinkExpanding 1.0",
                "Accept": "application/json",
            },
        )
        check("slack preview", preview.status_code == 200, preview.text)
        check("slack is html", "text/html" in preview.headers["content-type"], preview.headers["content-type"])
        check("slack card", "this bridge expires" in preview.text and "not included in this preview" in preview.text)
        check("slack has no script", "<script" not in preview.text.lower())
        check("slack has no plaintext", "parent-secret" not in preview.text and parent["code"] not in preview.text)
        check("slack left the clock dormant", server._melds[parent["code"]]["expires_at"] is None)

        head = client.head(f"/m/{child['code']}")
        check("head is not a body read", head.status_code == 405, head.text)
        check("head left the clock dormant", server._melds[child["code"]]["expires_at"] is None)
        api_head = client.head(f"/api/melds/{parent['code']}")
        check("api head is not a body read", api_head.status_code == 405, api_head.text)
        check("api head left the clock dormant", server._melds[parent["code"]]["expires_at"] is None)

        clock["t"] = T0 + timedelta(minutes=25)
        discord = client.get(
            f"/m/{child['code']}",
            headers={"User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0)"},
        )
        check("discord preview", discord.status_code == 200 and "child-secret" not in discord.text, discord.text)
        check("discord left the clock dormant", server._melds[child["code"]]["expires_at"] is None)

        opened = client.get(
            f"/api/melds/{parent['code']}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        check("browser body read", opened.status_code == 200 and opened.json()["context_a"] == "parent-secret")
        opened_exp = datetime.fromisoformat(opened.json()["expires_at"])
        check(
            "clock starts at the body read",
            opened_exp == clock["t"] + timedelta(seconds=server.HOUR_SECONDS),
            opened.json()["expires_at"],
        )
        check("sibling still dormant", server._melds[child["code"]]["expires_at"] is None)
        again = client.get(f"/api/melds/{child['code']}/chain")
        by_code = {node["code"]: node for node in again.json()["nodes"]}
        check("chain still hides plaintext", "parent-secret" not in again.text and "child-secret" not in again.text)
        check("chain shows the started hop", by_code[parent["code"]]["expires_at"] == opened.json()["expires_at"])
        check("chain does not start the sibling", by_code[child["code"]]["expires_at"] is None)
        check("sibling row still dormant", server._melds[child["code"]]["expires_at"] is None)

        clock["t"] = T0 + timedelta(minutes=40)
        resolved = client.post(f"/api/melds/{child['code']}/resolve", json={"context": "child-reply"})
        check("resolve starts a dormant hop", resolved.status_code == 200, resolved.text)
        child_exp = datetime.fromisoformat(resolved.json()["expires_at"])
        check(
            "resolve clock is its own hour",
            child_exp == clock["t"] + timedelta(seconds=server.HOUR_SECONDS),
            resolved.json()["expires_at"],
        )
        check("resolve did not move the parent", server._melds[parent["code"]]["expires_at"] == opened_exp)
        meta = client.get(f"/api/melds/{child['code']}/chain").json()
        child_node = next(node for node in meta["nodes"] if node["code"] == child["code"])
        check("chain reports the reply without the text", child_node["has_reply"] is True and child_node["resolved"] is True)
        check("reply text stays off the chain", "child-reply" not in str(meta))


def test_preview_does_not_flip_410_or_404() -> None:
    clock = {"t": T0}
    reset(clock)
    with TestClient(server.app) as client:
        row = client.post("/api/melds", json={"context": "gone-secret"}).json()
        client.get(f"/api/melds/{row['code']}")
        clock["t"] = T0 + timedelta(hours=1, seconds=1)
        check("dissolve on read", client.get(f"/api/melds/{row['code']}").status_code == 410)
        preview = client.get(f"/m/{row['code']}", headers={"User-Agent": "Twitterbot/1.0"})
        check("preview of a tombstone", preview.status_code == 200 and "gone-secret" not in preview.text, preview.text)
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
    check("trust host-readable", "Host-readable while live." in trust)
    check("trust not a vault", "not a vault" in trust)
    check("trust tombstone cap", str(server.TOMBSTONE_CAP) in trust)
    check("trust distinguishes gone", "410" in trust and "404" in trust)
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
    test_dissolve_and_mint_next()
    test_dissolved_stays_410_unknown_stays_404()
    test_tombstone_cap_drops_oldest()
    test_clock_skips_preview_and_chain()
    test_preview_does_not_flip_410_or_404()
    test_surface()
    test_public_tree()
    print("all passed")


if __name__ == "__main__":
    main()
