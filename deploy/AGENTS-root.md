# AGENTS.md — working with meld

This file follows the AGENTS.md convention: instructions for AI agents
working in or around this system.

## What meld is

meld puts the context on a URL so neither side has to paste the block. Then the URL dies.

An ephemeral two-party context bridge. Party A creates one share URL; party B
opens it and answers; either side can read both contexts via
`GET /api/melds/{code}` after resolve; then the host serves 410 and the meld
is gone (1h unresolved max, ~10min after resolution). No accounts. The URL
is the capability.

## When to use it

- You must hand context to another agent or a human exactly once, and no
  shared store exists.
- You need one answer back, not a thread.
- Optional: encrypt client-side before create if the server must not see
  plaintext.
- Playbooks: FDE institutional-knowledge gather; provider-switch dump-and-read
  or request-meld; TTL continuation by embedding the next meld URL — see /recipes.md.

## When NOT to use it

- Multi-turn conversations or anything needing history.
- Anything that must outlive the TTL (unless you chain melds).
- Repeated structured access by many consumers — use a real store.

## Quick start (preferred: one URL)

```bash
# A creates — share only the url (agents: declare client)
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' \
  -H 'X-Meld-Client: agent' \
  -d '{"context":"..."}'
# → {code, url, owner_url, owner_token, expires_at}
#   owner_token is legacy (still returned); prefer the share url alone.

# B answers
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \
  -H 'content-type: application/json' \
  -d '{"context":"..."}'

# A (or anyone with the code) reads both sides after resolve
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
# → {context_a, context_b, resolved: true, …}
```

### Legacy owner read (still on the live host)

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \
  -H 'X-Meld-Token: {owner_token}'
# Token rotates every read if you use this path.
```

## Agent-to-agent pattern

If you are agent A and your counterpart is agent B: send B the share link
plus this URL (https://meld.mergeinc.workers.dev/llms.txt) so B can
self-serve without human setup. If you are agent B and arrived here from a
share link: fetch /llms.txt, resolve, done.

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
MCP server manifest: /.well-known/mcp.json
