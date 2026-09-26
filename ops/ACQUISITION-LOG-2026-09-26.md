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
