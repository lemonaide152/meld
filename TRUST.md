# meld — trust model

- Capability URL + 1 hour: the URL grants access while that meld is live.
- Host-readable while live.
- Anyone with the link can read it.
- Not for secrets, credentials, or regulated data.
- Each link lives 1 hour, then it dissolves. The server enforces that clock and responds 410 when the meld is gone.
- Mint-next creates a new bearer URL with its own hour. That is not an extend. It is not a forever thread.
- Expired hops are deleted. A chain read returns only hops that are still live. Dissolved plaintext is not kept.
- This is not a private room and not a vault.

The server stores ordinary context in memory for the live hour and deletes the meld after expiry. There are no accounts and no archive of dissolved links. Use meld for ordinary, disposable handoffs only.
