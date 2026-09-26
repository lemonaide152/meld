#!/usr/bin/env python3
"""Keep MeshKore agent 'meld' live via wss presence.

Empirically (2026-09-26): HTTP token heartbeat updates the DiscoveryCard and
watermark, but directory `live=1` only sticks while the agent holds
`wss://api.meshkore.com/v1/agents/ws?token=...`. Serverless Workers can't hold
sockets, so this box process does.

Usage:
  python3 ops/meshkore-ws-keepalive.py
  # or: nohup ... >>/tmp/meshkore-ws.log 2>&1 &
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.stderr.write("websockets required: pip install websockets\n")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
CREDS = Path(os.environ.get("MESHKORE_CREDS", ROOT / "ops/meshkore-meld-credentials.json"))
UA = os.environ.get(
    "MESHKORE_UA",
    "Mozilla/5.0 (compatible; meld-ws-keepalive/1.0; +https://meld.mergeinc.workers.dev)",
)
HUB = os.environ.get("MESHKORE_HUB", "https://api.meshkore.com")


def mint_token() -> tuple[str, str]:
    creds = json.loads(CREDS.read_text())
    body = json.dumps({"agent_id": creds["agent_id"], "api_key": creds["api_key"]}).encode()
    req = urllib.request.Request(
        f"{HUB}/v1/agents/token",
        data=body,
        headers={"User-Agent": UA, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return creds["agent_id"], data["token"]


async def run() -> None:
    backoff = 1
    while True:
        try:
            agent_id, token = mint_token()
            uri = f"{HUB.replace('https://', 'wss://')}/v1/agents/ws?token={token}"
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} connecting agent_id={agent_id}", flush=True)
            async with websockets.connect(
                uri,
                additional_headers={"User-Agent": UA},
                open_timeout=30,
                ping_interval=20,
                ping_timeout=20,
                max_queue=32,
            ) as ws:
                backoff = 1
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} ws open (live should be 1)", flush=True)
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        # presence chatter from peers — ignore payload, stay connected
                        if os.environ.get("MESHKORE_WS_VERBOSE"):
                            print("recv", msg[:200], flush=True)
                    except asyncio.TimeoutError:
                        # idle is fine; websockets library pings keep the socket alive
                        continue
        except Exception as e:
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} ws err: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    if not CREDS.is_file():
        sys.stderr.write(f"missing creds: {CREDS}\n")
        sys.exit(1)
    asyncio.run(run())
