# Meld — Project Spec

Status: Pilot-ready foundation
Audience: Software factory foreman
Rule: The repo is the reference implementation. The hosted pilot runs the same code. Docs describe only what the code does.

## 1. Purpose

Meld is an ephemeral context bridge. Two parties exchange ordinary working context over one capability URL. No accounts, no logins, no shared infrastructure. The link is the authorization.

In scope: create, read, reply, expiry, uniform 404, MCP endpoint, OpenAPI spec, self-host and hosted pilot from the same code.

Out of scope: encryption, accounts, private rooms, owner tokens, dissolve endpoints, email capture, learn flags in the core spec, AI in the loop, rate-limit status codes that leak existence.

## Usage assumption

Each party keeps its own state. Meld assumes that the parties on either side of a bridge hold whatever they need to continue the work. If a bridge expires or drops, either party can create a new one, share the new link, and pick back up where they left off. This is an assumption about how meld is used, not something meld does. The bridge does not save, restore, link, or carry state between bridges, and a new bridge knows nothing about an old one.

## 2. Canonical spec

SPEC.md is the only source of truth for behavior. llms.txt, agents.md, skill.md, the MCP manifest, the agent card, and the OpenAPI document are generated from it or checked against it. Drift is a build failure.

Locked behavior:

- Create returns a code and a URL. That URL is the capability. Create does not return a token.
- Open window: 36 hours from creation until the first reply.
- Idle window: the first reply sets 24 hours. Each later reply resets that 24 hours. There is no maximum lifetime once replies have started.
- Reads do not move the clock.
- Termination is the timer only. No dissolve endpoint. No owner credential. Silence closes the bridge.
- Uniform not-found: expired, unknown, and over-cap codes return the same status and the same body. An observer cannot tell them apart.
- Host-readable while live. The operator can read plaintext on a live bridge. The host does not summarize, rewrite, or run a model on it.
- Not for secrets, credentials, or regulated data. This is a constraint, not a feature to work around.
- No accounts. No archive. A restart of a memory-only self-host drops live links. The hosted pilot may persist only while the bridge is live, then delete it.

## 3. API

Base paths may be /api/melds to match the current pilot. Behavior is what matters.

### POST /api/melds

Body: one note. Accept note or context. If older clients also send for and not_for, all supplied strings must be identical; otherwise 400. Reject ttl, email, pin, and prev_code.

Returns: code, url, expires_at.

### GET /api/melds/{code} and GET /m/{code}

While live: the note and every reply, plus timestamps. Does not start or reset the timer.

Link-preview crawlers on /m/{code} get an expires-only card. That card does not include the exchange and does not count as a read.

### POST /api/melds/{code}/resolve

Body: context (the reply). Appends the reply. The first reply switches the window to 24 hours from now. Each later reply resets that 24 hours.

### GET /health

Liveness only. No meld data.

### Errors

- Malformed body: 400, with a validation message. This is not an existence oracle.
- Unknown, expired, dissolved-by-sweep, or over the reply cap: 404 and {"detail":"Meld not found"}. Same status, same body, every time.
- No 410. No 429. Do not rate-limit in a way that changes the status code for a missing code.

Content limit: 100,000 characters per note and per reply.

Reply cap: 50 replies per meld. The next reply is 404, identical to a missing code.

Code entropy: at least 128 bits. Unguessable. No sequential ids.

## 4. Data model

melds

- code — primary key
- note — text, required
- created_at
- expires_at
- reply_count — integer, default 0

replies

- id
- code
- content
- created_at

No owner token column. No email column. No learn flag in this schema.

Timestamps are UTC ISO 8601 everywhere in JSON. Do not mix datetime objects and strings in stored or returned fields.

## 5. Expiry

A sweep deletes expired bridges. On the hosted pilot this is a scheduled job every 5 minutes. Self-host may sweep on a timer or on each request, but expiry must not depend on someone creating a new meld.

After deletion, the next request is the uniform 404. The server does not keep a record that the code existed.

If the hosted store has a backup or time-travel window, say so in trust.md in one sentence. Do not claim "gone" if a platform backup still holds it.

## 6. Surfaces that must match the spec

- Web UI: one note, create link, show the 36/24 rule, "not for secrets."
- MCP tools: meld_create, meld_resolve, meld_read. No ttl argument. No owner token.
- OpenAPI: real request and response schemas, including the 404 body. Empty schemas are a defect.
- Agent docs: two uses only — human writes a note and an agent replies on the link; or two agents talk on one URL.

## 7. One codebase

The GitHub repo is the reference. The Workers deployment builds from that repo. Hosted-only behavior that is not in the repo is a bug.

Self-host stays supported: Docker, Compose with Caddy, or plain Python. Self-host may be memory-only. The hosted pilot may use D1. Behavior of create, read, reply, and 404 is the same in both.

## 8. Definition of done

- Code matches SPEC.md.
- OpenAPI validates against live responses.
- Hosted pilot runs the same commit as the reference repo.
- CI fails on spec drift, empty schemas, or timestamp inconsistency.
- A client can be written from the OpenAPI document alone.

## 9. Pilot

Pilot instrumentation (a learn opt-in, follow-up contact) lives outside this spec. It must not change create, read, reply, or 404.

Track for 30 days: melds created, replies per meld, MCP calls versus browser creates.

Stop the pilot if MCP calls stay under 10 percent of creates, or if replies per meld stay under one. The wedge is agent handoff, not another pastebin.

## 10. Non-goals

Do not add encryption, accounts, private rooms, owner tokens, dissolve, email, or a model in the middle. The trust model is the product: a host-readable, timed, capability URL for ordinary disposable handoffs.

## Appendix: machine-checked constants

`check_spec.py` parses this block and fails the build if the code, the generated documents, or the OpenAPI document disagree with it.

```meld-spec
open_hours: 36
idle_hours: 24
max_chars: 100000
reply_cap: 50
min_code_bits: 128
sweep_minutes: 5
not_found_status: 404
not_found_body: {"detail":"Meld not found"}
create_fields: note, context
legacy_fields: for, not_for
rejected_fields: ttl, email, pin, prev_code
reply_field: context
mcp_tools: meld_create, meld_resolve, meld_read
routes: POST /api/melds, GET /api/melds/{code}, POST /api/melds/{code}/resolve, GET /m/{code}, GET /health
melds_columns: code, note, created_at, expires_at, reply_count
replies_columns: id, code, content, created_at
timestamp_format: YYYY-MM-DDTHH:MM:SS.mmmZ
```
