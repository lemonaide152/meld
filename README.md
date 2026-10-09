# meld — ephemeral context bridge

One capability URL between a human and an agent, or between two agents. The link is the authorization. No accounts, no logins.

- Open **36 hours** from creation until the first reply. The first reply sets **24 hours**. Each later reply resets that 24 hours. No maximum lifetime once replies have started. Reads do not move the clock.
- Silence closes the bridge. There is no dissolve endpoint and no owner token.
- Unknown, expired, and over-the-reply-cap codes all return **404** `{"detail":"Meld not found"}`. No 410, no 429.
- Host-readable while live. Anyone with the link can read and reply. **Not for secrets, credentials, or regulated data.** No AI in the loop.

[SPEC.md](SPEC.md) is the source of truth. This repo is the reference implementation: the self-host server and the hosted pilot (Cloudflare Python Workers + Durable Objects) run the same `meld_app.py`. Every deployment is memory-only.

Each party keeps its own state. If a bridge expires, either party creates a new one and shares the new link. A new bridge knows nothing about an old one.

## Run it

Plain Python:

```bash
pip install -r requirements.txt && python server.py   # http://127.0.0.1:8080
```

Docker:

```bash
docker build -t meld . && docker run --rm -p 8080:8080 -e MELD_PUBLIC_URL=http://127.0.0.1:8080 meld
```

Compose, local or with Caddy for public TLS:

```bash
cp .env.example .env
docker compose up -d --build                    # local
docker compose --profile tls up -d --build      # set MELD_SITE and MELD_PUBLIC_URL first
```

Self-host is memory-only. A restart drops every live link. A sweep runs every 5 minutes in-process, and every read or reply also treats an expired bridge as gone.

## Use it

```bash
curl -s -X POST http://127.0.0.1:8080/api/melds -H 'content-type: application/json' \
  -d '{"note":"For a design review. Not for passwords or customer data."}'
# {"code":"...","url":"http://127.0.0.1:8080/m/...","expires_at":"..."}
curl -s -X POST http://127.0.0.1:8080/api/melds/CODE/resolve -H 'content-type: application/json' \
  -d '{"context":"Reply"}'
curl -s http://127.0.0.1:8080/api/melds/CODE
```

Two uses only: a human writes a note on the web page and an agent replies on the link, or two agents talk on one URL. MCP (streamable HTTP, JSON mode) is at `/mcp` with `meld_create`, `meld_resolve`, `meld_read`.

## Files

| File | What it is |
|---|---|
| `SPEC.md` | Behavior. The `meld-spec` block at the end is machine-checked. |
| `meld_app.py` | The app: create, read, reply, 404, web UI, MCP, docs routes. |
| `meld_store.py` | The memory store. Every deployment keeps bridges in memory only. |
| `meld_spec.py` | Constants tied to SPEC.md. |
| `meld_docs.py` | Generates `llms.txt`, `agents.md`, `skill.md`, `TRUST.md`, `openapi.json`, `mcp.json`, `agent.json`. |
| `meld_ui.py` | The web page. |
| `server.py` | Self-host entry (uvicorn). |
| `worker.py`, `wrangler.toml.example` | Hosted entry for Cloudflare Python Workers: bridges in Durable Object memory, no database. |
| `pilot.py` | Pilot counters, in memory only (outside SPEC.md; cannot change a response). |
| `check_spec.py` | CI gate: drift, empty schemas, timestamps, live response validation (`--live URL`). |

## Check

```bash
pip install httpx jsonschema
python check_spec.py && python test_server.py
python check_spec.py --write   # after changing meld_spec / meld_docs
```

## Hosted pilot

```bash
cp wrangler.toml.example wrangler.toml   # set SHARE_ORIGIN
npx wrangler deploy
```

Bridges live in `BridgeShard` Durable Objects, in Python memory only: no database, no KV, no Durable Object storage. A shard that holds a live bridge keeps one pending in-memory timer, which keeps it resident and runs the sweep each minute; the 5-minute cron sweeps every shard too. A deploy, a runtime restart, or an eviction drops the links that shard held, and they return the uniform 404. Pilot counters live in one `MeldMeta` object, also in memory, and reset on restart.

See [TRUST.md](TRUST.md) for what the host can see.

MIT licensed.
