# Show HN / launch blurb (ready to post — needs user approval)

Do **not** post until db cooper explicitly approves. Title + body below are drafts.

## Title (≤80 chars)

Show HN: meld – put the context on a URL, then the URL dies

## Body

I built meld because every agent↔human (and agent↔agent) handoff still starts with “paste this giant block.”

meld puts the context on a URL so neither side has to paste the block. Then the URL dies.

How it works:
1. Party A POSTs context → gets one share URL
2. Party B opens it (human in browser, or agent via API) and resolves with their answer
3. Either side reads both contexts via GET /api/melds/{code}
4. After TTL the host serves 410 and the row is gone

Hard promises:
- No accounts
- Unresolved TTL 1h; ~10min after resolve; then 410
- Optional client-side encryption (server stores opaque meld1:… bytes)

Pricing (MVP just shipped):
- Humans: free in the browser
- Agents: 3 melds/IP/hour, then mint a free API key (POST /v1/keys) or wait
- No subscriptions

Live: https://meld.mergeinc.workers.dev
Agent docs: https://meld.mergeinc.workers.dev/llms.txt
A2A card: https://meld.mergeinc.workers.dev/.well-known/agent.json
MCP: https://meld.mergeinc.workers.dev/.well-known/mcp/server-card.json
OpenAPI: https://meld.mergeinc.workers.dev/openapi.json
Repo: https://github.com/lemonaide152/meld

I’d love a real first external user — someone who creates (and ideally resolves) a meld with real non-test context. Roast the API, the trust model, or the pricing.

## Shorter tweet-length variant (still needs approval; do not post)

meld: put the context on a URL so neither side pastes the block. Then the URL dies.
Humans free in browser. Agents: key or quota.
https://meld.mergeinc.workers.dev

## Channels (when approved)

1. Hacker News — Show HN (primary)
2. Reddit r/LocalLLaMA / r/ChatGPTCoding — link + one-sentence hero (no spam)
3. Agent Discord/Slack communities — share /llms.txt not a pitch deck

## Status

Drafted 2026-09-26 ~12:45 PT. Not posted.
