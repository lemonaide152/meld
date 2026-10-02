# meld — trust model

- Capability URL + 1 hour after first open: the URL grants access while that meld is live.
- Host-readable while live.
- No AI in the loop: the host only holds what you pour while the hop is live — then it's gone. While live the host can read the plaintext as-is; it does not summarize, rewrite, or put a model in the middle. After the hour: 410, no keep after TTL.
- Anyone with the link can read it.
- Not for secrets, credentials, or regulated data.
- Each hop lives 1 hour after the first open, then it dissolves. The server deletes the meld and responds 410 while it still remembers that code. It does not keep the plaintext.
- A code that never existed is 404. Dissolved codes are remembered only up to a cap of 4096 (oldest dropped) and are forgotten on restart. A forgotten code is indistinguishable from one that never existed, so the answer is 404.
- Mint-next creates a new bearer URL with its own hour. That is not an extend. It is not a forever thread.
- Expired hops are deleted. A chain read returns only hops that are still live. Dissolved plaintext is not kept.
- This is not a private room and not a vault.

The server only holds ordinary context in memory while the hop is live — then it's gone; we don't keep data after TTL. There are no accounts and no archive of dissolved plaintext. Use meld for ordinary, disposable handoffs only.
