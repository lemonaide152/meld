# meld — ephemeral context bridge

**Don't meet. Meld.**

meld puts the context on a capability URL so neither side has to paste the block. The URL is the bearer for the exchange. Each link lives 1 hour, then the host serves 410.

One hour on this link. Mint-next starts another link with its own hour. That is not an extend. Expired hops are gone. No accounts, and no archive of dissolved links.

Host-readable while live. Anyone with the link can read it. Not for secrets, credentials, or regulated data.

## Live

https://meld.mergeinc.workers.dev

Open that URL to pour context and share the link.

## Try hosted (agents)

Optional. These curls talk to the live host above.

```bash
# Create a meld — one share URL
curl -s -X POST https://meld.mergeinc.workers.dev/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "Auth flow: OAuth2+PKCE, JWT tokens, refresh rotation", "ttl": "1hr"}'

# The other party resolves:
curl -s -X POST https://meld.mergeinc.workers.dev/api/melds/abc123/resolve \
  -H "Content-Type: application/json" \
  -d '{"context": "Looks good, but add rate limiting to token refresh"}'

# Anyone with the link reads both sides:
curl -s https://meld.mergeinc.workers.dev/api/melds/abc123
```

Omit `ttl` or send `1hr`. Any other lifetime is rejected. Mint-next sends `prev_code` set to a live code and receives a new URL with its own hour.

The live host also returns `owner_token` and still accepts `GET /api/melds/{code}/result` with header `X-Meld-Token`. Prefer `GET /api/melds/{code}` after resolve.

Link previews of `/m/{code}` on the live host are a generic card only (title “meld — this bridge expires”). The meld body is not placed in that preview.

## Self-host (base case)

This tree is the short path to your own meld server. It covers create, resolve, a fixed 1 hour dissolve (410 when the link is gone), and mint-next. State is memory only. Restarting the process drops live links. Dissolved links are deleted.

```bash
pip install -r requirements.txt
python server.py
```

The server listens on `0.0.0.0:8080`. Override the port with `PORT`. Set `MELD_PUBLIC_URL` (no trailing slash) when the share URL should use a public host.

```bash
curl -s -X POST http://127.0.0.1:8080/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "Auth flow: OAuth2+PKCE, JWT tokens, refresh rotation"}'

curl -s -X POST http://127.0.0.1:8080/api/melds/CODE/resolve \
  -H "Content-Type: application/json" \
  -d '{"context": "Add rate limiting to token refresh"}'

curl -s http://127.0.0.1:8080/api/melds/CODE
```

`GET /m/{code}` is the same read as `GET /api/melds/{code}`. That path is the capability URL returned as `url`.

Mint-next:

```bash
curl -s -X POST http://127.0.0.1:8080/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "Next hop", "prev_code": "CODE"}'
```

The response is a new code and a new `expires_at`. The previous link keeps the hour it already had.

### Container

```bash
docker build -t meld .
docker run --rm -p 8080:8080 meld
```

## API

| Endpoint | Method | Description |
|---|---|---|
| `/api/melds` | POST | Create a meld. Body: `context`, optional `ttl` (`1hr`), optional `prev_code`. |
| `/api/melds/{code}` | GET | Read a live meld. 404 if unknown. 410 when the hour is over (the row is deleted). |
| `/m/{code}` | GET | Same read. This is the capability URL. |
| `/api/melds/{code}/resolve` | POST | Add the other side. The same answer may be retried. A different answer is 409. Resolve does not move `expires_at`. |
| `/api/melds/{code}/chain` | GET | Live hops that share this link's thread. Dissolved plaintext is not returned. |

## Trust

[TRUST.md](TRUST.md).

## Optional Worker file

Running the server above does not use Cloudflare. [`deploy/wrangler.toml.example`](deploy/wrangler.toml.example) has placeholders only. Copy it to `wrangler.toml`, fill your own ids, and do not commit `wrangler.toml`.
