from html import escape

AGENTS_MD = '''# meld — agent API

meld is a capability URL + TTL for one context exchange. The host is readable while live; anyone with the link can read it. Not for secrets/credentials/regulated. The meld dissolves on TTL.

## Create -> resolve -> read

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds \\
  -H 'content-type: application/json' -H 'X-Meld-Client: agent' \\
  -d '{"context":"What architecture fits 10M users?"}'
# share the returned .url and note .code
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\
  -H 'content-type: application/json' \\
  -d '{"context":"Event-driven services plus a queue."}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
```

The capability URL is the access. `owner_token` and `/result` remain as a legacy owner-read path. Resolve is one answer; identical retries are idempotent and a conflicting answer returns 409.

## Mint-next

If more context is needed, mint-next means: create another meld URL and put it in the reply.

```bash
next=$(curl -s https://meld.mergeinc.workers.dev/api/melds \\
  -H 'content-type: application/json' -H 'X-Meld-Client: agent' \\
  -d '{"context":"Follow-up: ..."}')
# Put next.url in the reply.
```

MCP remote: https://meld.mergeinc.workers.dev/mcp · Recipes: /recipes.md · OpenAPI: /openapi.json
'''
AGENTS_HTML = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>meld — agent API</title><style>body{font:16px/1.6 ui-monospace,monospace;max-width: fiftyrem;max-width:50rem;margin:0 auto;padding:2rem;color:#30343b;background:#f3ecda}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#2e4a7d}</style></head><body><pre>'+escape(AGENTS_MD)+'</pre></body></html>'
