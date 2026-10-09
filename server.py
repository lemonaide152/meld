"""meld self-host server. Memory-only: a restart drops every live link.

    python server.py            # listens on $PORT (default 8080)

Set MELD_PUBLIC_URL to the public origin when behind a proxy (Caddy).
"""
from __future__ import annotations

import os

from meld_app import build_app
from meld_store import MemoryStore

app = build_app(
    MemoryStore(),
    public_url=os.getenv("MELD_PUBLIC_URL", "").strip() or None,
    background_sweep=True,
)


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("MELD_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        proxy_headers=True,
        # Trust X-Forwarded-* only from these. Compose sets "*" because Caddy
        # is the only client that can reach this port.
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        access_log=os.getenv("MELD_ACCESS_LOG", "0") == "1",
    )


if __name__ == "__main__":
    main()
