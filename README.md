# meld — ephemeral context bridge

**Don't meet. Meld.**

Put working context on a capability URL. Neither side pastes the block. Party A creates the link. One note says what the exchange is for and what it is not for. A sends that URL to B privately. The conversation stays on that link.

The bridge stays open while the exchange is active. Until the first reply, the hop stays open **36 hours** from creation. The first reply sets a **24 hour** timer. Each later reply is kept. Each later reply resets that 24 hours. There is no maximum lifetime once replies have started. A read does not start the timer. A read does not reset the timer.

When the window ends, the host dissolves the meld. Dissolve deletes the bridge. The next request for that code is **404**. A code that never existed is **404**. An expired code is **404**. The response is the same.

Host-readable while live. Anyone with the link can read it. **Not for secrets.** No accounts. No plaintext archive.

**No AI in the loop.** The host holds what you pour while the bridge is live. The host does not summarize the exchange. The host does not rewrite it. The host does not invent a reply. The host does not run a model on it.

Self-host is the path. This repository is the server, Caddy, and Docker setup.

## Get started

```bash
git clone https://github.com/lemonaide152/meld.git && cd meld && docker build -t meld . && docker run --rm -p 8080:8080 -e MELD_PUBLIC_URL=http://127.0.0.1:8080 meld
```

Then:

```bash
curl -s -X POST http://127.0.0.1:8080/api/melds \
  -H "Content-Type: application/json" \
  -d '{"note":"For a design review. Not for passwords or customer data."}'
```

A sends the `url` to B privately. B replies with `POST /api/melds/{code}/resolve`. Keep talking on that same link.

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
| `/api/melds` | POST | Creation. One note: what the exchange is for and what it is not for. Send `note`, or put that same text in `context`, `for`, and `not_for`. Open 36 hours from create until the first reply. A sends the URL to B privately. |
| `/api/melds/{code}` | GET | Plaintext while live: the note and every reply. Does not start or reset the timer. Unknown, dissolved, and expired are the same 404. |
| `/m/{code}` | GET | Same plaintext read. This is the capability URL. Link-preview crawlers get an expires-only card with no exchange. That card does not read the meld. |
| `/api/melds/{code}/resolve` | POST | Append a reply on this same bridge. Keep talking on that link. The first reply sets a 24 hour timer. Each later reply is kept and resets that 24 hours. Unknown, dissolved, and expired are the same 404. |

A client that still posts the older keys uses one note, the same text in each field:

```json
{"context":"For a design review. Not for passwords or customer data.","for":"For a design review. Not for passwords or customer data.","not_for":"For a design review. Not for passwords or customer data."}
```

The read repeats that note in `note`, `context_a`, `for`, and `not_for`.

State is memory only. With no reply, 36 hours from creation deletes the meld. After a reply, 24 hours with no new reply deletes it. Dissolve removes the bridge. The server does not keep a record of that code. The next request is 404. A restart drops live links.

## Trust

[TRUST.md](TRUST.md).
