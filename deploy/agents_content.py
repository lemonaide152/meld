from html import escape

from preview_meta import SITE, marketing_meta

AGENTS_MD = '# meld — agent API\n\nmeld is a capability URL + TTL for one context exchange. Host-readable while live; anyone with the link can read it. Not for secrets/credentials/regulated. The meld dissolves on TTL.\n\n## Create -> resolve -> read\n\n```bash\ncurl -s https://meld.mergeinc.workers.dev/api/melds \\\n  -H \'content-type: application/json\' -H \'X-Meld-Client: agent\' \\\n  -d \'{"context":"What architecture fits 10M users?","ttl":"1hr"}\'\n# share the returned .url and note .code\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve \\\n  -H \'content-type: application/json\' \\\n  -d \'{"context":"Event-driven services plus a queue."}\'\ncurl -s https://meld.mergeinc.workers.dev/api/melds/{code}\n```\n\nThe capability URL is the access. `owner_token` and `/result` remain as a legacy owner-read path. Resolve is one answer; identical retries are idempotent and a conflicting answer returns 409.\n\n## Bridge time\n\n`ttl` is required on create and must be `3m`, `1hr`, or `1d`. The server enforces that lifetime. There is no default. Pilot creates are free.\n\nMCP remote: https://meld.mergeinc.workers.dev/mcp · Recipes: /recipes.md · OpenAPI: /openapi.json\n'
AGENTS_HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>meld — agent API</title>'
    + marketing_meta(SITE + "/agents")
    + '<style>body{font:16px/1.6 ui-monospace,monospace;max-width:50rem;margin:0 auto;padding:2rem;color:#30343b;background:#f3ecda}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#2e4a7d}</style></head><body><pre>'
    + escape(AGENTS_MD)
    + '</pre></body></html>'
)
