"""Custom Jinja filters used across the templates."""

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates


def register_filters(templates: "Jinja2Templates") -> None:
    """Wire our custom filters onto a Jinja2Templates instance.

    Each route module owns its own Jinja2Templates instance, so this
    helper exists to keep them in sync without sharing global state.
    """
    templates.env.filters["relative_time"] = relative_time


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
