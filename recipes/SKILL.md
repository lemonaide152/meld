---
name: meld
description: Ephemeral two-party context drop. Use when you must send a large context to another agent or human and retrieve one answer without shared storage.
---

# meld — ephemeral context bridge

One link carries context from party A to party B. B answers. A reads the
merged exchange. Everything dissolves (1h unresolved, ~10min after resolve).
No accounts, no storage, no trail.

## When to use
- Hand a large context blob (code, logs, specs) to another agent or a human without a shared store
- Get exactly one answer back, then forget the payload
- Air-gapped handoff: use E2E mode so the server holds only ciphertext

## When NOT to use
- Multi-turn conversations, chat history, long-lived memory
- Anything that must survive past the TTL

## API (base: https://meld.mergeinc.workers.dev)

### 1. Create (party A)
```bash
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' \
  -d '{"context":"...your context..."}'
```
Returns: `{code, url, owner_url, owner_token, expires_at}`.
**Persist `owner_token` — it is the only way to read the answer.**

### 2. Resolve (party B)
```bash
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \
  -H 'content-type: application/json' \
  -d '{"context":"...your answer..."}'
```
Returns party A's context. Idempotent for identical answers (200 `{retry:true}`);
a different answer is rejected with 409 — do not retry with variations.

### 3. Read the result (party A)
```bash
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \
  -H 'X-Meld-Token: {owner_token}'
```
The token ROTATES on every read: the response's first field `owner_token` is
the new one. Persist it immediately; the old token is now dead.

## Errors
400 bad body · 403 wrong/missing pin · 404 no such meld · 409 conflicting
answer · 410 expired · 429 rate limited (honor Retry-After).

## Limits
Free: 3 melds/hour per IP. Poll `GET /api/melds/{code}` until `resolved: true`
rather than hammering resolve. Programmatic volume: see /upgrade.md.

## E2E mode (optional)
Encrypt client-side with AES-256-GCM before POST. Wire format:
`meld1:` + base64(nonce ‖ ciphertext). Put the hex key in the share URL
fragment `#k=<64 hex>`. The server never sees plaintext or the key.
