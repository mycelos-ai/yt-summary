import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL


@dataclass(frozen=True)
class PlaylistEntry:
    id: str
    title: str
    description: str
    thumbnail_url: str | None
    duration_seconds: int | None


@dataclass(frozen=True)
class PlaylistMetadata:
    id: str
    url: str
    title: str
    description: str
    thumbnail_url: str | None
    entries: list[PlaylistEntry]


def _extract_playlist_info(url: str, cookies_path: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)  # type: ignore[return-value]


def _pick_thumbnail(item: dict[str, Any]) -> str | None:
    """Pick the highest-resolution thumbnail. See app/services/youtube.py
    for the same logic — duplicated to avoid a cross-service import."""
    thumbs = item.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        with_width = [
            t for t in thumbs
            if isinstance(t, dict) and t.get("url") and isinstance(t.get("width"), int)
        ]
        if with_width:
            best = max(with_width, key=lambda t: t["width"])
            return best["url"]
        for t in reversed(thumbs):
            if isinstance(t, dict) and t.get("url"):
                return t["url"]
    return item.get("thumbnail")


def _entry_from_dict(raw: dict[str, Any]) -> PlaylistEntry:
    return PlaylistEntry(
        id=raw["id"],
        title=raw.get("title") or "",
        description=raw.get("description") or "",
        thumbnail_url=_pick_thumbnail(raw),
        duration_seconds=raw.get("duration"),
    )


async def fetch_playlist(
    url: str, cookies_path: Path | None
) -> PlaylistMetadata:
    info = await asyncio.to_thread(_extract_playlist_info, url, cookies_path)
    raw_entries = info.get("entries") or []
    entries = [_entry_from_dict(e) for e in raw_entries if e]
    return PlaylistMetadata(
        id=info["id"],
        url=info.get("webpage_url", url),
        title=info.get("title") or "",
        description=info.get("description") or "",
        thumbnail_url=_pick_thumbnail(info),
        entries=entries,
    )
