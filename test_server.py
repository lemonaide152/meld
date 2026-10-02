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
        check("capability url", body["url"].endswith(f"/m/{code}"))
        check("no owner token", "owner_token" not in body)
        check("prev empty on root", body["prev_code"] is None)
        same = client.get(f"/m/{code}")
        api = client.get(f"/api/melds/{code}")
        check("capability read", same.status_code == 200 and same.json()["context_a"] == "side a")
        check("api read matches", api.json() == same.json())
        check("unresolved body", api.json()["context_b"] is None and api.json()["resolved"] is False)
        resolved = client.post(f"/api/melds/{code}/resolve", json={"context": "side b"})
        check("resolve", resolved.status_code == 200 and resolved.json()["resolved"] is True, resolved.text)
        check("resolve keeps expiry", resolved.json()["expires_at"] == body["expires_at"])
        retry = client.post(f"/api/melds/{code}/resolve", json={"context": "side b"})
        check("same answer retries", retry.status_code == 200 and retry.json().get("retry") is True)
        clash = client.post(f"/api/melds/{code}/resolve", json={"context": "other"})
        check("different answer 409", clash.status_code == 409, clash.text)
        both = client.get(f"/api/melds/{code}").json()
        check("both sides", both["context_a"] == "side a" and both["context_b"] == "side b")


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
        parent_exp = parent["expires_at"]
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
        check("child has its own expiry", hop_body["expires_at"] != parent_exp)
        still = client.get(f"/api/melds/{parent['code']}").json()
        check("parent clock unchanged", still["expires_at"] == parent_exp)
        check("stale clock unchanged", client.get(f"/api/melds/{stale['code']}").json()["expires_at"] == stale["expires_at"])

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
        clock["t"] = T0 + timedelta(hours=1, minutes=20, seconds=1)
        child = client.post(
            "/api/melds",
            json={"context": "chain-child", "prev_code": parent["code"]},
        ).json()
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
    test_surface()
    test_public_tree()
    print("all passed")


if __name__ == "__main__":
    main()
