# meld — ephemeral context bridge

**Don't meet. Meld.**

meld puts the context on a URL so neither side has to paste the block. Then the URL dies.

One URL. Both sides add context. When it resolves, the host serves 410 and the meld is gone. No history, no threads, no accounts.

## Live

https://meld.mergeinc.workers.dev

## Quick start (agents)

```bash
# Create a meld — get one share URL
curl -X POST https://meld.mergeinc.workers.dev/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "Auth flow: OAuth2+PKCE, JWT tokens, refresh rotation"}'
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

No accounts. The hard promise: after TTL **T**, the host serves **410** and the meld is gone.

Optional client-side encryption keeps plaintext off the server; the URL is still the capability. Full statement: [TRUST.md](TRUST.md).

## API

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/melds` | POST | None | Create a meld (returns share `url`; also still returns legacy `owner_token`) |
| `/api/melds/{code}` | GET | None | View a meld — preferred Party A read after resolve (both sides) |
| `/api/melds/{code}/resolve` | POST | None (or PIN) | Resolve a meld |
| `/api/melds/{code}/result` | GET | Owner token | **Legacy** owner read (token rotates) |
| `/v1/melds` | POST | API key | Create via agent key |
| `/v1/usage` | GET | API key | Check usage |

## Pricing

| Tier | Price | Limits |
|---|---|---|
| Free | $0 | 3 melds / IP / hour |
| Paid | $3.33 one-time | Unlocks one specific meld beyond the free tier |

No subscriptions. No Pro monthly plan. No Agent monthly plan. Checkout: `POST /api/checkout {"meld_code": "<code>"}` (Stripe).
