# meld → universal protocol for context bridging

## What "universal protocol" means here

A protocol is universal when a developer or agent on any stack can adopt it without depending on a vendor, and when the pattern it encodes is more convenient than every ad-hoc alternative (pasting into chat, gisting, spinning up a temp S3 bucket, a shared DB row). meld already has the primitive: three HTTP calls, no accounts, ephemeral by default, works for humans and agents. What it lacks: a formal spec anyone can implement against, an SDK ecosystem beyond Node, and adoption pressure from being the path of least resistance.

The protocol is not "use meld.mergeinc.workers.dev" — it's "implement these four endpoints with these semantics and your service is a meld-compatible context bridge." The hosted instance is one implementation, not the protocol itself.

## The four primitives (already shipped)

| # | Call | Semantics |
|---|------|-----------|
| 1 | `POST /melds {context}` → `{code, url, owner_token}` | Pour. Returns a share URL (capability for the counterpart) + an owner token (capability for the sender). |
| 2 | `GET /melds/{code}` → `{context_a, expires_at}` | Read the poured context. No auth needed — the URL is the capability. |
| 3 | `POST /melds/{code}/resolve {context}` → merged exchange | Answer once. Idempotent on identical answers, 409 on conflicting. |
| 4 | `GET /melds/{code}/result` (X-Meld-Token) → `{context_a, context_b}` | Sender reads the answer. Token rotates every read. |

Plus the lifecycle contract: TTL 1h unresolved, ~10 min post-exchange, then deleted. Optional client-side encryption (server stores only ciphertext). Optional PIN for responder authentication. Optional signed answers for attribution.

## Phases

### Phase 0 — Formal spec (the thing that makes it a protocol)

Write `meld-spec.md` — a standalone, versioned document that defines the four primitives, the lifecycle contract, the capability model (share URL = read capability, owner token = result capability), error semantics (409 conflicting answer, 410 expired, 403 invalid token), and a conformance test suite. Publish it at the repo root and at `/spec` on the hosted instance.

Anyone who implements those four endpoints with those semantics runs a meld-compatible bridge — self-hosted, on their own domain, with their own TTL policy. The spec makes the protocol independent of the hosted instance.

**Deliverable:** `meld-spec.md` + conformance test suite (reusable by any implementer). Target: 1 week.

### Phase 1 — SDKs on the two biggest stacks (npm + Python)

npm `meld-bridge` already exists (v1.0.0, tested, MIT). It needs a publish token. Python `meld` package: same four functions, `pip install meld-bridge`. Both wrap the spec, not the hosted instance — the base URL is configurable (`MELD_URL` env var), so self-hosted bridges work with the same SDK.

**Deliverable:** `npm publish` (needs NPM_TOKEN from operator) + PyPI package. Target: 1 week each, blocked on credentials.

### Phase 2 — Discovery at the protocol layer

Current discovery surfaces (`/.well-known/agent.json`, `llms.txt`, MCP registry, Smithery) describe the hosted instance. A protocol needs discovery of ALL implementations:
- Add a `meld-spec-version` field to the A2A card and MCP card
- Create a `meld://` URI scheme convention (like `mailto:` or `web+hook:`) — a meld link becomes `meld:{code}@{host}` so agents can route without knowing the base URL
- Submit the spec to the A2A official registry when it opens (it's still a proposal); meanwhile the agent card already carries the right shape
- Write the "implement meld in your framework" guide — 4 endpoints, any stack, conformance suite included

**Deliverable:** spec discovery fields + URI scheme convention + implementation guide. Target: 2 weeks, overlaps Phase 1.

### Phase 3 — Real adoption pressure (the hard part)

A protocol is universal when NOT having it is the exception. That means:
- **Default in frameworks**: LangChain, CrewAI, AutoGen — contribute a `MeldContextBridge` / `MeldHandoff` integration to each framework's contrib directory. Each one is ~50 lines wrapping the SDK.
- **The killer demo**: a working integration where two agents on different frameworks (say, a Claude Code agent and a Hermes agent) exchange context through a meld link, published as a reproducible example in the repo.
- **The trust story**: the T3 E2E encryption (client-side AES-256-GCM, key in URL fragment) is already built but was stripped from all surfaces. For a protocol pitch, this is the differentiator — "the server is content-blind by construction" — and needs to come back as an optional, documented mode with clear trust-model copy.

**Deliverable:** framework integrations + cross-framework demo + E2E mode re-surfaced. Target: 4–6 weeks.

### Phase 4 — Governance

A universal protocol needs a governance answer: who owns the spec, how do changes happen, what's the reference implementation. Start with "maintained by the meld project, MIT-licensed spec, reference implementation at the GitHub repo" and evolve toward a foundation if adoption justifies it.

## What NOT to do

- Don't rename the hosted instance to "the protocol" — the protocol is the spec, the instance is one implementation
- Don't add auth to the core primitive — capability URLs ARE the auth
- Don't build a consortium or a foundation before there are 3+ independent implementations — that's premature governance
- Don't compete with MCP or A2A — meld is complementary (they orchestrate, meld bridges context)

## Immediate next step

Write `meld-spec.md` and the conformance suite. This is the artifact that turns "a hosted tool with an API" into "a protocol with a reference implementation." Everything else builds on it.
