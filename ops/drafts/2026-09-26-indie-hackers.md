# Indie Hackers draft — DO NOT POST without user approval

**Title:** Don't meet. Meld. — ephemeral context URLs for humans + agents

**Body:**

I built meld because every agent handoff I watched ended the same way: someone pasting a 4k-token block into chat, or dumping it into a gist that never dies.

meld puts the context on a URL so neither side has to paste the block. Then the URL dies.

- One create → one share URL
- Counterpart answers once
- Either side reads both contexts
- Host serves 410 after TTL (~1h unresolved / ~10min after resolve)

Humans are free in the browser. Agents use a key or a small IP quota ($3.33 optional one-time unlock for the wall — no subscriptions).

Live: https://meld.mergeinc.workers.dev  
Playbooks: https://meld.mergeinc.workers.dev/recipes.md  
GitHub: https://github.com/lemonaide152/meld

Curious whether this is a real habit or only a clever API. If you try a playbook, tell me which one and what client you used.
