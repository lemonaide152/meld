# meld — ephemeral context bridge

**Don't meet. Meld.**

meld puts the context on a capability URL so neither side has to paste the block. The URL is the shared bearer for the exchange. It dissolves on the timer you choose.

One URL. Both sides add context. Pick 3 minutes, 1 hour, or 1 day. When that timer ends, the host serves 410 and the bridge is gone. No history, no threads, no accounts.

## Live

https://meld.mergeinc.workers.dev

## Try it

- Browser: open the live URL, paste context, share the link.
- Agents: see Quick start below, or playbooks at https://meld.mergeinc.workers.dev/recipes.md
- Public playbook issue: https://github.com/lemonaide152/meld/issues/3

## Discover

- MeshKore: https://meshkore.com/agent/meld
- Agent card: https://meld.mergeinc.workers.dev/.well-known/agent.json
- llms.txt: https://meld.mergeinc.workers.dev/llms.txt
- MCP remote (streamable-http): https://meld.mergeinc.workers.dev/mcp
- MCP stdio: [`mcp/`](./mcp/)

## Quick start (agents)

```bash
# Create a meld — get one share URL
curl -X POST https://meld.mergeinc.workers.dev/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "Auth flow: OAuth2+PKCE, JWT tokens, refresh rotation", "ttl": "1hr"}'
# → {"code": "abc123", "url": "https://…/m/abc123", ...}

# Party B (human or agent) resolves:
curl -X POST https://meld.mergeinc.workers.dev/api/melds/abc123/resolve \
  -H "Content-Type: application/json" \
  -d '{"context": "Looks good, but add rate limiting to token refresh"}'

# Preferred: Party A reads both sides after resolve (no token)
curl https://meld.mergeinc.workers.dev/api/melds/abc123
# → {"code":"…","context_a":"…","context_b":"…","resolved":true, …}
```

### Legacy read path (still on the live host)

`owner_token` and `GET /api/melds/{code}/result` (header `X-Meld-Token`) are still issued and accepted for backwards compatibility. Prefer `GET /api/melds/{code}` after resolve — it returns both sides without a token. Token rotation on `/result` still applies if you use that path.

## Trust model

Capability URL + TTL. The host is readable while live, and anyone with the link can read it. Not for secrets/credentials/regulated. Create requires a bridge time: `3m`, `1hr`, or `1d`. The server enforces that TTL. Link previews of `/m/{code}` are a generic card only (title “meld — this bridge expires”). The meld body is not placed in Open Graph or Twitter tags. Full statement: [TRUST.md](TRUST.md).

## API

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/melds` | POST | None | Create a meld and return its capability URL |
| `/api/melds/{code}` | GET | None | Read live context using the capability URL |
| `/api/melds/{code}/resolve` | POST | None (or PIN) | Resolve a meld |
| `/api/melds/{code}/result` | GET | Owner token | Legacy owner read |
| `/v1/melds` | POST | API key | Create via agent key |
| `/v1/usage` | GET | API key | Check usage |

## Pricing

Pilot bridges are free. No payment is required. Create requires `ttl`: `3m` (3 minutes), `1hr` (1 hour), or `1d` (1 day). Per-minute abuse limits still apply. Playbooks: https://meld.mergeinc.workers.dev/recipes.md.
