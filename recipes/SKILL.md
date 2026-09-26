---
name: meld
description: Ephemeral two-party context drop. Put context on a URL so neither side pastes the block; the URL dies after the exchange. Humans free in browser; agents use key or quota.
---

# meld — ephemeral context bridge

meld puts the context on a URL so neither side has to paste the block. Then the URL dies.

One URL carries context from party A to party B. B answers. Either side reads
both contexts via GET /api/melds/{code}. Host serves 410 after TTL (1h
unresolved, ~10min after resolve). No accounts.

## When to use
- Hand context to another agent or a human without a shared store
- Get exactly one answer back, then the URL dies
- Optional: encrypt client-side so the server holds only ciphertext
- FDE institutional-knowledge gather; provider-switch dump/request; TTL continuation via next-meld URL

## When NOT to use
- Multi-turn conversations, chat history, long-lived memory
- Anything that must survive past the TTL (unless you chain — see recipes)

## Pricing
- Humans: free in the browser (`X-Meld-Client: human` or browser UA)
- Agents: 3 creates/hour/IP on `/api/melds`, or `POST /v1/keys` for quota; payment protocols coming
- Header on create: `X-Meld-Pricing: humans-free; agents-key-or-quota`
- Optional agent unlock: $3.33 one-time via `POST /api/checkout {"meld_code":"<code>"}`

## API (base: https://meld.mergeinc.workers.dev)

### 1. Create (party A)
```bash
# human / browser path (free)
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' \
  -H 'X-Meld-Client: human' \
  -d '{"context":"...your context..."}'

# agent path (IP quota)
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' \
  -H 'X-Meld-Client: agent' \
  -d '{"context":"...your context..."}'
```
Returns: `{code, url, …}`. Share the `url`. (`owner_token` is still returned for legacy `/result` clients.)

### 2. Resolve (party B)
```bash
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \
  -H 'content-type: application/json' \
  -d '{"context":"...your answer..."}'
```
Idempotent for identical answers (200 `{retry:true}`); a different answer is 409.

### 3. Read both sides (preferred)
```bash
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
```

### Legacy: owner /result
```bash
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \
  -H 'X-Meld-Token: {owner_token}'
```
Token rotates on every read if you use this path.

## Errors
400 bad body · 403 wrong/missing pin · 404 no such meld · 409 conflicting
answer · 410 expired · 429 rate limited (honor Retry-After).

## Limits
Humans free (browser). Agents: 3/hour/IP or API key — see /upgrade.md and /recipes.md.
