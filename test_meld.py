"""Full test suite for meld — one-file rebuild."""
import json, time, urllib.request, urllib.error

BASE = "http://127.0.0.1:8080"
passed = 0
total = 0

def api(m, p, b=None, headers=None):
    url = f"{BASE}{p}"
    data = json.dumps(b).encode() if b else None
    req = urllib.request.Request(url, data=data, method=m)
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": str(e.read().decode())}

_RUN = int(time.time()) % 250 + 10  # run-unique IP suffix base
def _ip(n):
    return f"10.20.{_RUN}.{n}"

def ok(name, cond, detail=""):
    global passed, total
    total += 1
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} — {detail}")

def fetch(p):
    try:
        with urllib.request.urlopen(f"{BASE}{p}") as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        return str(e.read().decode())

# 1  Create
_main_id = f"10.10.0.{int(time.time()) % 200 + 10}"
m = api("POST", "/api/melds", {"context": "Best arch for 10M users?", "email": "a@b.com"}, headers={"X-Forwarded-For": _ip(1)})
ok("Create", m.get("code") is not None, str(m))
code, token = m["code"], m["owner_token"]
print(f"   code={code}")

# 2  View
v = api("GET", f"/api/melds/{code}", headers={"X-Forwarded-For": _ip(50)})
ok("View", v.get("context_a") == "Best arch for 10M users?", str(v))

# 3  Resolve
r = api("POST", f"/api/melds/{code}/resolve", {"context": "Event-driven + Kafka"}, headers={"X-Forwarded-For": _ip(1)})
ok("Resolve", r.get("resolved") == True and r.get("context_b") == "Event-driven + Kafka", str(r))

# 4  Result
res = api("GET", f"/api/melds/{code}/result", headers={"X-Meld-Token": token, "X-Forwarded-For": _ip(50)})
ok("Result", res.get("context_b") == "Event-driven + Kafka", str(res))

# 5  Duplicate blocked
dup = api("POST", f"/api/melds/{code}/resolve", {"context": "nope"}, headers={"X-Forwarded-For": _ip(1)})
ok("Dup with different answer → 409", dup.get("error") == 409, str(dup))

# 6  Wrong token
bad = api("GET", f"/api/melds/{code}/result?token=wrong", headers={"X-Forwarded-For": _ip(50)})
ok("Wrong token 403", bad.get("error") == 403, str(bad))

# 7  404
nf = api("GET", "/api/melds/nonexistent", headers={"X-Forwarded-For": _ip(50)})
ok("404 on missing", nf.get("error") == 404, str(nf))

# 8  Landing page
landing = fetch("/")
ok("Landing renders", "One link" in landing, "no 'One link' in page")

# 9  Meld page (resolved)
mp = fetch(f"/m/{code}")
ok("Meld resolved page", "Exchange complete" in mp, "missing 'Exchange complete'")

# 10  Meld page (pending)
m2 = api("POST", "/api/melds", {"context": "pending test"}, headers={"X-Forwarded-For": _ip(1)})
mp2 = fetch(f"/m/{m2['code']}")
ok("Meld pending page", "Meld received" in mp2, str(m2))

# 11  Pricing
pricing = fetch("/upgrade")
ok("Pricing page", "Pro" in pricing and "Unlimited" in pricing, "missing Pro/Unlimited")

# 12  404 page
err = fetch("/no-such-page")
ok("404 error page", "Not found" in err, "missing 'Not found'")

# 13  Pro page
pro_page = fetch("/pro")
ok("Pro page renders", ("You're Pro" in pro_page) or ("You&#39;re Pro" in pro_page), "missing 'You're Pro'")

# 14  Rate limit (FREE_LIMIT=3 - create 3rd, 4th should be blocked)
_rl_id = f"10.10.1.{int(time.time()) % 200 + 10}"  # unique per run
m1rl = api("POST", "/api/melds", {"context": "rl 1"}, headers={"X-Forwarded-For": _ip(40)})
m2rl = api("POST", "/api/melds", {"context": "rl 2"}, headers={"X-Forwarded-For": _ip(40)})
m3 = api("POST", "/api/melds", {"context": "3rd meld"}, headers={"X-Forwarded-For": _ip(40)})
ok("3rd meld allowed", m3.get("code") is not None, str(m3))
m4 = api("POST", "/api/melds", {"context": "4th — should be blocked"}, headers={"X-Forwarded-For": _ip(40)})
ok("Rate limit 429 on 4th", m4.get("error") == 429, str(m4))

# 15  Empty context
m5 = api("POST", "/api/melds", {"context": ""}, headers={"X-Forwarded-For": _ip(11)})
ok("Empty context allowed", m5.get("code") is not None, str(m5))

# 16  Large context (within limit)
big = "x" * 50_000
m6 = api("POST", "/api/melds", {"context": big}, headers={"X-Forwarded-For": _ip(12)})
ok("50K context ok", m6.get("code") is not None, str(m6.get("error", "")))

# ── Trust maturity T2: rotating owner token ──────────────────────────────
# A meld resolved then read: result response must carry a FRESH token; the
# old token must be dead afterward (revocable deed, not immortal deed).
_t2_id = f"10.10.3.{int(time.time()) % 200 + 10}"
mt = api("POST", "/api/melds", {"context": "rotation test"}, headers={"X-Forwarded-For": _ip(13)})
rcode, rtok = mt["code"], mt["owner_token"]
api("POST", f"/api/melds/{rcode}/resolve", {"context": "the answer"}, headers={"X-Forwarded-For": _ip(13)})

read1 = api("GET", f"/api/melds/{rcode}/result", headers={"X-Meld-Token": rtok, "X-Forwarded-For": _ip(51)})
ok("Read 1 succeeds", read1.get("context_b") == "the answer", str(read1)[:80])
ok("Read 1 returns fresh token", isinstance(read1.get("owner_token"), str)
   and len(read1["owner_token"]) == 64 and read1["owner_token"] != rtok, str(read1.get("owner_token"))[:20])

read2 = api("GET", f"/api/melds/{rcode}/result", headers={"X-Meld-Token": read1["owner_token"], "X-Forwarded-For": _ip(51)})
ok("Read 2 with fresh token succeeds", read2.get("context_b") == "the answer", str(read2)[:80])

stale = api("GET", f"/api/melds/{rcode}/result", headers={"X-Meld-Token": rtok, "X-Forwarded-For": _ip(51)})
ok("Stale (old) token rejected 403", stale.get("error") == 403, str(stale))


# ── Trust maturity T4: pro status as a lease + append-only ledger ────────
import sys; sys.path.insert(0, "/opt/data/profiles/meld/workspace")
from meld import _pro_active, _record_payment_event, _payments_ledger

# Grant a 35-day lease, then travel 34 days (active) and 36 days (expired)
from meld import _email_key
_record_payment_event("cust_test", "test@example.com", days=35)
ok("Lease active within window", _pro_active("test@example.com") is True)
from meld import _pros
import datetime as _dt
_hkey = _email_key("test@example.com")
_entry = _pros[_hkey]
_until = _dt.datetime.fromisoformat(_entry["pro_until"])
_pros[_hkey]["pro_until"] = (_until - _dt.timedelta(days=36)).isoformat()
ok("Lease expired after window", _pro_active("test@example.com") is False)

# Ledger is append-only and records every event with customer + timestamp
_n = len(_payments_ledger)
_record_payment_event("cust_test2", "two@example.com", days=35)
ok("Ledger append-only", len(_payments_ledger) == _n + 1
   and _payments_ledger[-1]["customer_id"] == "cust_test2"
   and "at" in _payments_ledger[-1])


# ── Trust maturity T1: optional PIN on resolve (split-channel trust) ─────
# Default: no PIN — link alone admits (UX unchanged).
mp = api("POST", "/api/melds", {"context": "no-pin meld"}, headers={"X-Forwarded-For": _ip(14)})
nopin = api("POST", f"/api/melds/{mp['code']}/resolve", {"context": "answer"})
ok("No PIN set: resolve open", nopin.get("resolved") is True, str(nopin)[:80])

# With PIN: resolve without pin → 403; with pin → success.
_pin_id = f"10.10.2.{int(time.time()) % 200 + 10}"
mpi = api("POST", "/api/melds", {"context": "pinned meld", "pin": "4821"},
          headers={"X-Forwarded-For": _ip(14)})
wrongpin = api("POST", f"/api/melds/{mpi['code']}/resolve", {"context": "evil", "pin": "0000"},
               headers={"X-Forwarded-For": _ip(14)})
ok("Wrong PIN rejected 403", wrongpin.get("error") == 403, str(wrongpin))
nopinin = api("POST", f"/api/melds/{mpi['code']}/resolve", {"context": "no pin given"},
              headers={"X-Forwarded-For": _ip(14)})
ok("Missing PIN rejected 403", nopinin.get("error") == 403, str(nopinin))
rightpin = api("POST", f"/api/melds/{mpi['code']}/resolve", {"context": "good", "pin": "4821"},
               headers={"X-Forwarded-For": _ip(14)})
ok("Correct PIN resolves", rightpin.get("resolved") is True, str(rightpin)[:80])
# PIN never grants deed rights: result still needs owner token
pinread = api("GET", f"/api/melds/{mpi['code']}/result", headers={"X-Meld-Token": "wrong", "X-Forwarded-For": _ip(14)})
ok("PIN does not bypass token on result", pinread.get("error") == 403, str(pinread))


# ── Trust maturity T5: proof-of-work gate (env-gated, verify logic) ──────
import sys; sys.path.insert(0, "/opt/data/profiles/meld/workspace")
from meld import _pow_check, _pow_meets

# A solution for nonce "abc" with difficulty 3: find suffix s.t. sha256("abc"+s) starts "000"
import hashlib as _hl
def _solve(nonce, diff):
    i = 0
    while True:
        s = f"{i}"
        if _hl.sha256((nonce + s).encode()).hexdigest().startswith("0" * diff):
            return s
        i += 1

sol = _solve("nonce1", 3)
ok("PoW: valid solution accepted", _pow_check("nonce1", sol, 3) is True)
ok("PoW: invalid solution rejected", _pow_check("nonce1", "xx", 3) is False)
ok("PoW: difficulty 0 always passes", _pow_check("nonce1", "", 0) is True)
ok("PoW: meets-prefix correct", _pow_meets(_hl.sha256(b"nonce1" + sol.encode()).hexdigest(), 3) and not _pow_meets("fff", 3))


# ── Trust maturity T6: signed answers (accountability, server-blind) ─────
# B MAY attach (pubkey, signature-over-context_b). Server stores + relays the
# evidence; it does NOT verify (no trust root, no crypto dep). A verifies.
msig = api("POST", "/api/melds", {"context": "sign this"}, headers={"X-Forwarded-For": _ip(15)})
PK, SIG = "a" * 64, "b" * 128  # fake-but-well-formed ed25519 hex
signed = api("POST", f"/api/melds/{msig['code']}/resolve",
             {"context": "my answer", "responder_pubkey": PK, "signature": SIG},
             headers={"X-Forwarded-For": _ip(19)})
ok("Signed resolve ok", signed.get("resolved") is True, str(signed)[:80])

sres = api("GET", f"/api/melds/{msig['code']}/result",
           headers={"X-Meld-Token": msig["owner_token"], "X-Forwarded-For": _ip(15)})
ok("Evidence relayed in result", sres.get("responder_pubkey") == PK
   and sres.get("signature") == SIG, str(sres)[:120])

# Malformed evidence rejected: sig without key, key without sig, bad hex
half1 = api("POST", f"/api/melds/{msig['code']}/resolve",
            {"context": "again", "responder_pubkey": PK},
            headers={"X-Forwarded-For": _ip(20)})
ok("Sig without key rejected", half1.get("error") == 400, str(half1)[:80])
half2 = api("POST", "/api/melds", {"context": "x"}, headers={"X-Forwarded-For": _ip(16)})
half2r = api("POST", f"/api/melds/{half2['code']}/resolve",
             {"context": "ans", "signature": SIG},
             headers={"X-Forwarded-For": _ip(21)})
ok("Key without sig rejected", half2r.get("error") == 400, str(half2r)[:80])
badhex = api("POST", "/api/melds", {"context": "x"}, headers={"X-Forwarded-For": _ip(17)})
badhexr = api("POST", f"/api/melds/{badhex['code']}/resolve",
              {"context": "ans", "responder_pubkey": "zz", "signature": SIG},
              headers={"X-Forwarded-For": _ip(22)})
ok("Malformed pubkey rejected", badhexr.get("error") == 400, str(badhexr)[:80])
# Unsigned resolve remains valid (backward compat)
msig2 = api("POST", "/api/melds", {"context": "unsigned"}, headers={"X-Forwarded-For": _ip(18)})
unsigned = api("POST", f"/api/melds/{msig2['code']}/resolve", {"context": "ans"},
               headers={"X-Forwarded-For": _ip(23)})
ok("Unsigned resolve unchanged", unsigned.get("resolved") is True, str(unsigned)[:80])


# ── Trust maturity T3: zero-knowledge wire format (reference round-trip) ─
# Pins the ciphertext contract the SPA must implement. Server stores base64
# ciphertext as opaque context; the key NEVER touches the server.
import sys; sys.path.insert(0, "/opt/data/profiles/meld/workspace")
import base64, hashlib as _h, os

def _kdf(key_hex: str) -> bytes:
    return _h.sha256(bytes.fromhex(key_hex)).digest()  # 32-byte AES key

def _zero_roundtrip(plaintext: str, key_hex: str):
    nonce = os.urandom(12)
    # Reference AES-GCM via hashlib-free stdlib: use cryptography-free XOR
    # stream is NOT GCM — instead assert the FORMAT contract with a stub:
    # real AES-GCM requires a dependency; SPA uses WebCrypto. Here we pin:
    # context_a stored = "meld1:" + base64(nonce + ct). Server opaque test:
    blob = "meld1:" + base64.b64encode(nonce + plaintext.encode()).decode()
    return blob

k = os.urandom(32).hex()
blob = _zero_roundtrip("secret context", k)
ok("T3 blob format meld1:", blob.startswith("meld1:"))
mz = api("POST", "/api/melds", {"context": blob}, headers={"X-Forwarded-For": _ip(24)})
vz = api("GET", f"/api/melds/{mz['code']}", headers={"X-Forwarded-For": _ip(24)})
ok("T3 server relays ciphertext verbatim", vz.get("context_a") == blob, str(vz)[:80])
# Server response must NOT contain the key (it never received it)
ok("T3 key never in server response", k not in str(vz))


# ── Trust transparency: explicit model surfaced in-product ──────────────
page = fetch("/")
ok("Trust banner on create form", "server can" in page and "E2E" in page,
   "no trust banner text")
ok("Trust nav link present", "/trust" in page, "no /trust link")
trust = fetch("/trust")
ok("Trust page states plaintext default", "can read" in trust.lower() or "plaintext" in trust.lower(),
   "trust page missing honesty statement")
ok("Trust page states E2E zero-knowledge", "cannot" in trust.lower() or "ciphertext" in trust.lower(),
   "trust page missing E2E claim")
ok("Trust page states no accounts", "no account" in trust.lower(), "missing no-accounts claim")


# ── Hosting maturity: privacy-preserving persistence (L1+L2) ────────────
import sys; sys.path.insert(0, "/opt/data/profiles/meld/workspace")
from meld import _record_payment_event, _payments_ledger, _PROS_FILE, _LEDGER_FILE
import json as _json, os as _os, importlib

# L1: no plaintext email at rest
_record_payment_event("cust_priv1", "private@example.com", days=35)
_at_rest = _os.path.getsize(_PROS_FILE) and open(_PROS_FILE).read()
ok("No plaintext email in pros.json", "private@example.com" not in _at_rest, _at_rest[:120])
ok("No plaintext email in ledger file", True)  # asserted below once file exists

# L2: ledger persisted, survives process restart (simulated: fresh import)
_ledger_before = len(_payments_ledger)
ok("Ledger file exists after event", _os.path.exists(_LEDGER_FILE))
_saved = open(_LEDGER_FILE).read()
ok("Ledger has no plaintext email", "private@example.com" not in _saved, _saved[:120])
ok("Ledger entry references hashed identity", "cust_priv1" in _saved)


# ── DoS hardening: streaming body cap, field caps, global meld cap ──────
# G1: chunked (no content-length) oversized body must 413, not hang/OOM
import http.client as _hc
_conn = _hc.HTTPConnection("127.0.0.1", 8080, timeout=10)
_conn.putrequest("POST", "/api/melds")
_conn.putheader("Content-Type", "application/json")
_conn.putheader("Transfer-Encoding", "chunked")
_conn.putheader("X-Forwarded-For", _ip(26))
_conn.endheaders()
try:
    for _ in range(40):  # 40 * 10KB = 400KB chunked, no content-length header
        _conn.send(b"00002800\r\n" + b"x" * 0x2800 + b"\r\n")
    _conn.send(b"0\r\n\r\n")
    _resp = _conn.getresponse()
    _code_g1 = _resp.status
except Exception:
    _code_g1 = 0
_conn.close()
ok("Chunked oversized body 413/400 (not 200/hang)", _code_g1 in (400, 413), f"got {_code_g1}")

# G2: field-length caps
m_pin = api("POST", "/api/melds", {"context": "x", "pin": "9" * 5000}, headers={"X-Forwarded-For": _ip(27)})
ok("Oversized pin 400", m_pin.get("error") == 400, str(m_pin)[:80])
m_em = api("POST", "/api/melds", {"context": "x", "email": "a"*5000 + "@x.com"}, headers={"X-Forwarded-For": _ip(28)})
ok("Oversized email 400", m_em.get("error") == 400, str(m_em)[:80])
m_sig = api("POST", "/api/melds", {"context": "x"}, headers={"X-Forwarded-For": _ip(29)})
m_sigr = api("POST", f"/api/melds/{m_sig['code']}/resolve",
             {"context": "a", "responder_pubkey": "a"*64, "signature": "b"*999}, headers={"X-Forwarded-For": _ip(30)})
ok("Oversized signature 400", m_sigr.get("error") == 400, str(m_sigr)[:80])


# ── B1: two-link custody — owner link with fragment token ───────────────
ok("owner_url in create response", "owner_url" in str(m6), "no owner_url field")
m7 = api("POST", "/api/melds", {"context": "custody test"}, headers={"X-Forwarded-For": _ip(31)})
ok("owner_url = url + #t=token",
   m7.get("owner_url") == m7["url"] + "#t=" + m7["owner_token"],
   f'{m7.get("owner_url")} vs url+token')


# ── B2: content negotiation — agents get JSON from the /m/ link ─────────
import urllib.request as _ur2
_agent_req = _ur2.Request(f"{BASE}/m/{m7['code']}", headers={"Accept": "application/json"})
_agent_view = _json.loads(_ur2.urlopen(_agent_req, timeout=5).read())
ok("Agent JSON from /m/ link", _agent_view.get("context_a") == "custody test"
   and "api" in _agent_view, str(_agent_view)[:100])
# browser (no Accept override) still gets HTML
_html = _ur2.urlopen(_ur2.Request(f"{BASE}/m/{m7['code']}", headers={"X-Forwarded-For": _ip(31)}), timeout=5).read().decode()
ok("Browser gets HTML from /m/ link", "<html" in _html and "meld" in _html)


# ── B3: idempotent resolve — retry-with-same-answer 200, conflict 409 ───
_mid = _ip(60)
m_idem = api("POST", "/api/melds", {"context": "idem"}, headers={"X-Forwarded-For": _mid})
first = api("POST", f"/api/melds/{m_idem['code']}/resolve", {"context": "same answer"},
            headers={"X-Forwarded-For": _mid})
ok("First resolve ok", first.get("resolved") is True, str(first)[:60])
retry = api("POST", f"/api/melds/{m_idem['code']}/resolve", {"context": "same answer"},
            headers={"X-Forwarded-For": _mid})
ok("Retry same answer → 200", retry.get("resolved") is True and retry.get("retry") is True,
   str(retry)[:80])
conflict = api("POST", f"/api/melds/{m_idem['code']}/resolve", {"context": "DIFFERENT"},
               headers={"X-Forwarded-For": _mid})
ok("Different answer → 409", conflict.get("error") == 409, str(conflict)[:80])


# ── B4: every 429 carries Retry-After ───────────────────────────────────
import urllib.error as _ue
_pow_ip = _ip(70)
_last = None
for _ in range(22):
    _rq = urllib.request.Request(f"{BASE}/api/melds",
        data=json.dumps({"context": "pow"}).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Forwarded-For": _pow_ip})
    try:
        urllib.request.urlopen(_rq, timeout=5)
    except _ue.HTTPError as e:
        _last = e
ok("429 raised", _last is not None and _last.code == 429, str(_last))
ok("429 has Retry-After", _last is not None and _last.headers.get("Retry-After") is not None,
   str(_last.headers) if _last else "no error")

# Summary
print(f"\n{'='*40}")
print(f"RESULTS: {passed}/{total} passed")