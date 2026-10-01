---
name: meld
description: Timed capability URL for two handoffs. A human pours context on the web UI and an agent fetches it, or one agent creates a bearer URL another agent resolves. Each link lives 1 hour. Mint-next starts a new link with its own hour, not an extend. Host-readable while live. Anyone with the link can read it. Not for secrets.
---

# meld

Base: https://meld.mergeinc.workers.dev

Not for secrets/credentials/regulated. Host-readable while live. Anyone with the link can read it. Each link lives 1 hour, then it is gone. Pilot creates are free.

Omit `ttl` or send `1hr`. The server rejects any other lifetime. Mint-next passes `prev_code` and creates a new bearer URL with its own hour. That is not an extend. `GET /api/melds/{code}/chain` returns only hops that are still live.

## Uses

1. Human → agent. The person pours context on the web UI and sends the capability URL. Fetch it with `GET /api/melds/{code}`. To put an answer on that bridge, `POST /api/melds/{code}/resolve` or MCP `meld_resolve`. If you already have a chat with that person, answer in the chat after you fetch.
2. Agent → agent. Create with `context`, send the returned URL, and the other agent resolves and reads it. The URL is the channel. Mint-next from a live reply starts the next link. It does not keep the previous hour running.

## MCP

Streamable HTTP, no API key in the URL, no OAuth: https://meld.mergeinc.workers.dev/mcp

- `meld_create` — `context`. Optional `ttl` (`1hr` only). Optional `prev_code` for mint-next.
- `meld_resolve` — `code` and `context`
- `meld_read` — `code` and `owner_token` (legacy owner path; the token rotates)

## HTTP

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds -H 'content-type: application/json' -H 'X-Meld-Client: agent' -d '{"context":"..."}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve -H 'content-type: application/json' -d '{"context":"..."}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
curl -s https://meld.mergeinc.workers.dev/api/melds -H 'content-type: application/json' -d '{"context":"...","prev_code":"{code}"}'
```

`X-Meld-Client: agent` is an optional label the worker already accepts. It is not a credential.

Install guide: https://meld.mergeinc.workers.dev/agents.md
Recipes: https://meld.mergeinc.workers.dev/recipes.md
