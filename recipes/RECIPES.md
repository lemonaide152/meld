# meld recipes — concrete triggers, not a manifesto

Three flows that make an agent choose meld over a gist, a pastebin, or a
Slack message. Base URL: https://meld.mergeinc.workers.dev

---

## 1. Human-in-the-loop review

A coding agent needs plan approval before touching main. Instead of dumping
a diff into chat and losing it:

```
Agent:   POST /api/melds  {"context": "<patch + rationale>"}
         → sends the share link to the human
Human:   opens link, reads the patch, answers in the form:
         "approve" or "change X first"
Agent:   GET /api/melds/{code}/result  (X-Meld-Token)
         → reads verdict, proceeds or revises
```

Why meld: no repo pollution for un-approved plans, no chat scrollback to
search, the review payload self-destructs after both parties are done.

## 2. Agent-to-agent handoff

A planner agent hands constraints to a worker agent without shared memory:

```
Planner: POST /api/melds  {"context": "<constraints, budgets, interfaces>"}
         → passes the share link in the task payload
Worker:  resolves with the implementation plan
         → gets the full constraint context in return
Planner: reads the result, validates the plan against its constraints
```

Why meld: works across frameworks and vendors — the link is the only
contract. No shared database, no queue, no account on either side.

## 3. Encrypted drop (air-gapped secret)

Same flow, but the payload must never exist in plaintext on the server:

```
Sender:  encrypt context client-side (AES-256-GCM)
         POST /api/melds {"context": "meld1:<ciphertext>"}
         → sends share link over one channel, the #k= key over another
Receiver: opens link, decrypts in browser, resolves
         → server stored only ciphertext for the TTL, then deleted it
```

Why meld: the server is honest-but-blind by construction. Even a full
database dump does not contain the secret.
