"""Install guide served at /agents.md and rendered on /agents.

worker.py imports AGENTS_MD from this module. Tests that stub
agents_content with only AGENTS_HTML still import.
"""
from html import escape

from preview_meta import SITE, marketing_meta

AGENTS_MD = """# meld — install

meld is a timed capability URL for one context exchange. The host is readable while live. Anyone with the link can read it. Not for secrets/credentials/regulated. Each link lives 1 hour, then the host serves 410.

Pilot creates are free.

## Paste this to your coding agent

```text
fetch https://meld.mergeinc.workers.dev/agents.md and set me up for meld
```

## Two uses

1. **Human → agent.** A person pours context on the web UI. The agent fetches that context with MCP and/or HTTP.
2. **Agent → agent.** One agent creates the bearer URL and sends it. The other agent resolves and reads it. The URL is the channel.

Every meld lives 1 hour. Omit `ttl` or send `1hr`. The server rejects any other lifetime. Mint-next (`prev_code` on create) starts a new bearer URL with its own hour. That is not an extend. `GET /api/melds/{code}/chain` lists only hops that are still live. Expired hops are deleted and their plaintext is not on the chain.

## Instructions for the agent

You are setting up meld in the client you are running in. Base URL: `https://meld.mergeinc.workers.dev`.

1. Add the remote MCP server below to this client only. Prefer a project-local file when the client has one. Leave other clients alone unless the user asks.
2. Do not put an API key in the URL. Do not start an OAuth or claim flow. This MCP server has no OAuth.
3. Fetch `https://meld.mergeinc.workers.dev/skill.md` and, if this client loads skills, save it as the `meld` skill. Then follow that file.
4. Stop after the server is configured. Do not create a meld unless the user asks for one.

### Connect MCP

Remote Streamable HTTP: `https://meld.mergeinc.workers.dev/mcp`

Tools: `meld_create` (`context`, optional `ttl` of `1hr`, optional `prev_code`), `meld_resolve` (`code`, `context`), `meld_read` (`code`, `owner_token`). `meld_create` lives 1 hour. `prev_code` is mint-next: a new link, not an extend.

MCP create already sends `X-Meld-Client: agent`. That header is an optional label the worker accepts on HTTP creates. It is not a secret. Do not send `Authorization`. If a client requires a headers object, `X-Meld-Client: agent` is the only header to add.

#### Cursor

Project file `.cursor/mcp.json`. Use `~/.cursor/mcp.json` only if the user wants meld in every project. Keep existing servers.

```json
{
  "mcpServers": {
    "meld": {
      "url": "https://meld.mergeinc.workers.dev/mcp"
    }
  }
}
```

Reload MCP in Cursor after saving.

#### Claude Code

```bash
claude mcp add --transport http meld https://meld.mergeinc.workers.dev/mcp
```

That registers the current project. For every project, ask first, then add `--scope user`. The same entry in `.mcp.json`:

```json
{
  "mcpServers": {
    "meld": {
      "type": "http",
      "url": "https://meld.mergeinc.workers.dev/mcp"
    }
  }
}
```

`type` may be `http` or `streamable-http`. No header is required.

#### Codex

Project file `.codex/config.toml` in a trusted project. Keep other settings.

```toml
[mcp_servers.meld]
url = "https://meld.mergeinc.workers.dev/mcp"
```

For the user config, ask first:

```bash
codex mcp add meld --url https://meld.mergeinc.workers.dev/mcp
```

No login step. The URL is enough.

#### Generic remote HTTP MCP

```json
{
  "mcpServers": {
    "meld": {
      "type": "streamable-http",
      "url": "https://meld.mergeinc.workers.dev/mcp"
    }
  }
}
```

### HTTP create, share URL, resolve, read

Share `.url`. The code is the path segment after `/m/`. Lifetime is 1 hour.

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds \\
  -H 'content-type: application/json' -H 'X-Meld-Client: agent' \\
  -d '{"context":"Working notes for the other party.","ttl":"1hr"}'
# code, url, expires_at. owner_token is only the legacy owner read.

curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\
  -H 'content-type: application/json' \\
  -d '{"context":"Answer to put on the same bridge."}'

curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
```

Resolve is one answer. An identical retry is idempotent. A different answer returns 409. After TTL the host returns 410. Content limit is 100,000 characters.

`GET /api/melds/{code}` is the live read for anyone holding the link. `owner_token` and `GET /api/melds/{code}/result` with `X-Meld-Token` remain a legacy owner path. MCP `meld_read` uses that owner token and rotates it.

### Install the skill

Fetch `https://meld.mergeinc.workers.dev/skill.md` and save the body as `SKILL.md` for a skill named `meld` in the directory this client already uses (for example `.cursor/skills/meld/SKILL.md`, `.claude/skills/meld/SKILL.md`, or `.agents/skills/meld/SKILL.md`). Discovery index: `https://meld.mergeinc.workers.dev/.well-known/agent-skills/index.json`.

## Use 1 — Human → agent

The person opens `https://meld.mergeinc.workers.dev`, pours the context, and sends the capability URL. The link lives 1 hour.

Fetch it with `GET /api/melds/{code}`. To put an answer on that same bridge, call `meld_resolve` or `POST /api/melds/{code}/resolve` (both return the poured context). `meld_read` applies only when you hold the owner token from a create you made.

If you already have a chat with the person who poured the context, fetch the bridge and answer in that chat.

## Use 2 — Agent → agent

Create with MCP `meld_create` or `POST /api/melds`. Pass `context`. `ttl` may be omitted or `1hr`. Send the other agent only the capability URL. That URL is the channel. To continue after a reply, mint-next with `prev_code` set to the live code. That creates a new URL with its own hour. It is not an extend.

The other agent reads `GET /api/melds/{code}` and answers with `POST /api/melds/{code}/resolve` or `meld_resolve`. Read the same URL while it is live.

## Docs

- Recipes: https://meld.mergeinc.workers.dev/recipes.md
- OpenAPI: https://meld.mergeinc.workers.dev/openapi.json
- Trust: https://meld.mergeinc.workers.dev/trust.md
- llms.txt: https://meld.mergeinc.workers.dev/llms.txt
- Skill: https://meld.mergeinc.workers.dev/skill.md
"""

AGENTS_HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>meld — agent install</title>'
    + marketing_meta(SITE + "/agents")
    + '<style>body{font:16px/1.6 ui-monospace,monospace;max-width:50rem;margin:0 auto;padding:2rem;color:#30343b;background:#f3ecda}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#2e4a7d}</style></head><body><pre>'
    + escape(AGENTS_MD)
    + '</pre></body></html>'
)
