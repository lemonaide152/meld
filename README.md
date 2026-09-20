# meld — ephemeral context bridge

**Don't meet. Meld.**

One link. Pour in context. Done.

meld is a minimal context-sharing primitive for humans and AI agents. Create a link, share it, each party provides their context, and when the exchange resolves the link dissolves. No history, no threads, no accounts.

## Live

https://meld.lemonaide152.workers.dev

## Quick start (agents)

```bash
# Create a meld
curl -X POST https://meld.lemonaide152.workers.dev/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "Auth flow: OAuth2+PKCE, JWT tokens, refresh rotation"}'
# → {"code": "abc123", "url": "https://…/m/abc123", "owner_token": "…"}

# Party B (human or agent) resolves:
curl -X POST https://meld.lemonaide152.workers.dev/api/melds/abc123/resolve \
  -H "Content-Type: application/json" \
  -d '{"context": "Looks good, but add rate limiting to token refresh"}'

# Read the merged result:
curl https://meld.lemonaide152.workers.dev/api/melds/abc123/result \
  -H "X-Meld-Token: <owner_token>"
```

## Trust model

No accounts. Authority comes from held secrets, never from identity.

- **Capability-based**: the code admits, the PIN authenticates, the token reads
- **E2E encrypted** (optional): AES-256-GCM in the browser, key in `#k=` fragment, server stores only ciphertext
- **Rotating tokens**: a leaked owner token dies on the next read
- **Ephemeral**: melds expire (1 hour standard, 10 minutes after resolution)
- **Leased pro status**: 35-day leases, append-only audit ledger, emails hashed at rest

Full statement: [TRUST.md](TRUST.md)

## API

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/melds` | POST | None | Create a meld |
| `/api/melds/{code}` | GET | None | View a meld |
| `/api/melds/{code}/resolve` | POST | None (or PIN) | Resolve a meld |
| `/api/melds/{code}/result` | GET | Owner token | Read the merged result |
| `/v1/melds` | POST | API key | Create (agent tier, 10K limit) |
| `/v1/usage` | GET | API key | Check usage |

## Pricing

| Tier | Price | Limits |
|---|---|---|
| Free | $0 | 3 melds/hour |
| Pro | $5/mo | Unlimited melds |
| Agent | $20/mo | 10,000 API melds |
