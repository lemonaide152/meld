# meld trust model (self-hosted)

This describes what `server.py` in this repository does. If you change the code or the host around it, re-check each line.

## What it does

- A bridge is a link with a random 12-character code. Party A creates the link with one note and sends it to party B privately. Replies are appended to the same bridge.
- Host-readable while live.
- Creating a bridge opens it for 36 hours. The first reply replaces that with a 24-hour idle timer. Each later reply resets the 24 hours. Reads never move the clock. There is no maximum lifetime while replies keep coming.
- When the window ends, the link returns 404. A code that never existed returns the same 404.
- The server stores and returns text as written. No model runs on it. Nothing is summarized or rewritten.
- Known link-preview bots that fetch `/m/{code}` get a generic card. The exchange is not in that card.

## What it does not do

- It does not hide bridges from the server operator. Bridges are plaintext in memory while live.
- It does not check who is asking. Anyone with the link can read the bridge and add a reply.
- No encryption. TLS comes from Caddy or your own proxy.
- It does not notify anyone or record reads.
- It does not rate-limit or block abuse.
- It does not make anyone forget. Each party may still remember or copy what they read.

## Storage

- Bridges live in a Python dictionary in process memory. There is no database.
- When the window ends, the link returns 404. The expired entry stays in process memory until someone requests that code or creates any bridge; then the server clears it. A restart clears every bridge and every open link returns 404.
- The server logs codes on create, reply, and when it clears an expired entry. uvicorn's access log records request paths, which include codes. Note and reply text is not logged. Treat these logs as sensitive. A code in them opens a live bridge until the window ends.
- Swap, memory snapshots, VM backups, and log retention are controlled by whoever runs the host.

This is not a private room and not a vault. Use meld for ordinary context you would be comfortable pasting into a shared chat. Not for secrets, credentials, or regulated data.
