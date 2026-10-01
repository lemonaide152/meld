"""Open Graph and Twitter card copy.

Marketing pages (/, /agents, /trust) share one card. Title and description
state four unfused meanings: a temporary resource to align context; pick
3 minutes, 1 hour, or 1 day; when the clock ends, the link dies; not for
secrets. Host-readable stays on the human trust warning and the TRUST page.
It is not part of this X description.

No mint-next. No claim that a conversation hard-stops.
No end-to-end, server-blind, or private-room wording.

Capability URLs (/m/{code}) use a generic expires-only card. The meld
body is never placed in these tags. Link-preview crawlers receive
capability_preview_document(), which has no script and no body.
"""

SITE = "https://meld.mergeinc.workers.dev"
OG_IMAGE_PATH = "/og.png"
OG_IMAGE_URL = SITE + OG_IMAGE_PATH
OG_IMAGE_W = "1200"
OG_IMAGE_H = "630"
OG_IMAGE_ALT = (
    "A temporary resource to align context. "
    "Pick 3 minutes, 1 hour, or 1 day. "
    "When the clock ends, the link dies. "
    "Not for secrets."
)

META_SLOT = "<!--MELD_PREVIEW-->"

MARKETING_TITLE = "A temporary resource to align context."
MARKETING_DESCRIPTION = (
    "A temporary resource to align context. "
    "Pick 3 minutes, 1 hour, or 1 day. "
    "When the clock ends, the link dies. "
    "Not for secrets."
)

CAPABILITY_TITLE = "meld — this bridge expires"
CAPABILITY_DESCRIPTION = "This link expires. The exchange is not included in this preview."

# Claims that must not appear on a card or in the preview document.
FORBIDDEN_CARD_CLAIMS = (
    "zero-knowledge",
    "zero knowledge",
    "end-to-end",
    "end to end",
    "e2e",
    "private",
    "pastebin",
    "weights",
    "trained",
    "safer than",
    "server-blind",
    "server blind",
    "mint-next",
    "mint next",
    "hard stop",
    "hard-stop",
    "conversation ends",
)


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def marketing_meta(url: str, *, include_title: bool = False) -> str:
    """Product-bar tags for /, /agents, and /trust.

    include_title is for documents that do not already have a <title>.
    The homepage keeps its own title element and still sets og:title.
    """
    title = _esc(MARKETING_TITLE)
    description = _esc(MARKETING_DESCRIPTION)
    page = _esc(url)
    image = _esc(OG_IMAGE_URL)
    alt = _esc(OG_IMAGE_ALT)
    title_tag = f"<title>{title}</title>\n" if include_title else ""
    return (
        title_tag
        + f'<meta name="description" content="{description}">\n'
        f'<meta property="og:title" content="{title}">\n'
        f'<meta property="og:description" content="{description}">\n'
        f'<meta property="og:type" content="website">\n'
        f'<meta property="og:url" content="{page}">\n'
        f'<meta property="og:image" content="{image}">\n'
        f'<meta property="og:image:type" content="image/png">\n'
        f'<meta property="og:image:width" content="{OG_IMAGE_W}">\n'
        f'<meta property="og:image:height" content="{OG_IMAGE_H}">\n'
        f'<meta property="og:image:alt" content="{alt}">\n'
        f'<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{title}">\n'
        f'<meta name="twitter:description" content="{description}">\n'
        f'<meta name="twitter:image" content="{image}">\n'
        f'<meta name="twitter:image:alt" content="{alt}">\n'
    )


def capability_meta(*, include_title: bool = False) -> str:
    """Generic expires-only card for /m/{{code}}. No image, no meld body."""
    title = _esc(CAPABILITY_TITLE)
    description = _esc(CAPABILITY_DESCRIPTION)
    title_tag = f"<title>{title}</title>\n" if include_title else ""
    return (
        title_tag
        + f'<meta name="description" content="{description}">\n'
        f'<meta name="robots" content="noindex, nofollow">\n'
        f'<meta property="og:title" content="{title}">\n'
        f'<meta property="og:description" content="{description}">\n'
        f'<meta property="og:type" content="website">\n'
        f'<meta name="twitter:card" content="summary">\n'
        f'<meta name="twitter:title" content="{title}">\n'
        f'<meta name="twitter:description" content="{description}">\n'
    )


def capability_preview_document() -> str:
    """Preview HTML for Slack/X/Discord and similar crawlers.

    No script and no meld body, so a crawler cannot unfurl the exchange.
    """
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        + capability_meta(include_title=True)
        + "</head>\n"
        "<body>\n"
        "<p>meld — this bridge expires</p>\n"
        "<p>This link expires. The exchange is not included in this preview.</p>\n"
        "</body>\n"
        "</html>\n"
    )
