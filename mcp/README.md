# meld MCP server

Model Context Protocol server for meld — lets any MCP client (Claude Desktop,
Claude Code, Cursor, Glama connectors, remote MCP directories, etc.) create and
resolve ephemeral context bridges as tools.

## Remote (streamable-http) — preferred for connectors / directories

```
https://meld.mergeinc.workers.dev/mcp
```

Transport: [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
in JSON response mode (stateless; no SSE session required). Auth: none.
Pilot bridges are free. `meld_create` requires `ttl`: `3m`, `1hr`, or `1d`. There is no default.

Smoke:

```bash
curl -s https://meld.mergeinc.workers.dev/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'

curl -s https://meld.mergeinc.workers.dev/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

Manifests: `/.well-known/mcp.json` · `/.well-known/mcp/server-card.json`

## Stdio (local clients)

Zero dependencies. Node 18+. Speaks JSON-RPC over stdio.

Clone/copy `meld-mcp.mjs` + `package.json`, then add to your MCP client config:

```json
{
  "mcpServers": {
    "meld": {
      "command": "node",
      "args": ["/path/to/meld-mcp.mjs"],
      "env": { "MELD_BASE": "https://meld.mergeinc.workers.dev" }
    }
  }
}
```

Or: `"command": "npx", "args": ["meld-mcp"]` when published.

## Tools

| Tool | Purpose |
|---|---|
| `meld_create` | Create a bridge. Requires `context` and `ttl` (`3m`, `1hr`, or `1d`) → capability URL |
| `meld_resolve` | Answer a meld you received → returns the other party's context |
| `meld_read` | Owner: read the resolved result (token rotates each read) |

## Verified

- Stdio round-trip: create → resolve → read → token rotation (2026-09-20).
- Streamable-http: `initialize` + `tools/list` on `/mcp` (2026-09-26).
