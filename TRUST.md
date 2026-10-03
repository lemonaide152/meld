# meld — trust model

- Capability URL. The conversation stays on that link. Create stays dormant until the first reply.
- The first reply starts a 1 hour silence timer. Each later reply is kept and resets that timer. There is no maximum lifetime. The bridge stays open while the conversation continues, and closes only after one hour with no new reply.
- A body read returns the plaintext and does not start or reset the timer.
- Host-readable while live.
- No AI in the loop: the host only holds what you pour while the bridge is live — then it's gone. While live the host can read the plaintext as-is; it does not summarize, rewrite, invent a reply, or put a model in the middle. After a quiet hour: 410, no keep after TTL.
- Anyone with the link can read it.
- Not for secrets, credentials, or regulated data.
- When the quiet hour ends, the server deletes the meld and responds 410 while it still remembers that code. It does not keep the plaintext.
- A link-preview crawl of `/m/{code}` gets an expires-only card. The exchange is not in that card. The crawl does not read the meld.
- Each reply is appended. Earlier replies stay on the bridge.
- A code that never existed is 404. Dissolved codes are remembered only up to a cap of 4096 (oldest dropped) and are forgotten on restart. A forgotten code is indistinguishable from one that never existed, so the answer is 404.
- Expired bridges are deleted. Dissolved plaintext is not kept.
- This is not a private room and not a vault.

The server only holds ordinary context in memory while the bridge is live — then it's gone; we don't keep data after a quiet hour. There are no accounts and no archive of dissolved plaintext. Use meld for ordinary, disposable handoffs only.
