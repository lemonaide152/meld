# meld — ephemeral context bridge

**Don't meet. Meld.**

Put working context on a capability URL so neither side pastes the block. The conversation stays on that link. The bridge stays open while the context exchange is active. Pilot TTL is **24h** from the last reply. The first reply starts it. Each later reply is kept and resets 24h. There is no maximum lifetime. It closes only after 24h with no new reply. The host then deletes it and serves **410 Gone** for that code while it remembers the dissolve. A code that never existed is **404**. Create stays dormant until the first reply. A read does not start or reset the timer.

Host-readable while live. Anyone with the link can read it. **Not for secrets.** No accounts, no plaintext archive.

**No AI in the loop.** The host only holds what you pour while the bridge is live — then it's gone. It does not summarize, rewrite, invent a reply, or run a model on the exchange.

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
| `/api/melds` | POST | Create. Body: `context`, optional `ttl` (`24h`). Leaves the bridge dormant. The conversation stays on the returned link. |
| `/api/melds/{code}` | GET | Plaintext while live: the opening `context_a` and every reply. Does not start or reset the timer. 404 unknown. 410 after 24h with no new reply, including later requests, while that code is still remembered. |
| `/m/{code}` | GET | Same plaintext read — the capability URL. Link-preview crawlers get an expires-only card with no exchange. That card does not read the meld. |
| `/api/melds/{code}/resolve` | POST | Append a reply on this same bridge. The first reply starts the pilot 24h. Each later reply is kept and resets 24h. 404 unknown. 410 if dissolved and still remembered. |

State is memory only. 24h with no new reply, the meld is deleted (no plaintext archive). The host keeps a tombstone of the code only, capped at 4096, and drops the oldest when that cap is full. Restart drops live links and tombstones. A forgotten code is not distinguishable from one that never existed, so the answer is 404.

## Trust

[TRUST.md](TRUST.md).
