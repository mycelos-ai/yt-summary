"""Markdown rendering pinned: GFM tables work, timestamp links get
wired up for the inline player, both surfaces (summary + chat) share
the exact same renderer."""

from app.services.markdown import _decorate_timestamp_links, render_markdown


def test_render_markdown_renders_gfm_tables():
    """The summarizer prompt and the chat system prompt both
    encourage tables for two-column mappings. CommonMark alone
    doesn't render them — the renderer must have the table
    extension on. A regression here would leak raw pipes through
    to the user."""
    src = (
        "| Concept | Why it matters |\n"
        "|---------|----------------|\n"
        "| Self-attention | Each token attends to every other |\n"
        "| Softmax | Converts logits into probabilities |\n"
    )
    out = render_markdown(src)
    assert "<table>" in out
    assert "<th>Concept</th>" in out
    assert "<td>Self-attention</td>" in out


def test_render_markdown_renders_strikethrough():
    out = render_markdown("This is ~~outdated~~ now correct.")
    assert "<s>outdated</s>" in out


def test_render_markdown_decorates_timestamp_links():
    """Inline `[12:34](#t=754)` markdown links should pick up the
    data-yt-timestamp attribute so the player JS can hook them.
    Without this, the inline player can't seek when the user clicks
    a timestamp inside a chat reply or the summary."""
    out = render_markdown("See [12:34](#t=754) for the demo.")
    assert 'data-yt-timestamp="754"' in out
    assert 'class="yt-ts"' in out


def test_render_markdown_empty_string_returns_empty_string():
    assert render_markdown("") == ""


def test_render_markdown_idempotent_on_decoration():
    """Rendering decorated HTML twice through the post-pass shouldn't
    double-decorate. The regex's negative lookahead guards this."""
    once = _decorate_timestamp_links('<a href="#t=10">0:10</a>')
    twice = _decorate_timestamp_links(once)
    assert once == twice
    # And the count of data-yt-timestamp attrs stays at 1.
    assert twice.count("data-yt-timestamp") == 1
