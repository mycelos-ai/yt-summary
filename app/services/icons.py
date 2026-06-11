"""Inline SVG icon set, exposed to Jinja as the `icon()` global.

One visual language for all UI icons: 24x24 viewBox, currentColor
strokes with round caps/joins, stroke-width 2.2 — matching the
line style of the logo (see docs/superpowers/specs/
2026-06-11-svg-logo-design.md). Icons inherit the surrounding text
color, so hover/active states need no extra CSS.

Usage in templates: {{ icon('headphones') }} or {{ icon('cpu', 26) }}.
"""

from markupsafe import Markup

# name -> inner SVG markup (everything between the <svg> tags)
_ICONS: dict[str, str] = {
    "search": (
        '<circle cx="10.5" cy="10.5" r="6.5"/>'
        '<line x1="15.3" y1="15.3" x2="20.5" y2="20.5"/>'
    ),
    "ask": (
        '<path d="M7.5 4h9a3.5 3.5 0 0 1 3.5 3.5v6a3.5 3.5 0 0 1-3.5 '
        '3.5h-4.2l-4.3 4v-4h-.5a3.5 3.5 0 0 1-3.5-3.5v-6a3.5 3.5 0 0 1 '
        '3.5-3.5z"/>'
    ),
    "check-circle": (
        '<circle cx="12" cy="12" r="8.5"/>'
        '<path d="M8.2 12.4l2.6 2.6 5-5.4"/>'
    ),
    "ban": (
        '<circle cx="12" cy="12" r="8.5"/>'
        '<line x1="6.2" y1="6.2" x2="17.8" y2="17.8"/>'
    ),
    "tag": (
        '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h5.4c.4 0 .78.16 1.06.44l7.6 '
        '7.6a1.5 1.5 0 0 1 0 2.12l-5.4 5.4a1.5 1.5 0 0 1-2.12 0l-7.6-7.6A1.5 '
        '1.5 0 0 1 4 10.9z"/>'
        '<path d="M9 9h.01"/>'
    ),
    "gear": (
        '<circle cx="12" cy="12" r="3.4"/>'
        '<path d="M12 2.8v2.8M12 18.4v2.8M21.2 12h-2.8M5.6 12H2.8M18.5 '
        '5.5l-2 2M7.5 16.5l-2 2M18.5 18.5l-2-2M7.5 7.5l-2-2"/>'
    ),
    "news": (
        '<rect x="3.5" y="5" width="17" height="14" rx="2"/>'
        '<path d="M7 9h4.5v4.5H7z"/>'
        '<path d="M14.5 9.5h3M14.5 13h3M7 16.5h10.5"/>'
    ),
    "play": '<path d="M8.5 5.5v13l10-6.5z" fill="currentColor"/>',
    "mail": (
        '<rect x="3.5" y="5.5" width="17" height="13" rx="2"/>'
        '<path d="M4.5 7.5l7.5 5.5 7.5-5.5"/>'
    ),
    "clock": (
        '<circle cx="12" cy="12" r="8.5"/>'
        '<path d="M12 7.5V12l3 2.5"/>'
    ),
    "bolt": '<path d="M13 3L6 13.5h4.5L11 21l7-10.5h-4.5z"/>',
    "headphones": (
        '<path d="M4.5 15v-3a7.5 7.5 0 0 1 15 0v3"/>'
        '<rect x="3.5" y="14" width="4.2" height="6" rx="1.8"/>'
        '<rect x="16.3" y="14" width="4.2" height="6" rx="1.8"/>'
    ),
    "pulse": '<path d="M3.5 12.5h3.6L9.6 6l4.4 12 2.4-5.5h4.1"/>',
    "clipboard": (
        '<rect x="5.5" y="5" width="13" height="15.5" rx="2"/>'
        '<rect x="9" y="3" width="6" height="3.6" rx="1.2"/>'
    ),
    "list": (
        '<path d="M8.5 6.5H20M8.5 12H20M8.5 17.5H20"/>'
        '<path d="M4 6.5h.01M4 12h.01M4 17.5h.01"/>'
    ),
    "doc": (
        '<rect x="5" y="3.5" width="14" height="17" rx="2"/>'
        '<path d="M8.5 8.5h7M8.5 12.5h7M8.5 16.5h4"/>'
    ),
    "globe": (
        '<circle cx="12" cy="12" r="8.5"/>'
        '<path d="M3.5 12h17M12 3.5c2.5 2.4 3.9 5.2 3.9 8.5s-1.4 6.1-3.9 '
        '8.5c-2.5-2.4-3.9-5.2-3.9-8.5s1.4-6.1 3.9-8.5z"/>'
    ),
    "cpu": (
        '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>'
        '<rect x="10.2" y="10.2" width="3.6" height="3.6"/>'
        '<path d="M9.5 3.2v3.3M14.5 3.2v3.3M9.5 17.5v3.3M14.5 17.5v3.3M3.2 '
        '9.5h3.3M3.2 14.5h3.3M17.5 9.5h3.3M17.5 14.5h3.3"/>'
    ),
    "volume": (
        '<path d="M11.5 5.5L7 9H4v6h3l4.5 3.5z"/>'
        '<path d="M15 9.3a4 4 0 0 1 0 5.4M17.8 6.8a7.6 7.6 0 0 1 0 10.4"/>'
    ),
    "mic": (
        '<rect x="9" y="3.5" width="6" height="11" rx="3"/>'
        '<path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v2.5"/>'
    ),
    "bookmark": '<path d="M6.5 4h11v16.5L12 16.7l-5.5 3.8z"/>',
    "flask": (
        '<path d="M10 3.5h4M10.5 3.5v5.2L4.9 18.4a1.9 1.9 0 0 0 1.7 '
        '2.8h10.8a1.9 1.9 0 0 0 1.7-2.8L13.5 8.7V3.5"/>'
        '<path d="M7.2 14.5h9.6"/>'
    ),
    "thumb-up": (
        '<path d="M8 11.2L11.5 4a2.4 2.4 0 0 1 1.9 2.4v3h4.4a2 2 0 0 1 2 '
        '2.4l-1.1 6a2 2 0 0 1-2 1.7H8z"/>'
        '<path d="M8 11.2H4.5v8.3H8z"/>'
    ),
    "thumb-down": (
        '<path d="M16 12.8L12.5 20a2.4 2.4 0 0 1-1.9-2.4v-3H6.2a2 2 0 0 '
        '1-2-2.4l1.1-6a2 2 0 0 1 2-1.7H16z"/>'
        '<path d="M16 12.8h3.5V4.5H16z"/>'
    ),
    "link": (
        '<path d="M10.2 13.8a4.2 4.2 0 0 0 6 .2l2.4-2.4a4.2 4.2 0 0 '
        '0-5.9-5.9l-1.3 1.3"/>'
        '<path d="M13.8 10.2a4.2 4.2 0 0 0-6-.2l-2.4 2.4a4.2 4.2 0 0 0 5.9 '
        '5.9l1.3-1.3"/>'
    ),
    "broadcast": (
        '<path d="M12 12h.01"/>'
        '<path d="M8.2 15.8a5.4 5.4 0 0 1 0-7.6M15.8 8.2a5.4 5.4 0 0 1 0 '
        '7.6M5.3 18.7a9.5 9.5 0 0 1 0-13.4M18.7 5.3a9.5 9.5 0 0 1 0 13.4"/>'
    ),
    "plug": (
        '<path d="M9 3v4.5M15 3v4.5M6.5 7.5h11v3.2a5.5 5.5 0 0 1-11 0z"/>'
        '<path d="M12 16.2V21"/>'
    ),
    "target": (
        '<circle cx="12" cy="12" r="8.5"/>'
        '<circle cx="12" cy="12" r="4.5"/>'
        '<path d="M12 12h.01"/>'
    ),
    "download": (
        '<path d="M12 4v11M7.5 11.5L12 16l4.5-4.5"/>'
        '<path d="M4.5 19.5h15"/>'
    ),
}


def icon(name: str, size: int = 14) -> Markup:
    """Render the named icon as an inline <svg>, sized in CSS pixels."""
    try:
        inner = _ICONS[name]
    except KeyError:
        raise ValueError(f"unknown icon {name!r}") from None
    return Markup(
        f'<svg class="icon icon-{name}" width="{size}" height="{size}" '
        f'viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{inner}</svg>'
    )
