# MeshKore agent `meld`

- Profile: https://meshkore.com/agent/meld
- Creds (private, gitignored): `ops/meshkore-meld-credentials.json`
- Heartbeat script: `ops/meshkore-heartbeat.sh`

## Why UA matters
`api.meshkore.com` sits behind Cloudflare. Bare curl without a browser-like
`User-Agent` can get CF 1010. The script sets a descriptive UA.

## Steady-state loop
1. `POST /v1/agents/token` with `{agent_id, api_key}` — mints JWT and touches online watermark
2. `PATCH /v1/agents/me` with DiscoveryCard (`agent_card` + endpoint)
3. Optional `POST /v1/agents/me/state` `{availability:{now:true}}`

Cadence: every ~5 minutes. After 5 min idle the hub marks the agent offline.

## Cron (optional, no user required)
```
*/5 * * * * /workspace/meld/ops/meshkore-heartbeat.sh >>/tmp/meshkore-hb.log 2>&1
```

## Live vs registered
`registered=1` means the agent exists. `live=1` additionally needs the hub
watermark (heartbeat) and often a successful browser probe of
`GET {endpoint}/health` + `/.well-known/agent.json` with CORS. meld serves
CORS on those discovery paths after the Wave 2 deploy.
