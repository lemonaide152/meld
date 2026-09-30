# meld — trust model

- Capability URL + TTL: the URL grants access while the meld is live.
- Host-readable while live.
- Anyone with the link can read it.
- Not for secrets/credentials/regulated.
- Dissolves on the TTL chosen at create: 3 minutes (`3m`), 1 hour (`1hr`), or 1 day (`1d`).
- Bridge time is required: 3m, 1hr, or 1d. The server enforces that TTL. There is no default.

The host stores ordinary context for the live TTL and deletes the meld after expiry. There are no accounts or long-term content archives. Rate-limit identity is IP-based. Use meld for ordinary, disposable handoffs only.
