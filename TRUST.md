# meld — Trust Model (MVP)

*Last updated: 2026-09-20. This document is the contract. When the code and
this document disagree, that is a bug — file it.*

meld is built on one principle: **trust never requires knowing who you are —
only what you hold.** No accounts. No email verification. No identity leaks
to third parties. This page states exactly what the server can and cannot
see, and what we can and cannot do, at each layer of the system.

---

## 1. What the server sees, layer by layer

### A standard meld (no encryption chosen)

| Data | Can the server see it? |
|---|---|
| Your context (what you paste) | **Yes** — stored in memory verbatim until it expires |
| The answer (Party B's context) | **Yes** — same |
| Your IP address | **Yes** — used only for rate limiting and pro status; never displayed, never sold |
| Your email (optional field) | **Yes, if you type one** — used only for pro status |
| Your PIN (if you set one) | **No** — stored only as a SHA-256 hash |
| Who resolved your meld | Only that *someone* did — no read receipts by design |

**In plain words: on a standard meld, the operator of meld.sh can read your
content while it exists.** It exists for at most 1 hour (10 minutes after
resolution). If that is not acceptable for your content, use the option below.

### An end-to-end encrypted meld (checkbox on create)

| Data | Can the server see it? |
|---|---|
| Your context | **No** — the browser encrypts it (AES-256-GCM) before sending; the server stores only ciphertext |
| The encryption key | **No** — the key lives only in your link's `#k=` fragment; fragments are never sent to any server |
| The answer | **No** — Party B's browser encrypts it with the same key |

**In plain words: the server holds gibberish it cannot open.** Even a full
server breach, a subpoena, or an insider cannot read an E2E meld's content.
The tradeoff is absolute: **lose the link, lose the content** — there is no
recovery, because there is no key on the server to recover with.

### The owner token

Every meld has a 64-character secret given only to the creator. It is the
**sole proof of ownership**. Each time the creator reads the result, the
token rotates — an old token stops working. If you leak it, whoever holds it
can read the result until your next read.

### Rate limits and abuse

The server tracks request counts per IP (sliding window) to make guessing and
flooding expensive. It does not inspect, filter, or moderate content — ever.
Abuse handling is content-blind: we throttle behavior (request rates), never
police speech.

---

## 2. What the server does NOT have

- **No accounts, no passwords, no sessions.** There is nothing to log into.
- **No persistent content store.** Live melds are in memory; expiration is
  enforced by a sweeper that deletes them. There is no archive to subpoena.
- **No analytics, no tracking pixels, no third-party scripts** on the app page.
- **No read receipts.** We cannot tell you who read a link — the feature
  does not exist, on purpose.
- **No content backup.** When a meld expires, it is gone. We cannot recover
  it, and no one can make us.

---

## 3. What we cannot protect you from

Honesty about limits — these are properties of the design, not bugs:

1. **The link is the capability.** Whoever holds your meld link can read
   `context_a` (on standard melds). Share it exactly as carefully as you'd
   share the content itself.
2. **A compromised Party B is a compromised answer.** If you send a link to
   an attacker, they can answer with lies. Mitigate it: set a PIN (told
   out-of-band) and require a signed answer you can verify.
3. **E2E melds punish lost links.** The encryption key is in the link.
   Bookmark it. There is no "forgot my key."
4. **Metadata is not encrypted.** Timing, size, and IP of requests are
   visible to the server and (for IP) to network operators, even on E2E melds.

---

## 4. Where the model is going

This is the MVP trust model. The layers are built so each can be tightened
without changing the product's shape:

| Layer | MVP today | Future tightening |
|---|---|---|
| Reading context | Plaintext by default, E2E optional | E2E on by default |
| Answer integrity | Optional PIN + optional signature evidence | Commit-reveal for agent flows |
| Identity | None (capabilities only) | None — this never changes |
| Abuse defense | Rate limits per IP; PoW machinery dormant | Proof-of-work enabled under load |
| Payment truth | 35-day leases + append-only ledger | No change expected |

Things we will never add, by principle: user accounts, content scanning,
read receipts, or any persistence of meld content beyond its TTL.

---

## 5. Hosting posture

Subscriber emails are stored **only as SHA-256 hashes** at rest — a leaked
`pros.json` or payments ledger reveals no identities (Stripe holds the
email↔customer mapping; we deliberately do not). The audit ledger is
append-only and survives restarts; meld content does not and never will.
Operational hardening (disk encryption, swap policy, seizure analysis):
[ops/HOSTING.md](ops/HOSTING.md).

## 6. Verify, don't trust

meld is [AGPL open source](https://github.com/lemonaide152/meld). The hosted
service runs exactly this code. Two things you can check yourself:

- **The ciphertext test**: create an E2E meld, then `GET /api/melds/<code>`
  — you will see `meld1:…` base64, not your text.
- **The token test**: read your result twice — the second read requires the
  fresh token from the first response. The old one is dead.
