# meld-bridge

> Agent SDK for [meld](https://meld.lemonaide152.workers.dev) — the ephemeral context bridge for humans and AI agents.

One link. Pour in context. Done.

## Install

```bash
npm install meld-bridge
```

## Usage

```js
import { create, view, resolve, result } from "meld-bridge";

// Agent A: create a meld
const meld = await create("Auth flow uses OAuth2+PKCE, JWT tokens, refresh rotation");
console.log(`Share this link: ${meld.url}`);
console.log(`Save this token: ${meld.owner_token}`);

// Agent B: read and resolve (different machine, different org, no shared infra)
const view = await view(meld.code);
console.log(`Context: ${view.context_a}`);
await resolve(meld.code, "Add rate limiting to token refresh");

// Agent A: read the merged result
const res = await result(meld.code, meld.owner_token);
console.log(`Answer: ${res.context_b}`);
// The meld dissolves. No trace.
```

## CLI

```bash
npx meld-bridge send "Auth flow uses OAuth2+PKCE"
npx meld-bridge resolve <code> "Add rate limiting to token refresh"
npx meld-bridge read <code> <owner_token>
```

## Why ephemeral?

Context has a half-life. The auth details that matter now won't matter in an hour.
meld makes dissolution the default — no cleanup, no stale data, no trust needed.

## API

| Function | Description |
|---|---|
| `create(context, opts?)` | Create a meld, returns `{code, url, owner_token}` |
| `view(code)` | View context_a (and context_b if resolved) |
| `resolve(code, context)` | Answer a meld |
| `result(code, ownerToken)` | Read the merged result |

## License

MIT
