# Security ship-blockers SB-1 / SB-2 / SB-3 — 2026-09-26 (PT)

Cleared for lemonaide152/meld before public ship.

| ID | Fix |
|---|---|
| SB-1 | Bare `GET /api/melds/{code}` = metadata only; plaintext needs `X-Meld-Token`. E2E (`meld1:`) ciphertext may appear on bare GET for `#k=` clients. |
| SB-2 | Browser create E2E checkbox **default ON**. |
| SB-3 | New meld rows: empty `creator_ip`; `resolver_ip` no longer written. Rate limits use edge IP in `rate` / `free_counts` / `ip_throttle` only. |

## Deploy

- Git SHA: `1af712cf7437f6dc6c1649248dba79276cc4e393`
- Worker version: `e8ddd81a-9146-4924-a3d5-db4569ab8e0a`
- Live: https://meld.mergeinc.workers.dev

## Design (SB-1)

One coherent rule: **never return plaintext bodies without owner token**. Returning E2E ciphertext on bare GET is intentional and documented — it is not plaintext.
