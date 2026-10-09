"""Web UI: one note, create link, the 36/24 rule, not for secrets. No model, no account."""
from __future__ import annotations

from html import escape

from meld_spec import IDLE_HOURS, MAX_CHARS, OPEN_HOURS

RULE_SHORT = (
    f"Open {OPEN_HOURS} hours for a first reply, then {IDLE_HOURS} hours after each reply. Silence closes it."
)
SECRETS = "Not for secrets. The host can read a live bridge, and anyone with the link can read and reply."

CSS = """
:root{--bg:#05050a;--panel:#101018;--ink:#f7f7fb;--muted:#b7b7c9;--line:#2e2a3d;--accent:#8b5cf6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
main{max-width:720px;margin:0 auto;padding:24px 16px}h1{font-size:28px;margin:0 0 8px}
.muted{color:var(--muted)}.warn{color:#fbbf24}
textarea{width:100%;min-height:160px;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:12px;font:inherit}
button{margin-top:12px;background:var(--accent);color:#fff;border:0;border-radius:8px;padding:10px 18px;font:inherit;cursor:pointer}
.box{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0;white-space:pre-wrap;word-break:break-word}
.meta{font-size:13px;color:var(--muted)}input{width:100%;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:10px;font:inherit}
#err{color:#f87171}
"""


def _page(title: str, body: str, nonce: str, *, head_extra: str = "", script: str = "") -> str:
    js = f'<script nonce="{nonce}">{script}</script>' if script else ""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{escape(title)}</title>{head_extra}<style>{CSS}</style></head>"
        f"<body><main>{body}</main>{js}</body></html>"
    )


HERO = (
    f"meld is one link for passing working context between you and an agent, or between two agents. "
    f"It closes after {OPEN_HOURS} hours without a reply, then {IDLE_HOURS} hours after each reply. "
    "The host can read it while it's live and runs no AI on it. Not for secrets."
)
TRUST_BULLETS = (
    "No AI in the loop. The host can read the text while the bridge is live. It doesn't summarize or rewrite it.",
    "Anyone with the link can read and reply while it's live. Send it privately.",
    "Not for secrets, credentials, or regulated data.",
    f"Open {OPEN_HOURS} hours for a first reply, then {IDLE_HOURS} hours after each reply. Silence closes it.",
    "When it closes, it's deleted. The link then returns not found, the same as a wrong code.",
)
MEMORY_LAST_BULLET = "When it closes, or if the server restarts, it's gone. The link then returns not found, the same as a wrong code."


def trust_bullets(memory_only: bool) -> tuple:
    return TRUST_BULLETS[:-1] + (MEMORY_LAST_BULLET,) if memory_only else TRUST_BULLETS
NOT_LIVE = "This link isn't live. It may have closed, or the code is wrong."
NOT_LIVE_NEXT = "If you were mid-conversation, start a new bridge and share the new link to pick up where you left off."
COUNTER_WINDOW = 5_000

# Shared client code: counters, the not-live screen, and the close line.
COMMON_JS = f"""
const MAX={MAX_CHARS},WIN={COUNTER_WINDOW};
function counter(t){{const c=document.getElementById(t.id+'-count');const upd=()=>{{const left=MAX-t.value.length;
c.hidden=left>WIN;c.textContent=left.toLocaleString()+' characters left';}};t.addEventListener('input',upd);upd();}}
function notLive(){{document.querySelector('main').innerHTML=document.getElementById('notlive').innerHTML;}}
function closesLine(d){{const t=new Date(d.expires_at).toLocaleString();
return d.reply_count>0?('Closes '+t+' if no one replies'):('Closes '+t+' unless someone replies');}}
"""

NOT_LIVE_HTML = (
    f'<template id="notlive"><h1>meld</h1><p>{escape(NOT_LIVE)}</p><p class="muted">{escape(NOT_LIVE_NEXT)}</p>'
    '<p><a href="/"><button type="button">Start a new bridge</button></a></p></template>'
)

HOME_JS = COMMON_JS + """
const f=document.getElementById('f'),out=document.getElementById('out'),err=document.getElementById('err');
counter(document.getElementById('note'));
let timer=null;
f.addEventListener('submit',async e=>{e.preventDefault();err.textContent='';
const note=document.getElementById('note').value;
const r=await fetch('/api/melds',{method:'POST',headers:{'content-type':'application/json','x-meld-surface':'ui'},body:JSON.stringify({note})});
const d=await r.json().catch(()=>({}));if(!r.ok){err.textContent=d.detail||'Error';return;}
f.hidden=true;out.hidden=false;const u=document.getElementById('url');u.value=d.url;u.select();
document.getElementById('exp').textContent='Open until '+new Date(d.expires_at).toLocaleString()+' for a first reply.';
document.getElementById('open').href=d.url;
const status=document.getElementById('status');status.textContent='Waiting for a first reply.';
timer=setInterval(async()=>{const g=await fetch('/api/melds/'+encodeURIComponent(d.code),{headers:{accept:'application/json'}});
if(g.status===404){clearInterval(timer);notLive();return;}if(!g.ok)return;const m=await g.json();
if(m.reply_count>0){clearInterval(timer);status.textContent='A reply arrived. Open until '+new Date(m.expires_at).toLocaleString()+'. Any reply renews it.';}},10000);});
"""


def _trust_block(memory_only: bool) -> str:
    return '<ul class="muted">' + "".join(f"<li>{escape(b)}</li>" for b in trust_bullets(memory_only)) + "</ul>"


def home(nonce: str, head_extra: str = "", memory_only: bool = True) -> str:
    body = f"""
<h1>meld</h1>
<p>{escape(HERO)}</p>
<form id="f"><textarea id="note" maxlength="{MAX_CHARS}" required placeholder="What this exchange is for. No passwords, keys, or customer data."></textarea>
<p class="meta" id="note-count" hidden></p>
<button type="submit">Create link</button></form>
<p id="err"></p>
<div id="out" hidden><p>Send this link privately:</p><input id="url" readonly><p class="meta" id="exp"></p>
<p class="meta" id="status"></p><p><a id="open" href="#">Open the bridge</a></p></div>
{_trust_block(memory_only)}
<p class="meta"><a href="/agents.md">agents.md</a> · <a href="/trust.md">trust.md</a> · <a href="/openapi.json">OpenAPI</a> · MCP at /mcp</p>
{NOT_LIVE_HTML}
"""
    return _page("meld — one link, one bridge", body, nonce, head_extra=head_extra, script=HOME_JS)


BRIDGE_JS = COMMON_JS + """
const f=document.getElementById('f'),err=document.getElementById('err');
counter(document.getElementById('reply'));
for(const t of document.querySelectorAll('time')){t.textContent=new Date(t.dateTime).toLocaleString();}
const code=f.dataset.code,seen=Number(f.dataset.count);
f.addEventListener('submit',async e=>{e.preventDefault();err.textContent='';
const context=document.getElementById('reply').value;
const r=await fetch('/api/melds/'+encodeURIComponent(code)+'/resolve',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({context})});
if(r.ok){location.reload();return;}if(r.status===404){notLive();return;}
const d=await r.json().catch(()=>({}));err.textContent=d.detail||'Error';});
setInterval(async()=>{if(document.getElementById('reply').value)return;
const g=await fetch('/api/melds/'+encodeURIComponent(code),{headers:{accept:'application/json'}});
if(g.status===404){notLive();return;}if(!g.ok)return;const m=await g.json();if(m.reply_count!==seen)location.reload();},10000);
"""


def _closes(meld: dict) -> str:
    when = f'<time datetime="{escape(meld["expires_at"])}">{escape(meld["expires_at"])}</time>'
    tail = "if no one replies" if int(meld["reply_count"]) > 0 else "unless someone replies"
    return f"Closes {when} {tail}."


def bridge(meld: dict, nonce: str, memory_only: bool = True) -> str:
    code = escape(meld["code"])
    items = [f'<div class="box">{escape(meld["note"])}</div>'
             f'<p class="meta">Note · <time datetime="{escape(meld["created_at"])}">{escape(meld["created_at"])}</time></p>']
    for i, r in enumerate(meld["replies"], 1):
        items.append(f'<div class="box">{escape(r["content"])}</div>'
                     f'<p class="meta">Reply {i} · <time datetime="{escape(r["created_at"])}">{escape(r["created_at"])}</time></p>')
    body = f"""
<h1>meld</h1>
<p class="meta">{_closes(meld)}</p>
<p class="warn">{escape(SECRETS)}</p>
{''.join(items)}
<form id="f" data-code="{code}" data-count="{int(meld["reply_count"])}"><textarea id="reply" maxlength="{MAX_CHARS}" required placeholder="Reply on this link"></textarea>
<p class="meta" id="reply-count" hidden></p>
<button type="submit">Reply</button></form><p id="err"></p>
{_trust_block(memory_only)}
{NOT_LIVE_HTML}
"""
    return _page("meld — bridge", body, nonce,
                 head_extra='<meta name="robots" content="noindex,nofollow">', script=BRIDGE_JS)


def not_found(nonce: str) -> str:
    body = (f'<h1>meld</h1><p>{escape(NOT_LIVE)}</p><p class="muted">{escape(NOT_LIVE_NEXT)}</p>'
            '<p><a href="/"><button type="button">Start a new bridge</button></a></p>')
    return _page("meld — not found", body, nonce, head_extra='<meta name="robots" content="noindex,nofollow">')


PREVIEW_TITLE = "meld — this bridge expires"
PREVIEW_DESC = "This link expires. The exchange is not included in this preview."


def preview_card(og_image: str | None) -> str:
    """Expires-only card for link-preview crawlers. Never reads the meld."""
    img = ""
    if og_image:
        img = (f'<meta property="og:image" content="{escape(og_image)}">'
               f'<meta name="twitter:image" content="{escape(og_image)}">')
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{PREVIEW_TITLE}</title><meta name=\"robots\" content=\"noindex,nofollow\">"
        f"<meta property=\"og:title\" content=\"{PREVIEW_TITLE}\">"
        f"<meta property=\"og:description\" content=\"{PREVIEW_DESC}\">"
        f"<meta name=\"twitter:card\" content=\"{'summary_large_image' if og_image else 'summary'}\">"
        f"<meta name=\"twitter:title\" content=\"{PREVIEW_TITLE}\">"
        f"<meta name=\"twitter:description\" content=\"{PREVIEW_DESC}\">{img}"
        f"</head><body><p>{PREVIEW_DESC}</p></body></html>"
    )
