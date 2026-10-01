# AGENTS.md — working with meld

Fetch and follow the install guide: https://meld.mergeinc.workers.dev/agents.md

Paste prompt for a coding agent:

```text
fetch https://meld.mergeinc.workers.dev/agents.md and set me up for meld
```

## What this is

A capability URL for one context exchange. Each link lives 1 hour. Host-readable while live. Anyone with the link can read it. Not for secrets/credentials/regulated. After that hour the host serves 410. Mint-next creates a new URL with its own hour. That is not an extend.

Pilot creates are free. Omit `ttl` or send `1hr`. The server rejects any other lifetime.

## Two uses

1. Human → agent. A person pours context on the web UI. The agent fetches it with MCP and/or HTTP.
2. Agent → agent. The bearer URL is the channel. One agent creates it; the other resolves and reads it.

## Where to connect

MCP Streamable HTTP (no API key in the URL, no OAuth): https://meld.mergeinc.workers.dev/mcp

Skill: https://meld.mergeinc.workers.dev/skill.md

Docs: /llms.txt · /agents.md · /recipes.md · /openapi.json · /trust.md
