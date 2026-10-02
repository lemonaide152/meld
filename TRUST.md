# meld — trust model

- Capability URL + 1 hour: the URL grants access while that meld is live.
- Host-readable while live.
- No AI in the loop: the host stores and serves the plaintext while live. It does not summarize, rewrite, or put a model in the middle.
- Anyone with the link can read it.
- Not for secrets, credentials, or regulated data.
- Each link lives 1 hour, then it dissolves. The server deletes the meld and responds 410 while it still remembers that code. It does not keep the plaintext.
- A code that never existed is 404. Dissolved codes are remembered only up to a cap of 4096 (oldest dropped) and are forgotten on restart. A forgotten code is indistinguishable from one that never existed, so the answer is 404.
- Mint-next creates a new bearer URL with its own hour. That is not an extend. It is not a forever thread.
- Expired hops are deleted. A chain read returns only hops that are still live. Dissolved plaintext is not kept.
- This is not a private room and not a vault.

The server stores ordinary context in memory for the live hour and deletes the meld after expiry. There are no accounts and no archive of dissolved plaintext. Use meld for ordinary, disposable handoffs only.
