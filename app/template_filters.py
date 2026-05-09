"""Custom Jinja filters used across the templates."""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates


# mtime of app.css, re-checked on every render so dev edits invalidate
# browser caches without restarting the server. One stat() call per
# template render is negligible compared to the surrounding I/O.
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version() -> str:
    css = _STATIC_DIR / "app.css"
    try:
        return str(int(css.stat().st_mtime))
    except OSError:
        return "0"


def register_filters(templates: "Jinja2Templates") -> None:
    """Wire our custom filters onto a Jinja2Templates instance.

    Each route module owns its own Jinja2Templates instance, so this
    helper exists to keep them in sync without sharing global state.
    """
    templates.env.filters["relative_time"] = relative_time
    templates.env.filters["format_duration"] = format_duration
    # Lazy import — markdown.py imports markdown_it which is
    # noticeable on import. Loading the filter on first use keeps
    # template-only routes (settings, status fragments) snappy.
    from app.services.markdown import render_markdown
    templates.env.filters["render_markdown"] = render_markdown
    # avatar_bg(avatar_id) → "#hex" — drives the per-avatar pastel
    # CSS variable in the _avatar.html macro.
    from app.services.avatars import bg_color_for
    templates.env.filters["avatar_bg"] = bg_color_for
    # Expose as a callable so each render reads the current mtime.
    templates.env.globals["asset_version"] = _asset_version


def relative_time(dt: datetime | None, *, now: datetime | None = None) -> str:
    """Human-readable relative time, only down to the same calendar day.

    Earlier today → "X minutes ago" / "X hours ago" / "just now".
    Anything before today → ISO date (YYYY-MM-DD).

    The `now` parameter is for tests; production callers always omit it.
    """
    if dt is None:
        return ""
    if now is None:
        now = datetime.now()

    # Different calendar day → fall back to date for stability
    if dt.date() != now.date():
        return dt.strftime("%Y-%m-%d")

    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


def format_duration(seconds: int | None) -> str:
    """Human-readable duration: MM:SS or HH:MM:SS depending on length.

    Used in the player caption to show how long a video is, so users
    can decide whether they actually want to commit to it.
    """
    if seconds is None or seconds <= 0:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
