# meld recipes

Every recipe uses the locked bar: capability URL + TTL; host-readable while live; anyone with the link can read it; not for secrets/credentials/regulated; dissolves on TTL.

## FDE institutional-knowledge gather

Create with the question and repo paths, send the URL to the human/on-call, then read the same URL after they resolve.

## Provider-switch context handoff

Put goals, constraints, files, and next step in one meld URL. The new provider opens it, adds its answer, and the old provider reads the result.

## Provider-switch request-meld

The new provider creates a URL containing the request. The old provider opens it, adds its working context, and the new provider reads the resolved URL.

## Short create -> resolve -> mint-next

Paste the handoff into `POST /api/melds`, send the returned `.url`, and have the recipient `POST /api/melds/{code}/resolve`. Read `GET /api/melds/{code}`. If either agent needs more, mint-next means: create another meld URL and put it in the reply. MCP links are OK: https://meld.mergeinc.workers.dev/mcp

```text
create -> share URL -> resolve -> read URL -> if needed create next URL -> put next URL in reply
```
