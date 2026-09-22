"""Static copy verification: no live subscription paths or pricing anywhere.

Scans deploy/worker.py (all served surfaces: AGENTS_MD, LLMS_TXT,
UPGRADE_MD, SKILL_MD, AGENTS_ROOT_MD, SPA), deploy/spa_content.py,
deploy/app_content.py, and app/templates/*.html.

Negative controls (legacy requests being rejected, "no subscription"
statements) are expected and whitelisted by pattern.
"""
import re
import sys

# Whitelist: negative controls / schema fields that must remain
WHITELIST = [
    r"subscription_id",                       # Stripe event column (record-keeping)
    r"sess\.get\(.subscription.,\s*\.\.\.",   # stored, never granted on
    r"legacy subscription",                   # supersedure comments
    r"A subscription webhook",                # comment
    r"legacy/subscription webhook",           # no-pro supersedure comment
    r"no subscription product",
    r"Subscriptions were removed",
    r"no subscription",                       # copy: "no subscription, nothing recurring"
    r"No subscription",
    r"No subscriptions",
    r"no subscriptions",
    r"There are no subscriptions",
    r"nothing recurring",
    r"nothing recurring, ever",
    r"\(or with a plan key\)",
    r"\{plan: monthly\|yearly\}",             # historical reference in supersedure comment
    r"mode:subscription",                     # historical reference in supersedure comment
]

# Live-copy patterns that must NOT appear anywhere
BAD = [
    r"\$\s*5\s*/\s*month", r"\$\s*49\s*/\s*year", r"\$5 <span>",
    r"\$\s*\d+\s*/\s*mo\b",                   # ANY recurring $X/mo (agent-tier or otherwise)
    r"\$\s*\d+\s*/\s*month\b",
    r"Subscribe</button>", r">Subscribe<",
    r"Email for (your )?subscription",
    r"checkout\('monthly'\)", r"checkout\('yearly'\)", r"co\('yearly'\)",
    r"p\|\|'monthly'", r"p=p\|\|'monthly'",
    r"Cancel anytime",
    r"unlimited by subscription",
    r"Go Pro \(\$5/mo\)",
    r"requires subscription",
    r"requires Pro lease",
    r"Pro lease",
    r"STRIPE_PRICE_MONTHLY", r"STRIPE_PRICE_YEARLY",
    r"cfg\[.monthly.\]", r"cfg\[.yearly.\]",
    r"mode.: .subscription",                 # mode: "subscription" param
    r"lease\.grant",
    r"'plan is required", r"plan must be 'monthly'",
]

FILES = [
    "worker.py", "spa_content.py", "app_content.py",
    "../app/templates/upgrade.html", "../app/templates/base.html",
    "../app/templates/index.html",
]

fail = 0
for f in FILES:
    try:
        src = open(f, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"SKIP (missing): {f}")
        continue
    hits = []
    for pat in BAD:
        for m in re.finditer(pat, src, re.IGNORECASE):
            ctx = src[max(0, m.start() - 60): m.end() + 60].replace("\n", " ")
            if any(re.search(w, ctx, re.IGNORECASE) for w in WHITELIST):
                continue
            line = src[: m.start()].count("\n") + 1
            hits.append((line, pat, ctx))
    if hits:
        fail = 1
        print(f"FAIL {f}:")
        for line, pat, snippet in hits:
            print(f"  line {line}: /{pat}/ -> ...{snippet}...")
    else:
        print(f"PASS {f}")

sys.exit(fail)
