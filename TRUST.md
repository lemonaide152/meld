# meld — Trust Model

*Last updated: 2026-09-26. This document is the contract. When the code and
this document disagree, that is a bug — file it.*

## The only hard promise

**After TTL T, the host serves 410 and the meld is gone.** Unresolved melds
live at most 1 hour; after resolution, about 10 minutes. Access paths delete
expired rows. There is no archive to recover from and no long-term content
store. That is the product guarantee — not crypto theater, not identity, not
an owner token.

meld puts the context on a URL so neither side has to paste the block. Then the URL dies.

---

## What the server sees

### A standard meld (no encryption chosen)

| Data | Can the server see it? |
|---|---|
| Your context (what you paste) | **Yes** — stored until it expires |
| The answer (Party B's context) | **Yes** — same |
| Your IP address | **Yes** — rate limiting and pay-per-meld unlocks only |
| Your PIN (if you set one) | **No** — stored only as a SHA-256 hash |

**In plain words: on a standard meld, the operator can read your content while
it exists.** It exists for at most 1 hour (about 10 minutes after resolution).
If that is not acceptable, encrypt client-side before create (optional).

### Optional client-side encryption

If you encrypt in your own tooling before POST (API clients), the server
stores ciphertext it cannot open. The key lives with the parties (e.g. a
`#k=` fragment). Lose the key, lose the content — there is no server-side
recovery. This is an option, not the primary trust story.

### Owner token (legacy)

Create still returns an `owner_token`, and `GET /api/melds/{code}/result`
still accepts it (token rotates on each read). That path remains for
backwards compatibility on the live host. **Preferred Party A read after
resolve is `GET /api/melds/{code}`, which returns both sides with no token.**
Do not treat the owner token as the main product trust mechanism.

### Rate limits and abuse

The server tracks request counts per IP to make guessing and flooding
expensive. It does not inspect, filter, or moderate content. Abuse handling
is content-blind: throttle behavior, never police speech.

---

## What the server does NOT have

- **No accounts, no passwords, no sessions.**
- **No persistent content store** beyond TTL. When a meld expires, it is gone.
- **No analytics / tracking pixels / third-party scripts** on the app page.
- **No read receipts.**

---

## Not for sensitive data

**Do not put secrets, credentials, PII you cannot afford leaked, or regulated data into a meld.** The human create UI does not offer E2E. Browser / API / MCP creates store plaintext the server can read while the meld exists (until TTL). Optional client-side `meld1:` encryption remains available to API clients only — it is not a vault and not an encryption-at-rest product promise. Use meld for ordinary context handoff only.

## What we cannot protect you from

1. **The link is the capability.** Whoever holds the meld URL can read
   `context_a` on a standard meld. Share it as carefully as the content.
2. **A compromised Party B is a compromised answer.** Optional PIN helps.
3. **Encrypted melds punish lost keys.** There is no "forgot my key."
4. **Metadata is not encrypted.** Timing, size, and IP of requests remain
   visible to the server even when the body is ciphertext.

---

## Hosting posture

Payment identity lives with Stripe. Durable host state (if any) is payment-
adjacent and must not become a content archive. Operational notes:
[ops/HOSTING.md](ops/HOSTING.md).

## Verify

- Create a meld, wait past TTL (or force expiry), then `GET /api/melds/<code>`
  — expect **410**. That is the contract.
- Optional: create an encrypted meld and `GET /api/melds/<code>` — you should
  see opaque ciphertext, not your plaintext.

## Ship posture (2026-09-26)

- Bare `GET /api/melds/{code}` returns metadata only (plus `meld1:` ciphertext when an API client used E2E). Plaintext bodies require `X-Meld-Token`.
- Human create UI: no E2E checkbox, no email field. Browser creates are plaintext on the server until TTL (same honesty as API/MCP). API clients may still POST `meld1:` ciphertext.
- `creator_ip` / `resolver_ip` are not stored on new meld rows.

## Study tagging (pilots)

Email was removed from the create UI. Pilots should put `study:v1:Pxx` in the **first line of context** (or similar) so study cohorts stay identifiable without the optional email field. Server may still accept `email` on the API; the human form does not collect it.

