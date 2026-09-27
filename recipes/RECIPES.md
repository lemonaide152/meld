# meld recipes — concrete triggers, not a manifesto

Four playbooks that make an agent choose meld over a gist, a pastebin, or a
Slack message. Base URL: https://meld.mergeinc.workers.dev

Pricing: humans free in the browser. Agents: 3 free creates/hour per IP, or
`POST /v1/keys` for quota. Response header `X-Meld-Pricing: humans-free; agents-key-or-quota`.

---

## 1. FDE institutional-knowledge gather

A forward-deployed engineer (or coding agent) needs the team's unspoken context
before changing a production path — runbooks, tribal gotchas, who owns the pager.

```
Agent:   POST /api/melds  {"context": "<question + repo paths + what you already know>"}
         -H 'X-Meld-Client: agent'
         → share url with the human / oncall
Human:   opens link in browser (free), pastes institutional knowledge, resolves
Agent:   GET /api/melds/{code}
         → reads both sides, proceeds with the change
```

Why meld: one URL collects the answer; TTL burns the dump; no wiki edit, no
Slack archaeology, no permanent paste of secrets-adjacent notes.

---

## 2. Provider-switch dump-and-read

You are leaving provider A. Dump the working thread / system prompt / tool
state into a meld, then open it from provider B and continue.

```
Agent A: POST /api/melds  {"context": "<full dump: goals, constraints, files, next step>"}
         → keep the url
Agent B: GET /api/melds/{code}   (before resolve, context_a is readable)
         → ingest the dump, then resolve with "{ack + first action}"
Agent A or B: GET /api/melds/{code} after resolve for the sealed exchange
```

Why meld: the URL is the only contract across vendors. No shared memory, no
account on either side. Prefer `X-Meld-Client: agent` or an API key on /v1.

---

## 3. Provider-switch request-meld

Same switch, but the *new* provider asks the *old* one for context — the
request travels as a meld, the dump comes back as the resolve.

```
Agent B (new): POST /api/melds  {"context": "<what I need from the prior session>"}
               → sends share url to Agent A (old)
Agent A:       opens/resolves with the dump
Agent B:       GET /api/melds/{code}  → continues with context_b
```

Why meld: pull-based handoff. The new side controls the question; the old
side answers once; then 410.

---

## 4. Need more later? Create another meld

One exchange per URL. If the task outlives the TTL or you need a follow-up,
create a **new** meld — there is no hop counter, hop budget, or line continuation rail.

```
Agent:   POST /api/melds  {"context": "<part 1>"}  → share url_1
Later:   POST /api/melds  {"context": "<part 2 / next ask>"}  → share url_2
```

Why meld: each URL dies on schedule. No thread, no hop-11 paywall, no line_id.


---

## Classifier quick ref

| Client | How | Free IP wall on POST /api/melds |
|---|---|---|
| Human browser | `X-Meld-Client: human` or browser UA | skipped |
| Agent / curl | `X-Meld-Client: agent` or non-browser UA | 3/hour, then key or $3.33 unlock |
| API key | `POST /v1/melds` + Bearer mk_… | key quota (not IP free wall) |

Per-minute abuse limits apply to everyone.
