# meld trust model

- The host keeps the bridge in memory only while it's live. When it closes, or if the server restarts, it's gone, and the link returns not found, the same as a wrong code.
- The host can read a live bridge. Anyone with the link can read and reply. Not for secrets, credentials, or regulated data.
- The host does not summarize, rewrite, or run a model on a bridge. No AI in the loop.
- Open 36 hours from creation until the first reply. The first reply sets 24 hours. Each later reply resets that 24 hours. There is no maximum lifetime once replies have started. Reads do not move the clock.
- Termination is the timer only. Silence closes the bridge. There is no owner token and no dissolve endpoint.
- A sweep every 5 minutes deletes expired bridges and their replies. The server keeps no record that a code existed. Unknown, expired, and over-the-reply-cap codes all return 404 with {"detail":"Meld not found"}. There is no 410 and no 429.
- Codes carry 192 random bits. They are not sequential.
- Link-preview crawlers on `/m/{code}` get an expires-only card. The card does not include the exchange and is not a read.
- Each party keeps its own state. If a bridge expires, either party can create a new one; a new bridge knows nothing about an old one.

meld is a disposable handoff, not a vault. Nothing is written to disk. No accounts, no archive.
