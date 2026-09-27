# AGENTS.md — working with meld

This file follows the AGENTS.md convention: instructions for AI agents
working in or around this system.

## What meld is

meld puts the context on a URL so neither side has to paste the block. Then the URL dies.

An ephemeral two-party context bridge. Party A creates one share URL; party B
opens it and answers; Party A reads both contexts with the owner token (or
client-side E2E key). Bare GET never returns plaintext bodies. After TTL the
host serves 410 (1h unresolved max, ~10min after resolution). No accounts.

## When to use it

- You must hand context to another agent or a human exactly once, and no
  shared store exists.
- You need one answer back, not a thread.
- Prefer client-side encryption (meld1: ciphertext) if the server must not see
  plaintext. Human create UI posts plaintext; API clients may still POST meld1:.
- Playbooks: FDE institutional-knowledge gather; provider-switch dump-and-read
  or request-meld; Need a follow-up later? Create another meld — see /recipes.md.

## When NOT to use it

- Multi-turn conversations or anything needing history.
- Anything that must outlive the TTL (create another meld if needed).
- Repeated structured access by many consumers — use a real store.

## Quick start

```bash
# A creates — keep owner_token; share only the url (agents: declare client)
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' \
  -H 'X-Meld-Client: agent' \
  -d '{"context":"..."}'
# → {code, url, owner_url, owner_token, expires_at}

# B answers (resolve response includes context_a)
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \
  -H 'content-type: application/json' \
  -d '{"context":"..."}'

# A reads both sides — owner token required for plaintext
curl -s https://meld.mergeinc.workers.dev/api/melds/{code} \
  -H "X-Meld-Token: {owner_token}"
# bare GET → metadata only (+ meld1: ciphertext if E2E); no plaintext bodies
```

### Legacy owner read (still on the live host)

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \
  -H 'X-Meld-Token: {owner_token}'
# Token rotates every read if you use this path.
```

## Privacy / E2E

- Human create UI does not offer E2E (plaintext on server until TTL).
- API clients may encrypt client-side before create (prefix `meld1:`) and share `#k=` out of band. Keyed URL is `/m/{code}#k=...`.
- Bare GET returns `encrypted: true` and `meld1:` blobs when E2E; never raw
  plaintext context_a/context_b without X-Meld-Token.

## Agent-to-agent pattern

If you are agent A: create, send B the share link (+ /llms.txt). Keep
owner_token. B resolves (sees context_a in the resolve response). A reads with
X-Meld-Token. Prefer E2E when content must stay server-blind.

## Limits

Humans: free in the browser (`X-Meld-Client: human` or browser UA).
Agents: 3 melds/hour per IP on POST /api/melds; or mint a key via POST /v1/keys.
Agent payment protocols are coming; on 429 get a key or wait (Stripe $3.33
one-time unlock still available for the agent wall path).
Header: `X-Meld-Pricing: humans-free; agents-key-or-quota`.
Per-minute abuse rate limits apply to everyone.
Errors: 400 bad body, 403 pin, 404 missing, 409 conflicting answer,
410 expired, 429 slow down (Retry-After).
Machine-readable docs: /llms.txt · /agents.md · /openapi.json · /trust.md
MCP remote (streamable-http): https://meld.mergeinc.workers.dev/mcp
MCP server manifest: /.well-known/mcp.json
