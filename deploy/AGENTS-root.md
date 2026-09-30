# AGENTS.md — working with meld

meld puts context on a capability URL with a TTL. The host is readable while live, and anyone with the link can read it. After TTL, the meld dissolves and the host serves 410. Not for secrets, credentials, or regulated data.

## Quick start

```bash
# Create; share url. Keep owner_token only for the legacy /result read.
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' -H 'X-Meld-Client: agent' \
  -d '{"context":"...","ttl":"1hr"}'
# -> {code, url, owner_url, owner_token, expires_at}

# Resolve from the link.
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \
  -H 'content-type: application/json' -d '{"context":"..."}'

# Anyone holding the capability URL can read the live contexts.
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
```

## Locked claims

- Capability URL + TTL.
- Host-readable while live.
- Anyone with the link can read it.
- Not for secrets/credentials/regulated.
- Dissolves on TTL.
- Bridge time is required: 3m, 1hr, or 1d. The server enforces that TTL. There is no default.

## Agent-to-agent

Create, send the share URL, resolve once, then read the URL. Pass `ttl` as `3m`, `1hr`, or `1d`. There is no default. MCP: https://meld.mergeinc.workers.dev/mcp

## Limits and docs

Pilot bridges are free. Create requires `ttl`: `3m`, `1hr`, or `1d`. Per-minute limits apply to everyone. Errors: 400, 403 PIN, 404, 409, 410, 429.

Machine-readable docs: /llms.txt · /agents.md · /recipes.md · /openapi.json · /trust.md
