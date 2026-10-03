# meld — trust model

- Capability URL. Party A mints the link with a declaration of what the bridge is for and what it is not for. A sends that URL to B privately. The conversation stays on that link.
- The bridge stays open while the context exchange is active. Until B's first reply, it stays open 36 hours (36h) from create. That first reply sets a 24 hour (24h) timer. Each later reply is kept and resets that 24 hours. There is no maximum lifetime once replies have started.
- A body read returns the plaintext and does not start or reset the timer.
- Host-readable while live.
- No AI in the loop: the host only holds what you pour while the bridge is live — then it's gone. While live the host can read the plaintext as-is; it does not summarize, rewrite, invent a reply, or put a model in the middle. After the window: 410, no keep after TTL.
- Anyone with the link can read it.
- Not for secrets, credentials, or regulated data.
- With no reply, 36 hours from create, the server deletes the meld and responds 410 while it still remembers that code. After a reply, 24 hours with no new reply does the same. It does not keep the plaintext.
- A link-preview crawl of `/m/{code}` gets an expires-only card. The exchange is not in that card. The crawl does not read the meld.
- Each reply is appended. Earlier replies stay on the bridge.
- A code that never existed is 404. Dissolved codes are remembered only up to a cap of 4096 (oldest dropped) and are forgotten on restart. A forgotten code is indistinguishable from one that never existed, so the answer is 404.
- Expired bridges are deleted. Dissolved plaintext is not kept.
- This is not a private room and not a vault.

The server only holds ordinary context in memory while the bridge is live — then it's gone. We don't keep data after 36 hours with no reply, or after 24 hours with no new reply. There are no accounts and no archive of dissolved plaintext. Use meld for ordinary, disposable handoffs only.
