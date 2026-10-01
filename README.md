# meld — ephemeral context bridge

**Don't meet. Meld.**

Put working context on a capability URL so neither side pastes the block. Each link lives **1 hour**, then the host serves 410. Mint-next starts a **new** link with its own hour (not an extend).

Host-readable while live. Anyone with the link can read it. **Not for secrets.** No accounts, no archive.

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
| `/api/melds` | POST | Create. Body: `context`, optional `ttl` (`1hr`), optional `prev_code`. |
| `/api/melds/{code}` | GET | Read live meld. 404 unknown. 410 after the hour (row deleted). |
| `/m/{code}` | GET | Same read — the capability URL. |
| `/api/melds/{code}/resolve` | POST | Other side. Same answer may retry; different answer is 409. |
| `/api/melds/{code}/chain` | GET | Live hops in the thread. Dissolved plaintext is not returned. |

State is memory only. Restart drops live links.

## Trust

[TRUST.md](TRUST.md).
