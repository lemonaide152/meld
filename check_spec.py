"""Fail the build when code, generated docs, OpenAPI, or schema drift from SPEC.md.

    python check_spec.py            # check (CI)
    python check_spec.py --write    # regenerate the committed documents
    python check_spec.py --live URL # validate a running server against OpenAPI

Needs fastapi, httpx, jsonschema.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import jsonschema

import meld_docs
import meld_spec

ROOT = Path(__file__).resolve().parent
errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def spec_block() -> dict:
    text = (ROOT / "SPEC.md").read_text()
    m = re.search(r"```meld-spec\n(.*?)```", text, re.S)
    if not m:
        err("SPEC.md has no meld-spec block")
        return {}
    out = {}
    for line in m.group(1).strip().splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def lst(value: str) -> tuple:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def check_constants(block: dict) -> None:
    pairs = {
        "open_hours": (int, meld_spec.OPEN_HOURS),
        "idle_hours": (int, meld_spec.IDLE_HOURS),
        "max_chars": (int, meld_spec.MAX_CHARS),
        "reply_cap": (int, meld_spec.REPLY_CAP),
        "sweep_minutes": (int, meld_spec.SWEEP_MINUTES),
        "not_found_status": (int, meld_spec.NOT_FOUND_STATUS),
        "not_found_body": (json.loads, meld_spec.NOT_FOUND_BODY),
        "create_fields": (lst, meld_spec.CREATE_FIELDS),
        "legacy_fields": (lst, meld_spec.LEGACY_FIELDS),
        "rejected_fields": (lst, meld_spec.REJECTED_FIELDS),
        "reply_field": (str, meld_spec.REPLY_FIELD),
        "mcp_tools": (lst, meld_spec.MCP_TOOLS),
    }
    for key, (conv, actual) in pairs.items():
        if key not in block:
            err(f"SPEC.md meld-spec block is missing {key}")
            continue
        if conv(block[key]) != actual:
            err(f"drift: SPEC.md {key}={block[key]!r} but code has {actual!r}")
    if meld_spec.code_bits() < int(block.get("min_code_bits", "128")):
        err(f"code entropy {meld_spec.code_bits()} bits is under {block.get('min_code_bits')}")
    if meld_spec.OPEN_SECONDS != meld_spec.OPEN_HOURS * 3600 or meld_spec.IDLE_SECONDS != meld_spec.IDLE_HOURS * 3600:
        err("OPEN_SECONDS / IDLE_SECONDS do not match the hours")
    wrangler = (ROOT / "wrangler.toml.example").read_text()
    want = f'crons = ["*/{meld_spec.SWEEP_MINUTES} * * * *"]'
    if want not in wrangler:
        err(f"wrangler.toml.example must schedule the sweep as {want}")


def check_schema(block: dict) -> None:
    sql = (ROOT / "schema.sql").read_text()
    for table in ("melds", "replies"):
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.S)
        if not m:
            err(f"schema.sql has no {table} table")
            continue
        cols = tuple(line.strip().split()[0] for line in m.group(1).strip().splitlines()
                     if line.strip() and not line.strip().startswith("--"))
        if cols != lst(block.get(f"{table}_columns", "")):
            err(f"drift: schema.sql {table} columns {cols} != SPEC.md")
    code_only = "\n".join(l.split("--")[0] for l in sql.splitlines())
    for banned in ("owner_token", "email", "learn", "pin"):
        if re.search(rf"\b{banned}\b", code_only):
            err(f"schema.sql mentions {banned}")


def check_generated(write: bool) -> None:
    for name, gen in meld_docs.GENERATED.items():
        want = gen()
        path = ROOT / name
        if write:
            path.write_text(want)
            continue
        if not path.exists() or path.read_text() != want:
            err(f"drift: {name} is not what meld_docs generates (run python check_spec.py --write)")
    rule_bits = (f"{meld_spec.OPEN_HOURS} hours", f"{meld_spec.IDLE_HOURS} hours")
    for name in ("llms.txt", "agents.md", "skill.md", "TRUST.md"):
        text = meld_docs.GENERATED[name]()
        for bit in rule_bits:
            if bit not in text:
                err(f"{name} does not state {bit}")
        if "Not for secrets" not in text:
            err(f"{name} does not say Not for secrets")
    for name in ("llms.txt", "agents.md", "skill.md"):
        if meld_docs.USES not in meld_docs.GENERATED[name]():
            err(f"{name} does not list exactly the two uses")
    required = (f"{meld_spec.MAX_CHARS:,}", f"{meld_spec.REPLY_CAP} replies", "error 1010",
                *(f"`{f}`" for f in meld_spec.REJECTED_FIELDS))
    for name in ("llms.txt", "agents.md", "skill.md"):
        for memory_only in (True, False):
            text = meld_docs.GENERATED[name](memory_only)
            for bit in required:
                if bit not in text:
                    err(f"{name} does not document {bit}")
    for name in ("llms.txt", "agents.md"):
        if "Time Travel" not in meld_docs.GENERATED[name](False):
            err(f"{name} (stored variant) does not state the backup window")
        if re.search(r"Time Travel|D1", meld_docs.GENERATED[name](True)):
            err(f"{name} (memory-only variant) mentions a store it does not use")
    banned = re.compile(r"(?i)\blearn\b|\bmessages\b|per-minute|rate limit|upgrade\.md|stripe|checkout|/chain|/v1/|/pro\b|owner_token")
    for name, gen in meld_docs.GENERATED.items():
        hit = banned.search(gen())
        if hit:
            err(f"{name} mentions {hit.group(0)!r}, which the code does not do")
    if meld_docs.BACKUP_LINE.count(". ") > 0:
        err("the backup window must be one sentence")
    mem = meld_docs.trust_md(True)
    if meld_docs.MEMORY_LINE not in mem or meld_docs.MEMORY_CLOSING not in mem or "Time Travel" in mem:
        err("memory-only TRUST.md must say memory-only and nothing written to disk, with no backup line")
    if "disk" in meld_docs.trust_md(False) or "memory only" in meld_docs.trust_md(False):
        err("stored TRUST.md must not claim memory-only")
    if "Time Travel" not in meld_docs.trust_md(False):
        err("TRUST.md does not state the D1 Time Travel window")


def _walk_schemas(node, where: str) -> None:
    if isinstance(node, dict):
        if "schema" in node:
            s = node["schema"]
            if not isinstance(s, dict) or not s:
                err(f"empty schema at {where}")
        for k, v in node.items():
            _walk_schemas(v, f"{where}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_schemas(v, f"{where}/{i}")


def check_openapi(block: dict) -> dict:
    doc = meld_docs.openapi()
    _walk_schemas(doc["paths"], "#/paths")
    for name, schema in doc["components"]["schemas"].items():
        if not schema.get("properties"):
            err(f"component {name} has no properties")
    ops = []
    for path, item in doc["paths"].items():
        for method, op in item.items():
            ops.append(f"{method.upper()} {path}")
            for status, resp in op["responses"].items():
                if not resp.get("content"):
                    err(f"{method.upper()} {path} {status} has no content schema")
            if method == "post" and "requestBody" not in op:
                err(f"POST {path} has no requestBody")
            if "{code}" in path and "404" not in op["responses"]:
                err(f"{method.upper()} {path} does not document the 404")
    if set(ops) != set(lst(block.get("routes", ""))):
        err(f"drift: OpenAPI routes {sorted(ops)} != SPEC.md routes")
    nf = doc["components"]["schemas"]["NotFound"]["properties"]["detail"].get("const")
    if nf != meld_spec.NOT_FOUND_BODY["detail"]:
        err("OpenAPI NotFound body does not match SPEC.md")
    tools = [t["name"] for t in meld_docs.MCP_TOOL_LIST]
    if tuple(tools) != meld_spec.MCP_TOOLS:
        err(f"MCP tools {tools} != SPEC.md")
    for t in meld_docs.MCP_TOOL_LIST:
        props = t["inputSchema"].get("properties", {})
        for banned in ("ttl", "token", "owner_token", "learn"):
            if banned in props:
                err(f"MCP tool {t['name']} has a {banned} argument")
    return doc


def check_source() -> None:
    src = (ROOT / "meld_app.py").read_text() + (ROOT / "meld_store.py").read_text()
    for pattern, why in ((r"\b410\b", "a 410"), (r"\b429\b", "a 429"), (r"owner_token", "an owner token"),
                         (r"/dissolve", "a dissolve endpoint"), (r"datetime\.isoformat|\.isoformat\(", "a second timestamp format")):
        if re.search(pattern, src):
            err(f"source has {why}")


TS_FIELDS = ("created_at", "expires_at")


def _check_ts(obj, where: str) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in TS_FIELDS:
                if not isinstance(v, str) or not meld_spec.TS_RE.match(v):
                    err(f"timestamp {where}.{k}={v!r} is not UTC ISO 8601 with ms and Z")
            _check_ts(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_ts(v, f"{where}[{i}]")


def validator(doc: dict, name: str):
    schema = {"$ref": f"#/components/schemas/{name}", "components": doc["components"]}
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def validate(doc, name, data, where) -> None:
    for e in validator(doc, name).iter_errors(data):
        err(f"{where}: response does not match {name}: {e.message}")
    _check_ts(data, where)


def exercise(client, doc: dict, prefix: str = "") -> None:
    """Drive a server through create, read, reply, MCP, and 404; validate every body."""
    r = client.post(f"{prefix}/api/melds", json={"note": "check_spec probe. Not a secret."},
                    headers={"x-meld-surface": "api"})
    if r.status_code != 200:
        err(f"create returned {r.status_code}")
        return
    validate(doc, "CreateResponse", r.json(), "create")
    code = r.json()["code"]
    r = client.get(f"{prefix}/api/melds/{code}")
    validate(doc, "Meld", r.json(), "read")
    r = client.post(f"{prefix}/api/melds/{code}/resolve", json={"context": "check_spec reply"})
    validate(doc, "Meld", r.json(), "reply")
    r = client.get(f"{prefix}/m/{code}", headers={"accept": "application/json"})
    validate(doc, "Meld", r.json(), "capability")
    seen = []
    for path in (f"/api/melds/{'x' * 32}", f"/m/{'y' * 32}"):
        r = client.get(f"{prefix}{path}", headers={"accept": "application/json"})
        if r.status_code != 404:
            err(f"{path} returned {r.status_code}, not 404")
        validate(doc, "NotFound", r.json(), path)
        seen.append(r.content)
    r = client.post(f"{prefix}/api/melds/{'z' * 32}/resolve", json={"context": "x"})
    seen.append(r.content)
    if len(set(seen)) != 1:
        err("not-found bodies differ")
    r = client.post(f"{prefix}/api/melds", json={"note": "a", "ttl": "1h"})
    if r.status_code != 400:
        err(f"create with ttl returned {r.status_code}")
    validate(doc, "Error", r.json(), "create 400")
    r = client.get(f"{prefix}/health")
    validate(doc, "Health", r.json(), "health")
    r = client.post(f"{prefix}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    if tuple(names) != meld_spec.MCP_TOOLS:
        err(f"live MCP tools {names}")
    r = client.post(f"{prefix}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                            "params": {"name": "meld_read", "arguments": {"code": code}}})
    data = dict(r.json()["result"]["structuredContent"])
    data.pop("untrusted_content", None)
    validate(doc, "Meld", data, "mcp meld_read")


def main(argv) -> int:
    write = "--write" in argv
    block = spec_block()
    check_constants(block)
    check_schema(block)
    check_generated(write)
    doc = check_openapi(block)
    check_source()
    if "--live" in argv:
        import httpx
        base = argv[argv.index("--live") + 1].rstrip("/")
        with httpx.Client(base_url=base, timeout=20, headers={"user-agent": "meld-check-spec/1.0"}) as c:
            exercise(c, doc)
    else:
        from fastapi.testclient import TestClient
        from meld_app import build_app
        from meld_store import MemoryStore
        exercise(TestClient(build_app(MemoryStore())), doc)
    if errors:
        for e in errors:
            print("FAIL", e)
        return 1
    print("ok: SPEC.md, code, schema, generated docs, OpenAPI, and live responses agree")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
