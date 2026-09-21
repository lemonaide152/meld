# meld MCP server

Model Context Protocol server for meld — lets any MCP client (Claude Desktop,
Claude Code, Cursor, etc.) create and resolve ephemeral context bridges as tools.

Zero dependencies. Node 18+. Speaks JSON-RPC over stdio.

## Install

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

## Tools

| Tool | Purpose |
|---|---|
| `meld_create` | Create a meld with your context → returns share + owner links |
| `meld_resolve` | Answer a meld you received → returns the other party's context |
| `meld_read` | Owner: read the resolved result (token rotates each read) |

## Verified

Round-trip tested: create → resolve → read → token rotation, all via MCP
JSON-RPC over stdio (2026-09-20).
