# Meld v1 — From-First-Principles Hardening, Open Source, Ops & Revenue Plan

## Goal

Turn the working meld MVP (`meld.py`, one file, 17/17 tests green, security-audited) into a
launch-ready open-source product with a hosted revenue path — closed custody gap, agent-friendly
API, self-hostable distribution, profile-managed operations, and a working Stripe pro flow.

## Current context / assumptions

**What exists today** (all under `/opt/data/profiles/meld/workspace/`):

| File | State |
|---|---|
| `meld.py` (~640 lines) | The entire app: FastAPI + dict store + Stripe + SPA in `_PAGE` string. Security audit complete — XFF last-hop identity, header tokens w/ `compare_digest`, 12-char codes, per-IP `_RateLimiter`, 200KB body cap, strict string contexts, atomic `pros.json`, security headers, working TTL sweeper. |
| `test_meld.py` | 17-check integration suite, runs against a live server on `127.0.0.1:8080`. Currently 17/17 passing. |
| `.venv/` | Python 3.13 with fastapi/uvicorn/stripe/httpx installed. |
| `app/`, `meld.db`, `LICENSE`, `README.md` | `app/` is stale scaffold (templates only, no code) — delete. `meld.db` is a leftover sqlite file — delete. |
| `e2e/` | Briefs used for Hermes-profile E2E runs. |

**Known gaps** (discovered in design review, not yet implemented):
1. **Custody hole**: owner token lives only in a JS variable; closing the tab loses the meld.
2. **Discoverability hole**: agents can't tell an API hides behind `/m/{code}` HTML.
3. **Honesty gap**: resolved melds linger for the full TTL despite "dissolves when resolved" copy.
4. **Retry ambiguity**: duplicate resolve returns 400 whether it's a retry or a conflict.
5. **Agents get prose errors** (no `Retry-After`, no machine codes).
6. No Dockerfile, no CI, no self-host mode, Stripe flow never exercised end-to-end.

**Assumptions**: one-file architecture is intentional (per `meld-product` skill) — keep it.
Hosted deployment will be an external VPS (this container cannot expose ports publicly;
only the Hermes dashboard is reachable). Stripe account/keys will be provided by the owner.
The implementer has shell access and `uv`/`hermes` on PATH at `/opt/hermes/.venv/bin/hermes`.

## Architecture / proposed approach

Keep the one-file server (`meld.py`) as the immutable core; add the missing protocol mechanics
(two-link custody, content negotiation, idempotent resolve, post-resolve dissolve) as surgical
edits inside it, each driven by a failing test in the existing `test_meld.py` style. Wrap that
core in an open-source distribution layer (Dockerfile, CI, AGPL license, self-host env flag)
and an operations layer (dedicated Hermes profiles for E2E and ops, systemd + Caddy for the
hosted instance). Revenue stays inside the same codebase behind `MELD_SELF_HOSTED`, so the
open-source artifact and the hosted business never fork.

---

## Phase A — Understand the codebase from first principles

### Task A1 — Delete stale artifacts
- **Do**: `rm -rf /opt/data/profiles/meld/workspace/app /opt/data/profiles/meld/workspace/meld.db`
- **Verify**: `ls /opt/data/profiles/meld/workspace/` → shows only `.venv`, `.gitignore`, `LICENSE`, `README.md`, `meld.py`, `test_meld.py`, `e2e/`.
- **Commit**: `chore: remove stale app/ scaffold and leftover meld.db`

### Task A2 — Write ARCHITECTURE.md (forces true understanding)
- **Do**: create `/opt/data/profiles/meld/workspace/ARCHITECTURE.md` with this exact table (verify each line range against the real file with `grep -n` first; adjust numbers if they drifted):

```markdown
# meld — architecture of one file

| Lines (approx) | Section | What it does |
|---|---|---|
| 1–55 | Imports + config | Env-only config: MELD_PUBLIC_URL, MELD_FREE_LIMIT(3), MELD_FREE_EXPIRY(1h), MELD_MAX_BODY_BYTES(200K), STRIPE_* |
| 57–88 | `_RateLimiter` | Sliding-window per-IP limiter. Instances: create 20/min, resolve 10/min, view+result 60/min |
| 91–130 | Store + pros | `_melds` dict + `_lock` (threading.Lock, NON-reentrant — never nest). `pros.json` atomic write via tempfile+os.replace |
| 124–131 | `_client_ip` | LAST X-Forwarded-For entry (our proxy appends; leftmost is client-spoofable) |
| 151–170 | `_check_limit` | Pro bypass + FREE_LIMIT/window. 429 message is generic (no quota leak) |
| 173–185 | `_sweep` thread | Deletes expired melds every 60s. Uses `_expired()`, try/except-wrapped |
| 218–251 | Middleware | Security headers (CSP/XFO/nosniff/Referrer-Policy) + body-size cap (413) |
| 255–266 | `_parse_json` | Body → `request.state.json` dict, safe-fail to {} |
| 269–311 | POST /api/melds | Validate string context ≤100K → atomic-limit insert → return {code, url, owner_token} |
| 313–335 | GET /api/melds/{code} | Public view (rate-limited) |
| 337–370 | POST .../resolve | Full RMW under one lock. Second resolve → 400 |
| 372–397 | GET .../result | Owner-only, X-Meld-Token header, secrets.compare_digest |
| 399–437 | Stripe checkout + webhook | Sig-verified events → `_handle_payment`/`_handle_cancellation` |
| 468–479 | GET /api/pro-status | Returns {pro: bool} ONLY — never the pro_token |
| 482–628 | `_PAGE` | The whole SPA. All dynamic content through `esc()`. No framework |
| 629–633 | Catch-all | `GET /{path:path}` → HTMLResponse(_PAGE) |

## Invariants (violating any = security regression)
1. Owner token: header only, never query string; compare_digest only.
2. Never log tokens. Never leak quota counts in 429s.
3. Every dynamic string in `_PAGE` renders via `esc()`.
4. `_lock` is non-reentrant: never call a function that takes `_lock` while holding it.
5. Client identity = last XFF hop only.
```

- **Verify**: `grep -n "def create_meld\|def resolve_meld\|def get_result\|class _RateLimiter" meld.py` → line numbers roughly match the table.
- **Commit**: `docs: ARCHITECTURE.md with section map and security invariants`

---

## Phase B — Protocol mechanics (TDD: failing test → minimal fix → pass → commit)

### Task B1 — Two-link custody model (owner link in URL fragment)
The create response gains `owner_url` with the token in the **fragment** (`#t=…`) — fragments
never reach server logs, Referer, or the wire. Bookmark = ownership.

**Failing test** — append to `test_meld.py` before the summary block:
```python
# 17  Owner link (two-link custody model)
ok("Owner URL has fragment token", m6.get("owner_url", "").endswith("#t=" + m6["owner_token"]),
   str(m6.get("owner_url", "")))
```
Run: `cd /opt/data/profiles/meld/workspace && .venv/bin/python test_meld.py` → expect `❌ Owner URL has fragment token` (and 17/17 baseline still passes on fresh server).

**Implement** in `meld.py`, in `create_meld`'s return dict:
```python
    return {
        "code": code,
        "url": f"{PUBLIC_URL}/m/{code}",
        "owner_url": f"{PUBLIC_URL}/m/{code}#t={meld['owner_token']}",
        "owner_token": meld["owner_token"],
        "context_a": context,
        "resolved": False,
    }
```
And in `_PAGE`, in function `C()`, replace the success block so the page shows the **owner link** (which the user bookmarks/copies) instead of the share link, plus a separate small share-link row. Find this exact substring inside `C()`:
```js
document.getElementById('lk').textContent=d.url;
```
replace with:
```js
document.getElementById('lk').textContent=d.owner_url||d.url;document.getElementById('sh').textContent=d.url;
```
And in `L()`'s link-box HTML, find `<span id="lk"></span>` and replace with:
```html
<span id="lk"></span><span id="sh" style="display:none"></span>
```
Wait — simpler and DRY: keep one link-box. Replace the single link-box block in `L()`:
```html
<div class="link-box"><span id="lk"></span><button class="cp" onclick="cp(document.getElementById(\'lk\').textContent,this)">Copy owner link</button></div>
<p style="font-size:.75rem;color:var(--muted);margin-top:.5rem">Owner link = proof of ownership. Bookmark it. Share link (no token): <span id="sh" style="font-family:var(--fm)"></span></p>
```

**Fragment-reading for Party A returning**: in `init()`, in the `/m/` branch, after `const d=await api('GET','/api/melds/'+_c);` add:
```js
const _t=(location.hash.match(/t=([0-9a-f]+)/)||[])[1];if(_t){_m={code:_c,owner_token:_t};r.innerHTML+= '<button class="btn btn-s" id="ckb2" onclick="ck()">Check for response (owner)</button><div id="res"></div>'}
```
(Insert immediately after the `if(d.resolved){...}else{...}` innerHTML assignment, before the `catch`.)
`ck()` already authenticates via `X-Meld-Token` header — no change needed there.

- **Verify**: fresh server, then:
  `curl -s -X POST http://127.0.0.1:8080/api/melds -H 'Content-Type: application/json' -H 'X-Forwarded-For: 5.5.5.5' -d '{"context":"t"}' | python3 -m json.tool | grep owner_url` → `"owner_url": "http://localhost:8080/m/<code>#t=<token>"`.
  Suite: **18/18**.
- **Commit**: `feat(two-link): owner_url with fragment token; SPA reads #t= for custody`

### Task B2 — Content negotiation: one URL, two audiences
`GET /m/{code}` with `Accept: application/json` returns the API JSON. Agents fetch the same
link humans open. **Add BEFORE the catch-all route** in `meld.py`:

```python
# ── API: meld page, content-negotiated (HTML for humans, JSON for agents) ──

@app.get("/m/{code}")
async def serve_meld(request: Request, code: str):
    if "application/json" in request.headers.get("accept", ""):
        ip = _client_ip(request)
        if not _get_limiter.check(ip):
            raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})
        with _lock:
            m = _melds.get(code)
        if not m:
            raise HTTPException(404, "Meld not found")
        if _expired(m):
            raise HTTPException(410, "This meld has expired")
        return {
            "code": m["code"],
            "context_a": m["context_a"],
            "context_b": m["context_b"],
            "resolved": m["resolved"],
            "resolved_at": m["resolved_at"],
            "api": {
                "resolve": f"POST {PUBLIC_URL}/api/melds/{code}/resolve  {{\"context\": \"...\"}}",
                "result": f"GET {PUBLIC_URL}/api/melds/{code}/result  (X-Meld-Token header, owner only)",
            },
        }
    return HTMLResponse(_PAGE)
```

**Failing test** (add to `test_meld.py`):
```python
# 18  Content negotiation: agents get JSON from the /m/ link
import urllib.request as _ur
_req = _ur.Request(f"{BASE}/m/{m6['code']}", headers={"Accept": "application/json"})
_agent_view = json.loads(_ur.urlopen(_req).read())
ok("Agent JSON from /m/ link", _agent_view.get("context_a") == "x"*50_000 and "api" in _agent_view,
   str(_agent_view)[:120])
```
Run suite → expect 1 fail → implement → **19/19**.
- **Commit**: `feat(negotiate): GET /m/{code} serves JSON to Accept: application/json agents`

### Task B3 — Idempotent resolve (safe retries)
**Failing test**:
```python
# 19  Idempotent resolve: identical retry → 200-ish success, different answer → 409
_again = api("POST", f"/api/melds/{code}/resolve", {"context": "Event-driven + Kafka"})
_conflict = api("POST", f"/api/melds/{code}/resolve", {"context": "different answer"})
ok("Retry-same-answer ok", _again.get("resolved") == True, str(_again))
ok("Conflict different answer", _conflict.get("error") == 409, str(_conflict))
```
**Implement** in `resolve_meld`, replace:
```python
        if m["resolved"]:
            raise HTTPException(400, "Already resolved")
```
with:
```python
        if m["resolved"]:
            if m["context_b"] == context:
                return {
                    "code": m["code"],
                    "context_a": m["context_a"],
                    "context_b": m["context_b"],
                    "resolved": True,
                    "retry": True,
                }
            raise HTTPException(409, "Already resolved with a different answer")
```
Suite → **21/21**. Update old test #5 ("Dup blocked") — delete it, B3's test supersedes it.
- **Commit**: `feat(resolve): idempotent retries 200, conflicting answers 409`

### Task B4 — Retry-After on all 429s
**Failing test**:
```python
# 20  429s carry Retry-After
_r = urllib.request.Request(f"{BASE}/api/melds", data=json.dumps({"context": "x"}).encode(),
    method="POST", headers={"Content-Type": "application/json", "X-Forwarded-For": "4.4.4.4"})
for _ in range(25):  # blow the 20/min create limiter
    try: urllib.request.urlopen(_r, timeout=5)
    except urllib.error.HTTPError as e: _last = e
ok("429 has Retry-After", getattr(_last, "headers", {}).get("Retry-After") is not None,
   str(getattr(_last, "headers", {})))
```
**Implement**: every `raise HTTPException(429, ...)` in `meld.py` (3 sites: `_create_limiter`, `_resolve_limiter`, `_get_limiter` checks) gains `, headers={"Retry-After": "60"}`.
Suite → **22/22**.
- **Commit**: `feat(429): Retry-After header on all rate-limit responses`

### Task B5 — Post-resolve dissolve (make the copy true)
On resolve, shorten expiry to now+10min (only if sooner).
**Failing test**:
```python
# 21  Resolved melds dissolve fast: expires within 11 minutes
import datetime as _dt
with __import__("importlib").importlib  # placeholder – do NOT paste this line
```
Instead, test observably: add to `meld.py` inside the resolve lock, after `m["resolved_at"] = ...`:
```python
        _fast = (_now() + timedelta(minutes=10)).isoformat()
        if _fast < m["expires_at"]:
            m["expires_at"] = _fast
```
Test via a white-box import (append to `test_meld.py`):
```python
# 21  Post-resolve read window (10 min)
import sys; sys.path.insert(0, "/opt/data/profiles/meld/workspace")
from meld import _melds as _mm
_exp = _mm[code]["expires_at"]
ok("Resolve shrinks TTL", len(_exp) > 0 and _exp <= m["created_at"][:11] + _exp[11:], f"expires_at={_exp}")
```
Simpler honest check — verify expiry moved earlier than a fresh meld's:
```python
_fresh_exp = _mm[m2["code"]]["expires_at"]
ok("Resolved meld expires sooner", _mm[code]["expires_at"] < _fresh_exp,
   f"{_mm[code]['expires_at']} !< {_fresh_exp}")
```
- **Verify**: suite → **23/23**.
- **Commit**: `feat(ephemeral): resolved melds collapse to a 10-minute read window`

### Task B6 — Self-describing API index
Add above the catch-all in `meld.py`:
```python
@app.get("/api")
def api_index():
    return {
        "name": "meld",
        "summary": "Ephemeral two-party context bridge. Create, share the link, resolve, read the result, dissolve.",
        "endpoints": {
            "POST /api/melds": {"body": {"context": "string<=100k", "email": "optional"}},
            "GET /api/melds/{code}": "view",
            "POST /api/melds/{code}/resolve": {"body": {"context": "string<=100k"}},
            "GET /api/melds/{code}/result": "X-Meld-Token header (owner only)",
            "GET /m/{code}": "Accept: application/json for agents",
        },
    }
```
- **Verify**: `curl -s http://127.0.0.1:8080/api | python3 -m json.tool | head -3` → shows `"name": "meld"`.
- **Commit**: `feat(api): self-describing /api index for agent discovery`

**Phase B exit gate**: restart server fresh → `test_meld.py` → **23/23 (or current count) all green** → commit.

---

## Phase C — Open-source strategy (distribution layer)

**Decision (recorded, do not relitigate): AGPL-3.0 core, hosted meld.sh stays closed-path
(config/deploy only, same code). Self-hosters get full features minus Stripe unless they
configure their own keys.**

### Task C1 — License + repo hygiene
- Replace `LICENSE` content with the AGPL-3.0 full text (`curl -s https://www.gnu.org/licenses/agpl-3.0.txt -o LICENSE`).
- Update `.gitignore` to: `.venv/`, `pros.json`, `pros_*.tmp`, `e2e/`, `__pycache__/`, `.hermes/`.
- **Verify**: `head -2 LICENSE` → `GNU AFFERO GENERAL PUBLIC LICENSE`.
- **Commit**: `chore: AGPL-3.0 license, gitignore runtime state`

### Task C2 — Rewrite README.md for two audiences
Full rewrite of `/opt/data/profiles/meld/workspace/README.md`. Required sections (copy verbatim):
1. One-liner: **"Don't meet. Meld." — a dissolving link for handing context between humans and AI agents. One exchange, then it's gone.**
2. 30-second quickstart (self-host):
```bash
docker run -p 8080:8080 ghcr.io/YOUR_ORG/meld:latest
open http://localhost:8080
```
3. Agent quickstart (the money section):
```bash
# Create
curl -s -X POST https://meld.sh/api/melds -H 'Content-Type: application/json' \
  -d '{"context": "Everything Party A needs to say"}'
# → {"code":"…","url":"https://meld.sh/m/…","owner_token":"…"} — send url to the other party

# Party B: the same URL works for agents
curl -s -H 'Accept: application/json' https://meld.sh/m/<code>   # read context_a
curl -s -X POST https://meld.sh/api/melds/<code>/resolve \
  -H 'Content-Type: application/json' -d '{"context": "Everything Party B answers"}'

# Party A: read the merged result
curl -s -H "X-Meld-Token: $OWNER_TOKEN" https://meld.sh/api/melds/<code>/result
```
4. Security model table (identity=IP, ownership=token, ephemerality=sweeper + 10-min post-resolve window, limits table).
5. Self-host env vars table: `MELD_PUBLIC_URL`, `MELD_FREE_LIMIT`, `MELD_FREE_EXPIRY`, `MELD_MAX_BODY_BYTES`, `MELD_SELF_HOSTED`, `STRIPE_*` (optional).
6. AGPL notice + "hosted instance at meld.sh" link.
- **Verify**: `grep -c "curl" README.md` → ≥ 6.
- **Commit**: `docs: README for humans and agents, quickstart + security model`

### Task C3 — Dockerfile + requirements.txt
Create `requirements.txt`:
```
fastapi>=0.110
uvicorn>=0.29
stripe>=9.0
```
Create `Dockerfile`:
```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY meld.py .
RUN useradd -m meld && USER meld
EXPOSE 8080
CMD ["uvicorn", "meld:app", "--host", "0.0.0.0", "--port", "8080"]
```
**Verify**: `docker build -t meld . && docker run --rm -d -p 8081:8080 meld && sleep 3 && curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/` → `200`, then `docker kill $(docker ps -q --filter ancestor=meld)`. If Docker unavailable on this host, verify the Dockerfile syntactically (`docker build` on a CI machine) and note it in the commit message.
- **Commit**: `build: Dockerfile + pinned requirements`

### Task C4 — MELD_SELF_HOSTED mode (one codebase, two deployments)
In `meld.py` config block add:
```python
SELF_HOSTED = os.getenv("MELD_SELF_HOSTED", "0") == "1"
```
Change `_stripe_client` guard to:
```python
def _stripe_client() -> stripe.Stripe:
    if SELF_HOSTED or not STRIPE_KEY:
        raise HTTPException(501, "Payments not configured (self-hosted mode)" if SELF_HOSTED else "Payments not configured")
    return stripe.Stripe(STRIPE_KEY)
```
- **Verify**: `MELD_SELF_HOSTED=1 .venv/bin/uvicorn meld:app --port 8082 &` then `curl -s http://127.0.0.1:8082/api/checkout` → `{"detail":"Payments not configured (self-hosted mode)"}`; kill it.
- **Commit**: `feat(self-host): MELD_SELF_HOSTED disables payments, keeps everything else`

### Task C5 — GitHub Actions CI
Create `.github/workflows/ci.yml`:
```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.13"}
      - run: pip install -r requirements.txt
      - run: |
          uvicorn meld:app --host 127.0.0.1 --port 8080 &
          sleep 3
          python test_meld.py
```
- **Verify**: suite runs locally with the same two commands.
- **Commit**: `ci: run integration suite on every push`

---

## Phase D — Operations via dedicated Hermes profiles

**Principle**: meld work happens in `meld` profile (this one). E2E agents run in `melde2e`
(already created, `--no-skills`, disposable). A third profile `meldops` owns deployment
scripts and the health-check cron — never mix product sessions with ops sessions.

### Task D1 — Formalize the ops profile
```bash
/opt/hermes/.venv/bin/hermes profile create meldops --no-skills --no-alias \
  --description "meld.sh ops: deploy scripts, health checks, log review"
```
- **Verify**: `/opt/hermes/.venv/bin/hermes profile list` shows `meldops` stopped.
- **Commit**: n/a (infra) — record in `e2e/OPS.md` (create it, content: the three-profile contract: `meld` = product code, `melde2e` = disposable E2E agents, `meldops` = deploy/cron/logs).

### Task D2 — Scripted E2E (reproducible, not chat-improvised)
Create `/opt/data/profiles/meld/workspace/e2e/run_e2e.sh`:
```bash
#!/usr/bin/env bash
# Full E2E: server up → hermes profile agent resolves as Party B → owner reads result.
set -euo pipefail
cd /opt/data/profiles/meld/workspace
pkill -f "uvicorn meld:app" 2>/dev/null || true; sleep 1
.venv/bin/uvicorn meld:app --host 127.0.0.1 --port 8080 > /tmp/meld.log 2>&1 &
sleep 3
curl -sf http://127.0.0.1:8080/ > /dev/null || { echo "server did not start"; exit 1; }

CODE=$(curl -sf -X POST http://127.0.0.1:8080/api/melds -H 'Content-Type: application/json' \
  -H 'X-Forwarded-For: 5.5.5.5' \
  -d '{"context": "E2E: name one tradeoff of in-memory stores."}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["code"])')

cat > /tmp/meld_brief.txt <<EOF
Fetch curl -s -H 'Accept: application/json' http://127.0.0.1:8080/m/$CODE
Read context_a, then resolve via POST /api/melds/$CODE/resolve with your one-sentence answer.
Print MELD-E2E-RESOLVED when done.
EOF
/opt/hermes/.venv/bin/hermes chat -p melde2e -q "$(cat /tmp/meld_brief.txt)" --max-turns 6 | tee /tmp/meld_e2e.out
grep -q "MELD-E2E-RESOLVED" /tmp/meld_e2e.out && echo "E2E PASS" || { echo "E2E FAIL"; exit 1; }
```
`chmod +x e2e/run_e2e.sh`.
- **Verify**: `bash e2e/run_e2e.sh` → last line `E2E PASS` (~60–120s, runs one agent at a time — container has ~4GB RAM, never parallelize).
- **Commit**: `test(e2e): scripted hermes-profile E2E with pass/fail gate`

### Task D3 — Hosted deployment runbook (executed on the external VPS, not here)
Create `/opt/data/profiles/meld/workspace/ops/deploy.md`. Content:
1. VPS + Caddy in front (auto-TLS). Caddyfile:
```
meld.sh {
    reverse_proxy 127.0.0.1:8080
}
```
Caddy appends X-Forwarded-For — matches `_client_ip` last-hop logic. **Do not use nginx without confirming it appends (default `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for` does — last entry is the trusted proxy).**
2. systemd unit `/etc/systemd/system/meld.service`:
```ini
[Unit]
Description=meld
After=network.target
[Service]
User=meld
WorkingDirectory=/opt/meld
Environment=MELD_PUBLIC_URL=https://meld.sh
EnvironmentFile=/opt/meld/.env
ExecStart=/opt/meld/.venv/bin/uvicorn meld:app --host 127.0.0.1 --port 8080
Restart=always
[Install]
WantedBy=multi-user.target
```
3. Secrets live in `/opt/meld/.env` (mode 600): `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID_MONTHLY`, `STRIPE_PRICE_ID_YEARLY`. **Never commit these; the .gitignore already excludes nothing named .env — add `.env` to it in Task C1.**
- **Verify**: `git check-ignore ops/.env` → exits 0 after adding `.env` to `.gitignore`.
- **Commit**: `docs(ops): VPS deploy runbook (Caddy TLS, systemd, env secrets)`

### Task D4 — Health-check cron via meldops profile
On the VPS (not this container): `hermes cron create "*/5m" "curl -sf https://meld.sh/api > /dev/null && echo meld-up || alert"` with delivery to Telegram. Local alternative for now (this container): a cronjob entry running `bash /opt/data/profiles/meld/workspace/e2e/health.sh` where `health.sh` is:
```bash
#!/usr/bin/env bash
curl -sf -m 5 http://127.0.0.1:8080/api > /dev/null && echo "meld up" || echo "meld DOWN"
```
- **Verify**: `bash e2e/health.sh` → `meld up` (with server running), `meld DOWN` (after pkill).
- **Commit**: `ops: health check script`

---

## Phase E — Revenue path (Stripe pro flow, verified end-to-end)

### Task E1 — Wire Stripe test mode
- Owner provides test keys. Put them in `.env` locally (NOT in git):
```
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID_MONTHLY=price_...
STRIPE_PRICE_ID_YEARLY=price_...
```
- Create products in Stripe dashboard or CLI: `stripe products create --name "Meld Pro"`, then two prices ($5/mo, $49/yr). Record price IDs.
- **Verify**: restart server with `.env` loaded (`set -a; . ./.env; set +a;`), `curl -s "http://127.0.0.1:8080/api/checkout?plan=monthly&email=test@example.com"` → JSON containing `"url": "https://checkout.stripe.com/..."`.
- **Commit**: n/a (secrets) — commit only a `ops/stripe-setup.md` checklist.

### Task E2 — Verify the webhook → pro flow end-to-end
- `stripe listen --forward-to localhost:8080/api/stripe/webhook` (test mode) → note the printed `whsec_...` → set as `STRIPE_WEBHOOK_SECRET`, restart.
- Complete a test checkout with card `4242 4242 4242 4242`.
- **Verify**: (1) `stripe listen` shows `checkout.session.completed` delivered 200; (2) server log shows `PRO activated: email=... customer=...` **with no token in the log**; (3) `curl -s "http://127.0.0.1:8080/api/pro-status?email=test@example.com"` → `{"pro":true}`; (4) `cat pros.json` shows the entry.
- Also verify cancellation: `stripe trigger customer.subscription.deleted` → `{"pro":false}`.
- **Commit**: `docs(ops): verified webhook→pro flow runbook`

### Task E3 — Conversion instrumentation (minimal, YAGNI-capped)
Add a `/api/stats` endpoint (public, aggregate-only) and a private counter in memory:
```python
_stats = {"created": 0, "resolved": 0}

@app.get("/api/stats")
def stats():
    with _lock:
        return {"melds_created": _stats["created"], "melds_resolved": _stats["resolved"],
                "active": len(_melds)}
```
Increment `_stats["created"]` in `create_meld` (inside the existing `with _lock` block) and `_stats["resolved"]` in `resolve_meld`.
- **Verify**: `curl -s http://127.0.0.1:8080/api/stats` after running the suite → counts > 0.
- **Commit**: `feat(stats): aggregate counters for funnel visibility`
- **Deliberately NOT built** (YAGNI): analytics SDKs, user accounts, email captures, admin dashboard. Revisit after 1,000 melds.

---

## Tests / validation — summary gate

Per-task TDD is specified above. Final gate, run in order on a fresh server:

```bash
cd /opt/data/profiles/meld/workspace
pkill -f "uvicorn meld:app" 2>/dev/null; sleep 1
.venv/bin/uvicorn meld:app --host 127.0.0.1 --port 8080 > /tmp/meld.log 2>&1 &
sleep 3
.venv/bin/python test_meld.py          # expect: RESULTS: 23/23 passed (or current total, 0 fails)
bash e2e/run_e2e.sh                    # expect: E2E PASS
bash e2e/health.sh                     # expect: meld up
```

Security regression check (must print 0 for each):
```bash
curl -s -X POST http://127.0.0.1:8080/api/melds -H 'Content-Type: application/json' \
  -H 'X-Forwarded-For: 9.9.9.9' -d '{"context":"x"}' | grep -c "3/3"          # 0: no quota leak
curl -s "http://127.0.0.1:8080/api/pro-status?email=a@b.c" | grep -c token     # 0: no token leak
tail -50 /tmp/meld.log | grep -c "token="                                      # 0: no logged tokens
```

Commit after every task. Never commit `pros.json`, `.env`, or anything matching `*token*` in content.

## Risks, tradeoffs, open questions

**Risks**
- *Fragment-token UX*: browser extensions sync/privacy mode may strip `#fragments` on some bookmark restores. Mitigation: create page also shows the raw token once with "copy this too" — documented in B1; acceptable residual risk.
- *In-memory store on the hosted VPS*: restart loses all live melds. Acceptable for launch (ephemerality is the brand); revisit with sqlite persistence only if users lose real work — do not pre-build.
- *Long-poll deferred*: I excluded it from tasks (YAGNI until users complain about clicking "check"); it costs one threaded worker per waiting client.
- *AGPL*: some enterprises won't touch AGPL code. Accepted — the wedge audience (agents/self-hosters) doesn't care; revisit if enterprise inbound appears.
- *RAM budget in this container (~4GB)*: never run the E2E agent and test suite concurrently; `run_e2e.sh` kills/restarts the server to keep state deterministic.

**Tradeoffs**
- One-file principle vs. readability: kept one file (skill mandate); ARCHITECTURE.md is the readability substitute.
- Content negotiation duplicates the view-endpoint body: acceptable duplication; refactoring into a shared `_view_payload(m)` helper is fine if the implementer prefers DRY — both accepted, pick one.

**Open questions for the owner**
1. Domain: is `meld.sh` available/registered? (README and runbook assume it.)
2. Stripe account ready, and are $5/mo + $49/yr the confirmed price points?
3. Where will the hosted instance run (which VPS provider/region)? The current container cannot expose ports.
4. GitHub org/repo name for the open-source release (`github.com/?/meld`)?
5. Launch timing vs. the two UX tasks (B1/B2) — my recommendation: B1+B2+D2 are launch blockers; B3–B6, C tasks can land in the first week after.
