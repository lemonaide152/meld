# meld — ephemeral context bridge

**Don't meet. Meld.**

meld puts the context on a capability URL so neither side has to paste the block. The URL is the bearer for the exchange. Each link lives 1 hour, then the host serves 410.

One hour on this link. Mint-next starts another link with its own hour. That is not an extend. Expired hops are gone. No accounts, and no archive of dissolved links.

Host-readable while live. Anyone with the link can read it. Not for secrets, credentials, or regulated data. This is not a private room and not a vault.

A hosted try-now exists at https://meld.mergeinc.workers.dev.

## Quick start

### Python

```bash
pip install -r requirements.txt
python server.py
```

The server listens on `0.0.0.0:8080`. Override the port with `PORT`.

```bash
curl -s -X POST http://127.0.0.1:8080/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "Auth flow: OAuth2+PKCE, JWT tokens, refresh rotation"}'

curl -s -X POST http://127.0.0.1:8080/api/melds/CODE/resolve \
  -H "Content-Type: application/json" \
  -d '{"context": "Add rate limiting to token refresh"}'

curl -s http://127.0.0.1:8080/api/melds/CODE
```

`GET /m/{code}` is the same read. That path is the capability URL returned as `url`.

### Docker and Caddy

Caddy is the reverse proxy and obtains TLS for a public hostname. The meld process stays on the compose network.

```bash
cp .env.example .env
# Set MELD_SITE to your DNS name and MELD_PUBLIC_URL to https://that-name
docker compose up -d --build
```

Point DNS at the machine and open ports 80 and 443. Caddy requests a certificate for `MELD_SITE`. Share links use `MELD_PUBLIC_URL`.

For a local trial with no domain, set `MELD_SITE=:80` and `MELD_PUBLIC_URL=http://localhost`, then open http://localhost.

```bash
curl -s -X POST https://meld.example.com/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "Auth flow: OAuth2+PKCE, JWT tokens, refresh rotation"}'

curl -s -X POST https://meld.example.com/api/melds/CODE/resolve \
  -H "Content-Type: application/json" \
  -d '{"context": "Add rate limiting to token refresh"}'

curl -s https://meld.example.com/api/melds/CODE
```

Mint-next:

```bash
curl -s -X POST https://meld.example.com/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "Next hop", "prev_code": "CODE"}'
```

The response is a new code and a new `expires_at`. The previous link keeps the hour it already had.

Omit `ttl` or send `1hr`. Any other lifetime is rejected.

## Publish an image

Suggested name: `ghcr.io/lemonaide152/meld`. No registry credentials belong in this repo. `docker login` uses your own GitHub username and a token with `write:packages`.

```bash
docker build -t ghcr.io/lemonaide152/meld:latest .
docker login ghcr.io
docker push ghcr.io/lemonaide152/meld:latest
```

`docker compose build` tags the local image with that name. After a push, another machine can `docker compose pull` and `docker compose up -d`.

## API

| Endpoint | Method | Description |
|---|---|---|
| `/api/melds` | POST | Create a meld. Body: `context`, optional `ttl` (`1hr`), optional `prev_code`. |
| `/api/melds/{code}` | GET | Read a live meld. 404 if unknown. 410 when the hour is over (the row is deleted). |
| `/m/{code}` | GET | Same read. This is the capability URL. |
| `/api/melds/{code}/resolve` | POST | Add the other side. The same answer may be retried. A different answer is 409. Resolve does not move `expires_at`. |
| `/api/melds/{code}/chain` | GET | Live hops that share this link's thread. Dissolved plaintext is not returned. |

State is memory only. Restarting the process drops live links. Dissolved links are deleted.

## Trust

[TRUST.md](TRUST.md).
