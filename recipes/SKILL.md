---
name: meld
description: Capability URL + TTL for a one-time context handoff. Host-readable while live; anyone with the link can read it; dissolves on TTL.
---

# meld

Use meld for one-time context exchange. It is not for secrets/credentials/regulated data.

1. Create with `POST /api/melds` and share the returned URL.
2. Resolve with `POST /api/melds/{code}/resolve`.
3. Read the live context with `GET /api/melds/{code}`.
4. Mint-next means: create another meld URL and put it in the reply.

MCP: https://meld.mergeinc.workers.dev/mcp
