# meld — ephemeral context bridge

**Don't meet. Meld.**

Put working context on a capability URL so neither side pastes the block. Each hop lives **1 hour after the first plaintext read or resolve**, then the host deletes it and serves **410 Gone** for that code while it remembers the dissolve. A code that never existed is **404**. Create stays dormant until that first use. A link-preview crawl does not start the clock. Mint-next starts a **new** link with its own hour (not an extend).

Host-readable while live. Anyone with the link can read it. **Not for secrets.** No accounts, no plaintext archive.

**No AI in the loop.** The host only holds what you pour while the hop is live — then it's gone. It does not summarize, rewrite, invent a reply, or run a model on the exchange.

Hosted try-now: https://meld.mergeinc.workers.dev

## Get started

```bash
git clone https://github.com/lemonaide152/meld.git && cd meld && docker build -t meld . && docker run --rm -p 8080:8080 -e MELD_PUBLIC_URL=http://127.0.0.1:8080 meld
```

Then:

```bash
curl -s -X POST http://127.0.0.1:8080/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context":"Auth flow: OAuth2+PKCE, JWT refresh"}'
```

Open the `url` from the response (or `GET /m/{code}`). Resolve with `POST /api/melds/{code}/resolve`.

### Compose (local)

```bash
cp .env.example .env
docker compose up -d --build
```

Same API on http://127.0.0.1:8080.

### Compose + Caddy (public TLS)

Set `MELD_SITE` and `MELD_PUBLIC_URL` in `.env`, point DNS at the machine, open 80/443:

```bash
docker compose --profile tls up -d --build
```

### Python (no Docker)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python server.py
```

## Publish an image

```bash
docker build -t ghcr.io/lemonaide152/meld:latest .
docker login ghcr.io
docker push ghcr.io/lemonaide152/meld:latest
```

## API

| Endpoint | Method | Description |
|---|---|---|
| `/api/melds` | POST | Create. Body: `context`, optional `ttl` (`1hr`), optional `prev_code`. Leaves the hop dormant. Unknown `prev_code` is 404. A dissolved `prev_code` is 410 while that code is still remembered. |
| `/api/melds/{code}` | GET | Plaintext of a live meld (`context_a` and `context_b`). Starts the hour on first use. 404 unknown. 410 after the hour, including later requests, while that code is still remembered. |
| `/m/{code}` | GET | Same plaintext read — the capability URL — and the same clock start. Link-preview crawlers get an expires-only card with no exchange; that does not read the meld and does not start the clock. |
| `/api/melds/{code}/resolve` | POST | Other side. Writes the reply, including when one is already stored. Starts the hour if it has not started. 404 unknown. 410 if dissolved and still remembered. |
| `/api/melds/{code}/chain` | GET | Metadata for live hops in the thread: code, link, whether a reply exists, expiry. No plaintext. Does not start the clock on this hop or its siblings. 404 unknown. 410 if this code has dissolved and is still remembered. |

State is memory only. After the hour the meld is deleted (no plaintext archive). The host keeps a tombstone of the code only, capped at 4096, and drops the oldest when that cap is full. Restart drops live links and tombstones. A forgotten code is not distinguishable from one that never existed, so the answer is 404.

## Trust

[TRUST.md](TRUST.md).
