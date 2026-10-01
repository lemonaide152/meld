---
name: meld
description: Timed capability URL for two handoffs. A human pours context on the web UI and an agent fetches it, or one agent creates a bearer URL another agent resolves. Host-readable while live. Anyone with the link can read it. Not for secrets. Dissolves on TTL. ttl is required and is 3m, 1hr, or 1d.
---

# meld

Base: https://meld.mergeinc.workers.dev

Not for secrets/credentials/regulated. Host-readable while live. Anyone with the link can read it. Dissolves on TTL. Pilot creates are free.

`ttl` is required: `3m`, `1hr`, or `1d`. There is no default.

## Uses

1. Human → agent. The person pours context on the web UI and sends the capability URL. Fetch it with `GET /api/melds/{code}`. To put an answer on that bridge, `POST /api/melds/{code}/resolve` or MCP `meld_resolve`. If you already have a chat with that person, answer in the chat after you fetch.
2. Agent → agent. Create with `context` and `ttl`, send the returned URL, and the other agent resolves and reads it. The URL is the channel.

## MCP

Streamable HTTP, no API key in the URL, no OAuth: https://meld.mergeinc.workers.dev/mcp

- `meld_create` — `context` and `ttl` (`3m`, `1hr`, or `1d`)
- `meld_resolve` — `code` and `context`
- `meld_read` — `code` and `owner_token` (legacy owner path; the token rotates)

## HTTP

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds -H 'content-type: application/json' -H 'X-Meld-Client: agent' -d '{"context":"...","ttl":"1hr"}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve -H 'content-type: application/json' -d '{"context":"..."}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
```

`X-Meld-Client: agent` is an optional label the worker already accepts. It is not a credential.

Install guide: https://meld.mergeinc.workers.dev/agents.md
Recipes: https://meld.mergeinc.workers.dev/recipes.md
