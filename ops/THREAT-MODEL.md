# meld — Threat Model & Security Assessment

Date: 2026-09-20 · Target: https://meld.mergeinc.workers.dev (Cloudflare Workers + D1)
Method: adversarial review of deployed worker.py + live black-box probing of every route.
Tester: meldsec profile. All findings verified with live probes, not static guessing.

---

## 1. Assets & Trust Boundaries

| Asset | Where it lives | Sensitivity |
|---|---|---|
| context_a (A's context) | D1 `melds.context_a`, ciphertext if E2E meld | High — this is the product |
| context_b (B's answer) | D1 `melds.context_b`, ciphertext if E2E | High |
| owner_token | D1 `melds.owner_token` (256-bit hex) | Critical — capability to read A's context |
| URL key #k= (E2E) | Browser fragment only — never sent to server | Critical — the entire zero-knowledge claim |
| Pro leases | D1 `pros` (hashed email → expiry) | Medium — monetization gate |
| API keys | D1 `api_keys` (sha256 hash, 192-bit key) | Critical — metered agent access |
| Stripe webhook secret | Worker secret | Critical — forges leases if leaked |
| creator_ip | D1 `melds.creator_ip` | Low — rate-limit identity |

Trust boundaries: internet ↔ Cloudflare edge (CF-Connecting-IP set here) ↔ Worker
(Python/ASGI) ↔ D1. Browser ↔ SPA (key in fragment). Stripe ↔ webhook (HMAC).

## 2. Adversaries

- **A1 — anonymous scanner**: no link, probes endpoints, fuzzes params.
- **A2 — link-holder without key (B minus capability)**: has share link, no
  owner token / no #k= key / no PIN.
- **A3 — malicious owner or B**: completed their role, wants more (read the
  other side early, extend life, break the other party's access).
- **A4 — freeloader**: wants unlimited free melds (undermines revenue).
- **A5 — lease thief**: wants free Pro / someone else's API quota.
- **A6 —Stripe forger**: sends fake webhook events to mint leases.
- **A7 — infrastructure attacker**: targets Cloudflare/D1 config, secrets.

## 3. Findings & Disposition (this assessment)

### Fixed during this assessment (verified on live deployment)

- **C1 · CRITICAL — `/debug/config` leaked config state publicly** (revealed
  `rk_live_` key prefix, secret-set booleans). Removed; route now falls through
  to the SPA. Verified: returns HTML, no JSON.
- **C2 · CRITICAL — free-tier limit was entirely bypassed (unlimited free
  melds)**. Root cause: `pros` table contained test leases keyed by RAW IP as
  `email_key`/`customer_id`, and the lease lookup matched `email_key = ? OR
  customer_id = ?` against the client IP → any IP with a stale row got
  unlimited pro. Fixed two ways: (1) deleted the 2 IP-keyed rows from prod D1
  (verified `changes: 2`); (2) hardened `_check_meld_limit` — lease lookup now
  matches hashed email keys ONLY, never IPs. Verified: create now returns 429
  "Free limit reached" from an IP with 40+ melds/hr.
- **H1 · HIGH — expired melds were never deleted** (broken ephemeral promise +
  unbounded D1 growth). No scheduled handler existed; `DELETE FROM melds` only
  in the manual dissolve endpoint; the result endpoint served expired melds
  forever with a valid token. Fixed: lazy deletion on every touchpoint
  (view/resolve/agent resolve/agent result now delete on 410) + amortized
  sweeper in create (`DELETE FROM melds WHERE expires_at <= now`). Verified by
  deploy + suite.
- **H2 · MEDIUM-HIGH — XFF rotation appeared to bypass rate limiting**.
  Probing showed 8 melds with rotated X-Forwarded-For. Root cause was actually
  C2 (each spoofed IP had no lease → unlimited). Post-fix, XFF spoofing
  returns 429 — CF-Connecting-IP is authoritative and cannot be spoofed.
  Verified.

### Accepted / by-design (documented, not fixed)

- **M1 · MEDIUM — token accepted as URL query param** on
  `/api/melds/{code}/result?token=…` (SPA uses header/fragment; API users may
  pass query → log leakage). Mitigation: token rotates on read; 62-bit code +
  256-bit token. Accept for MVP; deprecate query form later.
- **M2 · MEDIUM — CSP allows `script-src 'unsafe-inline'`** (SPA is a single
  inline script). XSS second-line weak, but all contexts are strict
  string-typed and HTML-escaped server-side. Accept for MVP.
- **M3 · MEDIUM — `/v1/keys` proves Pro with email knowledge alone**, no
  rate limit on attempts. An attacker who knows a paying customer's email can
  mint API keys and drain their 10K quota. Email is hashed at rest; the
  practical attack is enumeration of known emails. Fix path: email
  verification loop or requiring the Stripe customer portal session.
- **L1 · LOW — `/api/pro-status?email=` reveals whether an email is a paying
  customer** ({"pro":false}). Enumeration of subscriber status. Accept for MVP.
- **L2 · LOW — PIN brute-force at 10 resolves/min** → 6-digit PIN ≈ 4.6 days
  continuous. PINs are optional; strength is user-chosen.
- **L3 · LOW — idempotent resolve replay**: anyone holding the share link who
  can reproduce B's exact answer can read context_a (rendezvous semantics —
  answering IS the capability). By design; E2E encryption covers the
  confidentiality case.
- **N1 · NOTE — ALLOWED_IPS/BETA_KEYS middleware is dead code** (unset →
  inert). No risk; remove or wire up deliberately later.

### Verified secure (attacked, held)

- SQLi via code/context/pin params — parameterized D1 bindings (404s on
  injection payloads; no table results leaked).
- 200KB+ bodies → 400/413; 100K context cap enforced.
- Stripe webhook: unsigned → 400, wrong HMAC → rejected; no lease minted
  (verified 0 rows in `pros` for attacker customer).
- Token compare — constant-time `secrets.compare_digest`; 256-bit tokens;
  12-char codes (62^12 ≈ 3.2^19 bits... effectively unguessable).
- Token rotation on read — old token invalid after owner reads result.
- E2E melds — server stores ciphertext only; key never leaves the fragment.
- Secrets at rest — Stripe key/whsec as Worker secrets, never in code or
  wrangler.toml; emails only as sha256 hashes.

## 4. Memory / Data-Lifecycle Management (Workers model)

- No in-RAM state (Workers are stateless per request) — everything in D1.
- Ephemeral lifecycle now enforced in code: expired melds are DELETED lazily
  on every access + amortized sweep on create. D1 storage is bounded by
  live melds + up to ~1h of stale rows.
- Resolved melds TTL collapses to ~10 min (MIN(expires_at, fast_expiry)) —
  bounded post-resolution retention.
- `pros` grows only with paying users (one row per subscriber email hash).
- `api_keys`/usage rows grow with agent signups — bounded by revenue.
- Residual risk: no *scheduled* sweep yet — if create traffic stops entirely,
  stale rows linger (harmless: unreadable via API, still occupy D1). Add a
  Cloudflare cron trigger + scheduled handler as hardening later.

## 5. Residual Risks & Next Hardening (priority order)

1. `/v1/keys` email-proof weakness (M3) — add verification or rate limiting.
2. Cloudflare cron trigger for a true scheduled sweeper.
3. Deprecate query-string token on result (M1).
4. Tighten CSP when SPA refactors (M2).
5. Delete dead ALLOWED_IPS/BETA_KEYS middleware or configure it deliberately (N1).
6. D1 automated backups + restore drill (operational, not adversarial).

## 6. Trust-Model Posture After Fixes

- T1 (answer once): holds — resolve is idempotent-by-answer, then 409.
- T2 (token = revocable deed): holds — rotation verified.
- T3 (server honest-but-blind): holds for E2E melds; plaintext melds remain
  server-readable by design (user chooses per meld).
- T4 (pro = paid lease): now actually holds — lease cannot be claimed by IP,
  forged webhooks rejected, expired leases fall through to free limits.
- T5 (meter can't be gamed): holds now — free limit enforced on the
  unspoofable edge IP; XFF rotation verified ineffective post-fix.
