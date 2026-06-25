import json

from app.models import DetectedSpeaker
from app.repos import known_shows as shows_repo


def _parse_guest(rule: str | None, text: str) -> str | None:
    """Enumerated guest parser. rule is 'after:<sep>' or 'before:<sep>' or None."""
    if not rule or not text:
        return None
    mode, _, sep = rule.partition(":")
    if not sep:
        return None
    if mode == "after":
        idx = text.lower().find(sep.lower())
        if idx == -1:
            return None
        return text[idx + len(sep):].strip() or None
    if mode == "before":
        idx = text.find(sep)
        if idx == -1:
            return None
        return text[:idx].strip() or None
    return None


def _matches(show, video) -> bool:
    if show.channel_id and video.channel_id and show.channel_id == video.channel_id:
        return True
    if show.title_pattern and show.title_pattern.lower() in (video.title or "").lower():
        return True
    return bool(
        show.description_pattern
        and show.description_pattern.lower() in (video.description or "").lower()
    )


async def identify_from_metadata(db, video, *, known_shows=None) -> list[DetectedSpeaker]:
    """Detect speakers from a video's metadata via the enabled known-shows.

    `known_shows` lets a caller preload the enabled-shows list once and reuse
    it across many videos (e.g. the backfill loop) instead of re-querying per
    video. When None, the list is fetched for the video's owner.
    """
    out: list[DetectedSpeaker] = []
    seen: set[str] = set()
    shows = (known_shows if known_shows is not None
             else await shows_repo.list_enabled(db, user_id=video.user_id))
    for show in shows:
        if not _matches(show, video):
            continue
        for host in json.loads(show.hosts_json or "[]"):
            if host.lower() not in seen:
                seen.add(host.lower())
                out.append(DetectedSpeaker(name=host, role="host", is_host=True))
        guest = (
            _parse_guest(show.guest_rule, video.title)
            or _parse_guest(show.guest_rule, video.description)
        )
        if guest and guest.lower() not in seen:
            seen.add(guest.lower())
            out.append(DetectedSpeaker(name=guest, role="guest", is_host=False))
        break  # first matching show wins
    return out
