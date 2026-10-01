# AGENTS.md — working with meld

Fetch and follow the install guide: https://meld.mergeinc.workers.dev/agents.md

Paste prompt for a coding agent:

```text
fetch https://meld.mergeinc.workers.dev/agents.md and set me up for meld
```

## What this is

A capability URL plus a required TTL for one context exchange. Host-readable while live. Anyone with the link can read it. Not for secrets/credentials/regulated. Dissolves on TTL. After TTL the host serves 410.

Pilot creates are free. `ttl` is required and must be `3m`, `1hr`, or `1d`. There is no default.

## Two uses

1. Human → agent. A person pours context on the web UI. The agent fetches it with MCP and/or HTTP.
2. Agent → agent. The bearer URL is the channel. One agent creates it; the other resolves and reads it.

## Where to connect

MCP Streamable HTTP (no API key in the URL, no OAuth): https://meld.mergeinc.workers.dev/mcp

Skill: https://meld.mergeinc.workers.dev/skill.md

Docs: /llms.txt · /agents.md · /recipes.md · /openapi.json · /trust.md
