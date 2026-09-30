# meld — ephemeral context bridge

**Don't meet. Meld.**

meld puts the context on a capability URL so neither side has to paste the block. The URL is the shared bearer for the exchange. Each link lives 1 hour, then the host serves 410.

One hour on this link. Mint-next starts another link with its own hour. That is not an extend. Expired hops are gone. No accounts, and no archive of dissolved links.

## Live

https://meld.mergeinc.workers.dev

Production Worker `meld` is the free pilot. This branch does not deploy it. x402 testing uses a separate preview Worker. Steps: [E2E_RUNBOOK.md](E2E_RUNBOOK.md).

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

Capability URL. The host is readable while live, and anyone with the link can read it. Not for secrets/credentials/regulated. Each link lives 1 hour. Omit `ttl` or send `1hr`. Mint-next (`prev_code`) creates a new URL with its own hour. That is not an extend. Link previews of `/m/{code}` are a generic card only (title “meld — this bridge expires”). The meld body is not placed in Open Graph or Twitter tags. Full statement: [TRUST.md](TRUST.md).

## API

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/melds` | POST | None | Create a meld and return its capability URL |
| `/api/melds/{code}` | GET | None | Read live context using the capability URL |
| `/api/melds/{code}/resolve` | POST | None (or PIN) | Resolve a meld |
| `/api/melds/{code}/result` | GET | Owner token | Legacy owner read |
| `/api/x402` | GET | None | Unpaid x402 probe (HTTP 402 once the payee secret is set) |
| `/api/x402` | POST | `PAYMENT-SIGNATURE` | Paid create after the facilitator settles |
| `/v1/melds` | POST | API key | Create via agent key |
| `/v1/usage` | GET | API key | Check usage |

## Pricing

Humans are free in the browser. Each link lives 1 hour (`ttl` omit or `1hr`; other values rejected). Mint-next (`prev_code`) starts a new one-hour link (not an extend). Agents get 3 creates per IP per hour; the next create returns HTTP 402 (x402 v2, USDC on Base, 3330000 atomic units, same $3.33 as Stripe) once preview secret `X402_PAY_TO` is set. Probe: `GET /api/x402`. Paid create: `POST /api/x402` with `PAYMENT-SIGNATURE`. Browser unlock stays Stripe Checkout. No subscriptions. Not for secrets.

Production Worker `meld` keeps the free pilot (no payment wall) until a human deploys something else. This branch does not do that. Preview curl steps and the secrets to set on a throwaway `meld-prev-*` Worker are in [E2E_RUNBOOK.md](E2E_RUNBOOK.md).
