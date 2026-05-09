"""Shared Markdown renderer.

Both the summary view and the chat reply path need the same rendering
behavior: tables + strikethrough enabled, and a post-pass that hooks
inline `[MM:SS](#t=...)` timestamp links into the inline-player JS.

Centralized here so a future renderer change (e.g. enabling task
lists, switching to a different parser) lands in one place rather
than scattered across routes.
"""

import re

from markdown_it import MarkdownIt

# GFM-style: tables + strikethrough. The summarizer prompt and the
# chat system prompt both encourage tables for two-column mappings,
# so the table extension is mandatory — otherwise pipe-syntax leaks
# through as raw text. (Linkify is left off — it requires the
# optional `linkify-it-py` dep, and the LLMs emit explicit
# [text](url) links anyway.)
#
# `html=False` is critical: this renderer runs over assistant chat
# replies and over LLM-generated summaries, both of which are
# untrusted. Allowing inline HTML would mean a prompt-injected
# `<script>` tag survives untouched into the page. With html=False,
# any literal HTML in the source gets escaped — only proper
# markdown syntax produces tags.
_md = (
    MarkdownIt("commonmark", {"html": False})
    .enable("table")
    .enable("strikethrough")
)


# The summarizer (and chat) instruct the LLM to emit
# `[MM:SS](#t=SECONDS)` markdown links for key moments. markdown-it
# renders those as `<a href="#t=754">12:34</a>`. The inline player JS
# picks up clicks via `data-yt-timestamp`, so we decorate the rendered
# HTML with that attribute (and a CSS hook class) before handing it to
# the template. The negative lookahead skips already-decorated anchors
# so running this twice on the same string is a no-op.
_TS_LINK_RE = re.compile(
    r'<a href="#t=(\d+)"(?![^>]*\bdata-yt-timestamp\b)>([^<]+)</a>'
)


def render_markdown(src: str) -> str:
    """Render markdown to HTML and wire up timestamp links.

    Pure function. Safe to call on empty / None-coerced strings.
    """
    if not src:
        return ""
    html = _md.render(src)
    return _decorate_timestamp_links(html)


def _decorate_timestamp_links(html: str) -> str:
    """Add `data-yt-timestamp` + `yt-ts` class to inline
    `<a href="#t=N">…</a>` anchors so the player JS can hook them.
    Pure function. Idempotent."""
    return _TS_LINK_RE.sub(
        r'<a href="#t=\1" data-yt-timestamp="\1" class="yt-ts">\2</a>',
        html,
    )
