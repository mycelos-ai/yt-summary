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
    if show.description_pattern and show.description_pattern.lower() in (video.description or "").lower():
        return True
    return False


async def identify_from_metadata(db, video) -> list[DetectedSpeaker]:
    out: list[DetectedSpeaker] = []
    seen: set[str] = set()
    for show in await shows_repo.list_enabled(db, user_id=video.user_id):
        if not _matches(show, video):
            continue
        for host in json.loads(show.hosts_json or "[]"):
            if host.lower() not in seen:
                seen.add(host.lower())
                out.append(DetectedSpeaker(name=host, role="host", is_host=True))
        guest = _parse_guest(show.guest_rule, video.title) or _parse_guest(show.guest_rule, video.description)
        if guest and guest.lower() not in seen:
            seen.add(guest.lower())
            out.append(DetectedSpeaker(name=guest, role="guest", is_host=False))
        break  # first matching show wins
    return out
