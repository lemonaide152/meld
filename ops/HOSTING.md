# Hosting & Survivability Runbook (L3–L5)

Companion to [TRUST.md](TRUST.md): the trust model states what the server
can see; this runbook states how to host so that remains true under stress.

## Threat-informed requirements

| Threat | Requirement |
|---|---|
| VPS provider reads disk | Full-disk encryption (LUKS); RAM dict never swaps to plaintext |
| Server seized | Nothing decryptable at rest: melds are RAM-only or ciphertext; subscriber emails are hashed |
| Process crash | Leases + audit ledger survive (append-only, fsync'd); meld content does not — by design |
| Whole host loss | Rebuild from git + two state files; RPO for paid features = 0, for melds = their TTL (accepted) |
| Legal demand for content | Content is RAM-only/ciphertext; the honest answer is "we do not have it" |

## Deployment checklist

1. **Host**: any VPS. Enable LUKS full-disk encryption at provision time.
2. **Swap**: `swapoff -a` (meld content lives in RAM; swapping to an
   unencrypted swapfile would defeat E2E). If swap is unavoidable, encrypt it.
3. **systemd hardening** (add to `meld.service`):
   ```ini
   NoNewPrivileges=yes
   ProtectSystem=strict
   ProtectHome=yes
   PrivateTmp=yes
   ReadWritePaths=/opt/meld
   ```
4. **State files** (the only durable data): `pros.json` (hashed emails),
   `payments_ledger.jsonl` (hashed identities + lease events). Back them up
   encrypted; they contain no plaintext PII but are still payment-adjacent.
5. **Secrets**: `.env` (Stripe keys) mode 600, never committed.
6. **Logs**: uvicorn access logs contain IPs — rotate and expire fast
   (`logrotate`, 7 days), or disable access logs entirely for max privacy.
7. **Updates**: `git pull && systemctl restart meld`. Downtime = seconds;
   in-flight melds die with the process (accepted, documented in TRUST.md).

## What seizure actually looks like

- Live melds: in RAM → gone at process death. Not on disk, not recoverable.
- E2E melds: on disk as `meld1:…` ciphertext, keys never present.
- Subscriber identities: `sha256:` hashes — cannot be reversed to emails.
- Payment linkage: Stripe holds the email↔customer mapping, not us.
