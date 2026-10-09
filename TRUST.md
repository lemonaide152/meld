# meld trust model

- The capability URL is the authorization. There are no accounts, logins, owner tokens, or dissolve endpoint.
- The host can read a live bridge. Anyone with the link can read and reply. Not for secrets, credentials, or regulated data.
- The host does not summarize, rewrite, or run a model on a bridge. No AI in the loop.
- Open 36 hours from creation until the first reply. The first reply sets 24 hours. Each later reply resets that 24 hours. There is no maximum lifetime once replies have started. Reads do not move the clock.
- Termination is the timer only. Silence closes the bridge.
- A sweep every 5 minutes deletes expired bridges and their replies. The server keeps no record that a code existed. Unknown, expired, and over-the-reply-cap codes all return 404 with {"detail":"Meld not found"}. There is no 410 and no 429.
- Codes carry 192 random bits. They are not sequential.
- Link-preview crawlers on `/m/{code}` get an expires-only card. The card does not include the exchange and is not a read.
- Self-host keeps bridges in memory only. A restart drops every live link.
- The hosted pilot stores a bridge in Cloudflare D1 only while it is live; D1 Time Travel keeps restorable past database states for up to 30 days (7 on the Workers Free plan), so a deleted bridge can remain in those backups until that window passes.
- Each party keeps its own state. If a bridge expires, either party can create a new one; a new bridge knows nothing about an old one.

Use meld for ordinary, disposable handoffs only.
