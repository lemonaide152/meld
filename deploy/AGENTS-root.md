# AGENTS.md — working with meld

This file follows the AGENTS.md convention: instructions for AI agents
working in or around this system.

## What meld is

An ephemeral two-party context bridge. Party A creates a link containing
context; party B opens it, answers; party A reads the merged exchange; the
content is deleted (1h unresolved max, ~10min after resolution). No accounts.
The link is the capability.

## When to use it

- You must hand a large context blob (code, logs, specs) to another agent or
  a human exactly once, and no shared store exists.
- You need one answer back, not a thread.
- The context is sensitive enough that you don't want it persisted on a
  third party's server: use E2E mode (client-side AES-256-GCM, key stays in
  the URL fragment).

## When NOT to use it

- Multi-turn conversations or anything needing history.
- Anything that must outlive the TTL.
- Repeated structured access by many consumers — use a real store.

## Quick start (three calls)

```bash
# A creates
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' \
  -d '{"context":"..."}'
# → {code, url, owner_url, owner_token, expires_at}

# B answers (POST the share url's code)
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \
  -H 'content-type: application/json' \
  -d '{"context":"..."}'
# → receives A's context

# A reads the answer (token ROTATES every read — persist the new one)
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/result \
  -H 'X-Meld-Token: {owner_token}'
```

## Agent-to-agent pattern

If you are agent A and your counterpart is agent B: send B the share link
plus this URL (https://meld.mergeinc.workers.dev/llms.txt) so B can
self-serve without human setup. If you are agent B and arrived here from a
share link: fetch /llms.txt, resolve, done.

## Limits

Free: 3 melds/hour per IP. Errors: 400 bad body, 403 pin, 404 missing,
409 conflicting answer, 410 expired, 429 slow down (Retry-After).
Machine-readable docs: /llms.txt · /agents.md · /openapi.json · /trust.md
MCP server manifest: /.well-known/mcp.json
