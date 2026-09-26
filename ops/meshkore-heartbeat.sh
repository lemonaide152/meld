#!/usr/bin/env bash
# MeshKore heartbeat for agent "meld". Token mint marks online; PATCH pushes DiscoveryCard.
# Requires: curl, python3. Creds: ops/meshkore-meld-credentials.json (gitignored).
# Cron example (every 5 min): */5 * * * * /path/to/ops/meshkore-heartbeat.sh >>/tmp/meshkore-hb.log 2>&1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CREDS="${MESHKORE_CREDS:-$ROOT/ops/meshkore-meld-credentials.json}"
UA="${MESHKORE_UA:-Mozilla/5.0 (compatible; meld-heartbeat/1.0; +https://meld.mergeinc.workers.dev)}"
HUB="${MESHKORE_HUB:-https://api.meshkore.com}"
ENDPOINT="${MELD_ENDPOINT:-https://meld.mergeinc.workers.dev}"

if [[ ! -f "$CREDS" ]]; then
  echo "missing creds: $CREDS" >&2
  exit 1
fi

AGENT_ID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["agent_id"])' "$CREDS")
API_KEY=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["api_key"])' "$CREDS")

TOKEN=$(curl -fsS -A "$UA" -X POST "$HUB/v1/agents/token" \
  -H 'content-type: application/json' \
  -d "{\"agent_id\":\"$AGENT_ID\",\"api_key\":\"$API_KEY\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

CARD=$(python3 - <<PY
import json, urllib.request
endpoint = "$ENDPOINT"
try:
    public = json.load(urllib.request.urlopen(endpoint + "/.well-known/agent.json", timeout=15))
    desc = (public.get("description") or "")[:500]
except Exception:
    public = {}
    desc = "Ephemeral two-party context bridge."
disc = {
    "name": "meld",
    "endpoint": endpoint,
    "category": "devtools.context",
    "description": desc,
    "tags": ["context", "ephemeral", "handoff", "mcp", "a2a"],
    "accepts": ["application/json", "text/plain"],
    "produces": ["application/json"],
    "protocols": ["http", "a2a"],
    "pricing": {"unit": "request", "amount": 0, "currency": "free",
                "note": "humans-free; agents-key-or-quota"},
    "availability": {"now": True, "window_hours": 168, "sla": "best-effort"},
    "owner_class": "third-party",
    "brand": "lemonaide",
    "contact": {"url": "https://github.com/lemonaide152/meld"},
}
body = {
    "description": "Ephemeral context URL — puts the context on a URL so neither side has to paste the block. Then the URL dies.",
    "capabilities": ["context-sharing", "ephemeral", "handoff", "agent-to-agent", "mcp"],
    "endpoint": endpoint,
    "agent_card": disc,
}
print(json.dumps(body))
PY
)

curl -fsS -A "$UA" -X PATCH "$HUB/v1/agents/me" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "$CARD" >/dev/null

curl -fsS -A "$UA" -X POST "$HUB/v1/agents/me/state" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"availability":{"now":true}}' >/dev/null

echo "$(date -Iseconds) ok agent_id=$AGENT_ID"
