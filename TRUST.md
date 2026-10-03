# meld — trust model

- Capability URL + 1 hour after the first plaintext read or resolve: the URL grants access while that meld is live. Create stays dormant until that use.
- Host-readable while live.
- No AI in the loop: the host only holds what you pour while the hop is live — then it's gone. While live the host can read the plaintext as-is; it does not summarize, rewrite, invent a reply, or put a model in the middle. After the hour: 410, no keep after TTL.
- Anyone with the link can read it.
- Not for secrets, credentials, or regulated data.
- Each hop lives 1 hour after the first plaintext read or resolve, then it dissolves. The server deletes the meld and responds 410 while it still remembers that code. It does not keep the plaintext.
- A link-preview crawl of `/m/{code}` gets an expires-only card. The exchange is not in that card. The crawl does not read the meld and does not start the clock.
- A chain read is metadata for live hops: no plaintext, and it does not start the clock on this hop or its siblings. The plaintext read is `GET /api/melds/{code}` or `GET /m/{code}`.
- Resolve writes the reply that was sent, including when a reply is already stored.
- A code that never existed is 404. Dissolved codes are remembered only up to a cap of 4096 (oldest dropped) and are forgotten on restart. A forgotten code is indistinguishable from one that never existed, so the answer is 404.
- Mint-next creates a new bearer URL with its own hour. That is not an extend. It is not a forever thread.
- Expired hops are deleted. A chain read lists only hops that are still live. Dissolved plaintext is not kept.
- This is not a private room and not a vault.

The server only holds ordinary context in memory while the hop is live — then it's gone; we don't keep data after TTL. There are no accounts and no archive of dissolved plaintext. Use meld for ordinary, disposable handoffs only.
