# Acquisition log — 2026-09-26 (PT)

## Live home verification
- Hero present: "meld puts the context on a URL so neither side has to paste the block. Then the URL dies."
- Humans-free line present: "Humans free in the browser · agents use a key or quota"
- Meta description matches hero.

## Directory submissions (URL-only / unauth forms)
| Directory | Result | Ref |
|---|---|---|
| aiagenttools.dev | submitted OK | id `muisrl914xpo0` |
| MeshKore directory form (`POST /v1/directory/submit`) | received for review | id `146` |
| MeshKore agent register (`agent_id=meld`) | registered + DiscoveryCard patched | profile https://meshkore.com/agent/meld |
| mcp.directory | not submitted — Next.js form, no clear unauth JSON API | — |
| Official MCP Registry | blocked — needs GitHub OIDC / publisher login (user approval) | — |
| Future Tools / TAAFT / Toolify | skipped — captcha and/or paid / email-as-user | — |

Credentials for MeshKore heartbeat saved privately at `ops/meshkore-meld-credentials.json` (not for commit).

## Discovery gaps fixed + deployed
- Was: `/sitemap.xml` and `/.well-known/ai-plugin.json` fell through to SPA HTML (500 briefly during fix)
- Was: `AGENT_CARD` missing protocols / pricing / availability / contact
- Deployed to https://meld.mergeinc.workers.dev (version abe33a2e-d65a-40c3-bbd3-c899f1b64716 ~12:45 PT)
- Live now: real sitemap.xml, ai-plugin.json, enriched agent card, robots Sitemap line

## D1 user check
BLOCKED. Cloudflare API token verifies and can list Workers + GraphQL invocation metrics, but D1 query returns code 7403 (not authorized for D1). Cannot confirm/deny non-smoke melds from DB without a token that includes D1 edit/read on account `cfe3fcb93022209b34d70ce98115070a` / db `meld-prod`.

Traffic signal (Workers invocations, not creates): ~7.8k requests across sampled hours Sep 20–26 PT — consistent with crawlers + operator smoke, not proof of a legitimate create.

## Fake traffic
None created. No emails/Slack/tweets/DMs as the user.

## Wave 1 — ~14:40–14:45 PT (2026-09-26)

### Channels attempted + outcomes

| Channel | Action | Outcome |
|---|---|---|
| MeshKore agent `meld` | Heartbeat `POST /v1/agents/me/state` (needs browser-like UA; CF 1010 without) | Card updated; profile still indexed at https://meshkore.com/agent/meld. Page still showed Registered·offline earlier; deployed A2A skill aliases `/v1/meld-create\|resolve\|read` + `/health` to match MeshKore invoke convention. |
| MeshKore directory id 146 | Status check | Profile live via agent register path (directory review separate). |
| aiagenttools.dev `muisrl914xpo0` | Status check | Public `/tool-meld` still **404** (pending review). No status API. |
| AgentMRR | PoW register + `POST /api/products` | **Submitted** product id `c2a7689e-2e64-46ff-b22d-37318ed3a4f4` (live by id). Creds private: `ops/agentmrr-meld-credentials.json` (gitignored). |
| AgentBets.ai | Evaluated API | **Skipped** — directory is prediction-market/betting scope only; would auto-decline. |
| 402.ad | Evaluated | **Skipped** — agent `POST /v1/submit` requires x402 payment + wallet signature; human form is browser-only. |
| PulseMCP / mcp.so / CuratedMCP / AgenticSkills / Glama / Smithery / TAAFT | Probed | **Blocked this wave** — reCAPTCHA and/or GitHub OAuth / no unauth JSON API. Need user browser session for Wave 2. |
| Fushu.dev | GET register | **500** FUNCTION_INVOCATION_FAILED — retry later. |
| Official MCP Registry | — | Still needs GitHub OIDC publisher login (user). |
| TensorBlock/awesome-mcp-servers | PR as lemonaide152 | **Opened** https://github.com/TensorBlock/awesome-mcp-servers/pull/2732 (Communication & Messaging). |
| GitHub seed | Issue on lemonaide152/meld | **Opened** https://github.com/lemonaide152/meld/issues/3 (agent playbooks). Repo homepage set to live URL. |
| README / glama.json | Discovery polish | Try it + Discover sections; `glama.json` maintainers for future Glama claim. |
| Social (IH / Reddit / X) | Drafts only | Saved under `ops/drafts/` — **not posted**. |
| Show HN | — | **Not posted** (forbidden). |

### Deploy
- Worker version `dd7973d4-1231-4f94-b444-261582247344` (~14:43 PT): sitemap/ai-plugin already in tree; added MeshKore skill aliases + `/health` JSON.

### D1 snapshot (after Wave 1)
- Live `melds` rows: **7**; resolved among them: **0**
- Funnel 2026-09-26: `created=12`, `free_limit_hit=4` (creates include operator smokes; TTL deletes older rows so funnel ≫ live table)
- Classification of live rows:
  - `d2it56eot8zk`, `ni0o1ah7esqd` — **operator probe** (empty context from skill-route checks; IP `104.30.180.115`) — NOT users
  - `mg1fa9q0d703`, `iqau1n399f2r` — **smoke** ("status check only — discard")
  - `93h9govipt22`, `owe2sk52i3nw` — **builder** (`api:11b6ebec…` review/continuation hops)
  - `cpaqxw4s6dwu` — **builder seed** (`api:956fde15…` "understand meld" prompt)
- **Legitimate external user: NO**

### Fake traffic
None intentionally created as users. Empty-context probe melds above are operator-only and must not be counted.

### Recommended Wave 2 (ranked by leverage)
1. **User posts drafts** — Indie Hackers + one Reddit (r/LocalLLaMA or r/ClaudeAI) + short X from `ops/drafts/` (highest human reach).
2. **Official MCP Registry** — user GitHub OIDC `mcp-publisher` publish (feeds PulseMCP/VS Code).
3. **Browser submits** — PulseMCP, Glama claim, Smithery, mcp.so, AgenticSkills (need login/captcha).
4. **Reject empty context** on create (stop probe/junk melds) + optional MeshKore cron heartbeat every ~5 min.
5. **aiagenttools** follow-up if still 404 after 48h; Fushu retry when 500 clears.
6. **Show HN** only if user lifts the ban (`ops/SHOW-HN.md` ready).
7. Monitor TensorBlock PR #2732 merge; D1 for non-smoke / non-api:* creates with real context.

### Blockers needing the user
- Social login/posting as them (IH, Reddit, X, Product Hunt)
- GitHub OIDC for Official MCP Registry / Glama claim OAuth
- Show HN permission (currently forbidden)
- Optional: email for directory forms that require a real inbox (PulseMCP etc.)

## Wave 2 — ~14:45–15:50 PT (2026-09-26)

### Channels attempted + outcomes

| Channel | Action | Outcome |
|---|---|---|
| PulseMCP `/submit` | Probed | **Blocked** — submissions paused (“not accepting new MCP server or client submissions”); also reCAPTCHA. Official Registry recommended by them. |
| Glama claim | Probed | **Blocked** — no unauth claim path; OAuth/GitHub required. `glama.json` already in repo for future claim. |
| Smithery `/servers/new` | Probed | **Blocked** — redirects to GitHub OAuth login. |
| mcp.so `/submit` | Probed + GitHub issue | Paid $39 path skipped. Free path: **opened** https://github.com/chatmcp/mcpso/issues/4425 |
| mcp.directory `/api/submit-server` | POST `{githubUrl}` | **Already submitted** (409 “repository has already been submitted”). Pending their review. |
| Fushu.dev | Retry | Still **500** FUNCTION_INVOCATION_FAILED site-wide. |
| mcpservers.org `/submit` | Form probe | Free form exists (requires contact email + client JS). Curl POST inconclusive (SPA/TanStack). **Needs browser** to confirm. |
| CuratedMCP | Probed | `/api/submit` **401 Unauthorized** — account required. |
| MCPCentral | Probed | Read-only `/api/servers` (405 on POST). Indexes Official Registry; no unauth write. |
| MeshKore agent `meld` | Token + DiscoveryCard heartbeat | Heartbeat script works (`ops/meshkore-heartbeat.sh`). Profile registered; `endpoint` set; **`live` still 0** after CORS fix (hub watermark + external probe lag). |
| aiagenttools.dev `/tool-meld` | Status | Still **404** (pending review). |
| TensorBlock PR #2732 | Status | Still **open**, unmerged. |
| AgentMRR product `c2a7689e-…` | Status | Public product URL **404**; API returns HTML-only/500. Listing not publicly live. |

### Code harden + deploy
- Reject empty / whitespace-only `context` via `_require_context` on create+resolve for `/api/melds` and `/v1/melds` (worker.py + meld.py).
- Tests: `test_empty_context.py` (8/8); `test_meld.py` empty case now expects 400; freelimit suite still 42/42.
- CORS extended to `/health` + `/.well-known/*` (MeshKore/browser probes).
- Deployed Worker version `87b35ac1-45ba-42f4-8dd2-7bf527454080` (~15:49 PT). Live verified: empty create → 400; CORS `*` on `/health` and agent card.

### MeshKore ops
- `ops/meshkore-heartbeat.sh` + `ops/MESHKORE.md` (UA fix, token mint, DiscoveryCard PATCH, optional cron every 5 min). No user required to run.

### D1 snapshot (after Wave 2)
- Live `melds` rows: **7**; resolved among them: **0**
- Funnel 2026-09-26: `created=12`, `free_limit_hit=4` (unchanged since Wave 1 — empty-context harden stops new empty probes)
- Classification unchanged: operator empty probes + smoke + builder/api seeds only.
- **Legitimate external user: NO**

### Fake traffic
None. No Show HN. No email/Slack/tweets/DMs as the user.

### Recommended Wave 3 (ranked)
1. **User posts drafts** — Indie Hackers + Reddit + X (`ops/drafts/`).
2. **Official MCP Registry** — user GitHub OIDC `mcp-publisher` (unblocks PulseMCP/Glama/MCPCentral auto-index).
3. **Browser (user session)** — Glama claim, Smithery, mcpservers.org confirm, CuratedMCP login.
4. Monitor mcp.so #4425, mcp.directory review, TensorBlock #2732, aiagenttools `/tool-meld`.
5. MeshKore: confirm `live=1` after probe; optional box cron for heartbeat.
6. Show HN only if user lifts ban.

### Blockers needing the user
- Social posting as them (IH, Reddit, X, Product Hunt)
- GitHub OIDC for Official MCP Registry / Glama / Smithery
- Show HN permission (still forbidden)
- Optional: real inbox for directory forms that email (mcpservers.org)

## Wave 3 — ~14:50–16:00 PT (2026-09-26)

### Channels attempted + outcomes

| Channel | Action | Outcome |
|---|---|---|
| mcpservers.org `/submit` | Playwright inspect + free form | Form reachable, **no captcha/SSO**. Required fields include contact **email**. No public inbox for lemonaide152/meld → **stopped** (did not invent email). ServerFn POST without browser session → 403. |
| MeshKore agent `meld` | Diagnose `live=0`; pubkey bind; WS keepalive | Root cause: directory `live=1` only while `wss://api.meshkore.com/v1/agents/ws` is held (HTTP heartbeat alone insufficient). Bound Ed25519 pubkey (`verified=true`). Added `ops/meshkore-ws-keepalive.py`; process running on box → **API `live=1`**. Heartbeat loop every 5m (no crontab binary; bash loop). Residual: live drops if box WS process dies; Workers cannot hold sockets. |
| mcp.so #4425 | Status | Still open, 0 comments. No bump (same-day, body complete). |
| TensorBlock PR #2732 | Status | Still open/mergeable, 0 comments. No bump. |
| aiagenttools `/tool-meld` | Recheck | Still **404** (pending review). |
| AgentMRR product | Recheck | Public URL still **404**. |
| mcp.directory | Recheck | Still pending (prior 409); search HTML no meld hit. |
| **mcpub.dev** | Unauth `tools/call submit` | **Registered** — `https://meld.mergeinc.workers.dev` live in archive/search. |
| punkpeye/awesome-mcp-servers | PR | **Opened** https://github.com/punkpeye/awesome-mcp-servers/pull/15198 (Communication; agent `🤖🤖🤖` title). |

### Code + deploy
- Fixed OPTIONS CORS: `JSONResponse({}, 204)` crashed CF python workers (**1101**). Now empty-body `Response(204)` on `/health`, `/.well-known/*`, `/api/*`, `/v1/*`, `/llms.txt`, etc.
- Health JSON enriched: `agent_id`, `upstream_ready`.
- Agent card skills gained `examples` (MeshKore §27 / card_match friendly).
- Deployed Worker version `2a016a53-ff82-45ca-afd5-7c541bbfb90f` (~15:55 PT). Verified OPTIONS `/health` → 204 + ACAO `*`.

### MeshKore ops
- `ops/meshkore-ws-keepalive.py` + updated `ops/MESHKORE.md`
- Identity key private: `ops/meshkore-meld-identity.json` (gitignored)
- Box processes: WS keepalive + 5-min heartbeat loop

### D1 snapshot (after Wave 3)
- Live `melds` rows: **7**; resolved among them: **0**
- Funnel 2026-09-26 unchanged: `created=12`, `free_limit_hit=4`
- Classification unchanged: empty probes (pre-harden) + smoke (“status check only”) + builder `api:*` seeds only
- **Legitimate external user: NO**
- Redacted evidence: all non-`api:*` creators are IP `104.30.180.115` (operator) with empty or “status check only — discard” context; remaining rows are `api:11b6ebec…` / `api:956fde15…` builder seeds.

### Fake traffic
None. No Show HN. No email/Slack/tweets/DMs as the user.

### Recommended Wave 4 (solo-capable; still no user-ask)
1. Keep MeshKore WS keepalive supervised (restart if box reboots); confirm Oracle `operational` after their probe lag.
2. Monitor punkpeye #15198, TensorBlock #2732, mcp.so #4425, mcp.directory, aiagenttools `/tool-meld`.
3. Try Fushu.dev again if 500 clears; scan for one more quality unauth directory (avoid spam farms).
4. Optional: awesome-remote-mcp-servers entry (hosted URL angle) if punkpeye wants local-only distinction.
5. D1 watch for non-smoke / non-`api:*` creates with real context.
6. Still deferred (user-gated, do not ask): social drafts, Official MCP Registry OIDC, Glama/Smithery OAuth, mcpservers.org contact email, Show HN.

### Blockers needing the user (only if truly blocking solo)
- Contact email for mcpservers.org free form (and similar inbox-gated directories)
- Social / OIDC / Show HN still user-gated — **not** required to continue solo Wave 4 monitoring + more unauth channels
