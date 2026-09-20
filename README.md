# meld — ephemeral context bridge

**Don't meet. Meld.**

One link. Pour in context. Done.

meld is a minimal context-sharing primitive for humans and agents. Create a link, share it, each party provides their context, and when the exchange resolves the link dissolves. No history, no threads, no accounts.

## Get started (self-host)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn
uvicorn meld:app --host 0.0.0.0 --port 8080

# Open
open http://localhost:8080
```

No config required. Payments are disabled unless Stripe env keys are set
(`MELD_SELF_HOSTED=1` declares it explicitly).

## API

### Create a meld (human flow)
```bash
curl -X POST http://localhost:8080/api/melds \
  -H "Content-Type: application/json" \
  -d '{"context": "What architecture supports 10M users?"}'
# → {"code": "abc123…", "url": "…/m/abc123", "owner_token": "…", "owner_url": "…#t=…"}
```
Keep the `owner_url` (bookmark it) — it's the only way to read the result.

### Resolve a meld (Party B)
```bash
curl -X POST http://localhost:8080/api/melds/<code>/resolve \
  -H "Content-Type: application/json" \
  -d '{"context": "Event-driven microservices + Kafka"}'
```

### Agent flow — the meld link speaks JSON
```bash
# Same URL humans open, with a JSON Accept header:
curl -H "Accept: application/json" http://localhost:8080/m/<code>
# → context_a + api hints for resolve/result

curl -X POST http://localhost:8080/api/melds/<code>/resolve \
  -H "Content-Type: application/json" \
  -d '{"context": "K8s HPA + Nginx"}'
```

### Read the result (owner only)
```bash
curl -H "X-Meld-Token: $OWNER_TOKEN" http://localhost:8080/api/melds/<code>/result
```

## Trust model

meld has **no accounts** — authority comes from secrets you hold. The short
version: standard melds are readable by the server while they exist (max 1
hour, 10 minutes after resolution); **end-to-end encrypted melds** (checkbox
on create) are client-side AES-256-GCM — the server stores only ciphertext,
and the key lives solely in your link's `#k=` fragment. Lose the link, lose
the meld.

Full statement, including what we *cannot* protect you from:
**[TRUST.md](TRUST.md)** — served live at `/trust` on any instance.

## Security

- Capability-based: the code admits, the PIN (optional) authenticates the
  answerer, the token (rotating on every read) reads the result
- Rate limits per IP, 200KB body cap, 100K char contexts, global live-meld guard
- Subscriber emails stored only as SHA-256 hashes at rest
- Proof-of-work abuse gate (dormant, env-enabled)

## Stack

Python / FastAPI, one file, in-memory store, SQLite-free, zero accounts.
