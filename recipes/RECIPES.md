# meld recipes

Every recipe uses the same locked bar: capability URL; host-readable while live; anyone with the link can read it; not for secrets/credentials/regulated; each link lives 1 hour. Mint-next is a new link with its own hour, not an extend.

## 1. FDE institutional-knowledge gather

Create with the question and repo paths, send the URL to the human/on-call, then read the same URL after they resolve.

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds -H 'content-type: application/json' -H 'X-Meld-Client: agent' -d '{"context":"Question + repo paths + known constraints"}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve -H 'content-type: application/json' -d '{"context":"The institutional answer"}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
```

## 2. Provider-switch context handoff

Put goals, constraints, files, and next step in one meld URL. The new provider opens the URL, adds its answer, and the old provider reads the result.

## 3. Provider-switch request-meld

The new provider creates a URL containing the request. The old provider opens it, adds its working context, and the new provider reads the resolved URL.

## 4. Create, share, resolve, read, mint-next

`POST /api/melds` requires `context`. `ttl` may be omitted or `1hr`. Send the returned `.url`. The recipient resolves on that URL. Read `GET /api/melds/{code}` while the bridge is live. To hop, `POST /api/melds` with `prev_code` set to a live code. That new URL has its own hour. MCP: https://meld.mergeinc.workers.dev/mcp

```text
create -> share URL -> resolve -> read URL -> mint-next (new URL, own hour)
```

Pilot bridges are free. Each link is one hour. Per-minute limits apply to everyone.
