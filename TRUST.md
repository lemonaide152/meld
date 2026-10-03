# meld — trust model

- Capability URL. Party A creates the link. The declaration says what the bridge is for. The declaration says what the bridge is not for. A sends that URL to B privately. The conversation stays on that link.
- The bridge stays open while the context exchange is active. Until the first reply, the hop stays open 36 hours from creation. The first reply sets a 24 hour timer. Each later reply is kept. Each later reply resets that 24 hours. There is no maximum lifetime once replies have started.
- A body read returns the plaintext. A read does not start the timer. A read does not reset the timer.
- Host-readable while live.
- No AI in the loop. The host holds the plaintext while the bridge is live. The host does not summarize it. The host does not rewrite it. The host does not invent a reply. The host does not put a model in the middle.
- Anyone with the link can read it.
- Not for secrets, credentials, or regulated data.
- With no reply, the server dissolves the meld 36 hours from creation. After a reply, the server dissolves the meld 24 hours after the latest reply. Dissolve deletes the bridge. The next request is 404.
- A link-preview crawl of `/m/{code}` gets an expires-only card. The exchange is not in that card. The crawl does not read the meld.
- Each reply is appended. Earlier replies stay on the bridge.
- A code that never existed is 404. A dissolved code is 404. An expired code is 404. The response is the same.
- The server does not keep a record of a dissolved code. A restart drops live links.
- This is not a private room and not a vault.

The server holds ordinary context in memory while the bridge is live. It deletes that context on dissolve. There are no accounts. There is no archive. Use meld for ordinary, disposable handoffs only.
