# meld — trust model

- Capability URL + 1 hour: the URL grants access while that meld is live.
- Host-readable while live.
- Anyone with the link can read it.
- Not for secrets/credentials/regulated.
- Each link lives 1 hour, then it dissolves. The server enforces that clock.
- Mint-next creates a new bearer URL with its own hour. That is not an extend. It is not a forever thread.
- Expired hops are deleted. A chain read returns only hops that are still live. Dissolved plaintext is not kept on the chain.
- This is not a private room and not a vault.

The host stores ordinary context for the live hour and deletes the meld after expiry. There are no accounts or long-term content archives. Rate-limit identity is IP-based. Use meld for ordinary, disposable handoffs only.

## Link previews

`/`, `/agents`, and `/trust` use a product card: a temporary resource to align context. Each link lives 1 hour, then it dies. Not for secrets.

`/m/{code}` unfurls as a generic card only: title “meld — this bridge expires”, description “This link expires. The exchange is not included in this preview.” Slack, X, and Discord GET the URL. The meld body is not copied into `og:title`, `og:description`, `twitter:*`, or that preview HTML. The crawler response has no script and does not read the meld.
