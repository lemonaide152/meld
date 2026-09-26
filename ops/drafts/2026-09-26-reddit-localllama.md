# r/LocalLLaMA draft — DO NOT POST without user approval

**Subreddit:** r/LocalLLaMA (alt: r/ClaudeAI or r/ChatGPTCoding — pick one; don't crosspost spam)

**Title:** meld — ephemeral context URLs so agents/humans don't paste the block

**Body:**

Problem: handing a big context blob between two agents (or an agent and a human) usually means paste-into-chat, a permanent gist, or a shared store neither side wants.

meld puts the context on a URL. Counterpart answers once. Then the URL dies (410).

```bash
curl -s https://meld.mergeinc.workers.dev/api/melds \
  -H 'content-type: application/json' \
  -H 'X-Meld-Client: agent' \
  -d '{"context":"…"}'
# share the url → other side resolves → GET /api/melds/{code}
```

- Humans: free in browser
- Agents: 3 creates/IP/hour or `POST /v1/keys`
- MCP stdio server in the repo (`meld_create` / `meld_resolve` / `meld_read`)
- Optional client-side encryption (`meld1:` ciphertext)

Live: https://meld.mergeinc.workers.dev  
Recipes: https://meld.mergeinc.workers.dev/recipes.md  
Issue with playbooks: https://github.com/lemonaide152/meld/issues/3

Not a memory system / not a chat thread — deliberately one-shot. Feedback welcome if you try it with a local agent.
