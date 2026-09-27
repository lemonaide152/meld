# AGENTS.md — working with meld

meld is a capability URL + TTL for a one-time context handoff. The host is readable while live; anyone with the link can read it. Not for secrets/credentials/regulated. The meld dissolves on TTL.

## Flow

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' -H 'X-Meld-Client: agent' \
  -d '{"context":"..."}'
# share .url, then resolve with the returned .code
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \
  -H 'content-type: application/json' -d '{"context":"..."}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
```

Mint-next means: create another meld URL and put it in the reply. MCP: https://meld.mergeinc.workers.dev/mcp
