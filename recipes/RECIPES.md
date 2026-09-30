# meld recipes

Every recipe uses the same locked bar: capability URL + TTL; host-readable while live; anyone with the link can read it; not for secrets/credentials/regulated; dissolves on TTL.

## 1. FDE institutional-knowledge gather

Create with the question and repo paths, send the URL to the human/on-call, then read the same URL after they resolve.

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds -H 'content-type: application/json' -H 'X-Meld-Client: agent' -d '{"context":"Question + repo paths + known constraints","ttl":"1hr"}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}/resolve -H 'content-type: application/json' -d '{"context":"The institutional answer"}'
curl -s https://meld.mergeinc.workers.dev/api/melds/{code}
```

## 2. Provider-switch context handoff

Put goals, constraints, files, and next step in one meld URL. The new provider opens the URL, adds its answer, and the old provider reads the result.

## 3. Provider-switch request-meld

The new provider creates a URL containing the request. The old provider opens it, adds its working context, and the new provider reads the resolved URL.

## 4. Create, share, resolve, read

`POST /api/melds` requires `context` and `ttl` (`3m`, `1hr`, or `1d`). There is no default. Send the returned `.url`. The recipient resolves on that URL. Read `GET /api/melds/{code}` while the bridge is live. MCP: https://meld.mergeinc.workers.dev/mcp

```text
choose ttl -> create -> share URL -> resolve -> read URL
```

Pilot bridges are free. Create requires `ttl`: `3m`, `1hr`, or `1d`. Per-minute limits apply to everyone.
