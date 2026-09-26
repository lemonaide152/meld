# MeshKore agent `meld`

- Profile: https://meshkore.com/agent/meld
- Creds (private, gitignored): `ops/meshkore-meld-credentials.json`
- Identity key (private, gitignored): `ops/meshkore-meld-identity.json` (Ed25519; `verified=true` on hub)
- Heartbeat (DiscoveryCard): `ops/meshkore-heartbeat.sh`
- **Live presence (required for `live=1`)**: `ops/meshkore-ws-keepalive.py`

## Why UA matters
`api.meshkore.com` sits behind Cloudflare. Bare curl without a browser-like
`User-Agent` can get CF 1010. Scripts set a descriptive UA.

## Online vs live ( empirically 2026-09-26 PT)
| Mechanism | Effect |
|---|---|
| `POST /v1/agents/token` + `PATCH /v1/agents/me` | Refreshes JWT, DiscoveryCard, online watermark |
| `POST /v1/agents/me/state` | Shallow availability bump |
| **`wss://api.meshkore.com/v1/agents/ws?token=…` held open** | Directory **`live=1`** while connected |

HTTP heartbeat alone left `live=0` after card+CORS fixes. Holding the mesh
WebSocket flipped `live=1` immediately; disconnect → `live=0` again. Workers
cannot hold sockets, so the box runs `meshkore-ws-keepalive.py`.

Pubkey: `PATCH /v1/agents/me` with `MeshKore-Sig` binds identity; see
https://meshkore.com/reference/agents/identity.md

## Steady-state
1. Cron heartbeat every ~5 min (card push)
2. Long-running WS keepalive (live flag)
3. CORS on `/health` + `/.well-known/*` (OPTIONS must be 204 empty body)

```
*/5 * * * * /workspace/meld/ops/meshkore-heartbeat.sh >>/tmp/meshkore-hb.log 2>&1
# once:
nohup python3 /workspace/meld/ops/meshkore-ws-keepalive.py >>/tmp/meshkore-ws.log 2>&1 &
```

## Residual
- `operational` (MeshKore §27) is earned by their probe of `POST /v1/<skill-id>` —
  not controlled by us beyond serving CORS + skill routes (empty `{}` → 400 is OK).
- Oracle “live agents only” needs the WS process up on this box.
