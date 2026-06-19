"""Index a YouTube playlist via the official Data API (playlistItems.list).

Used as the primary playlist indexer when a youtube_api_key is configured;
the caller (playlist_sync) falls back to yt-dlp's fetch_playlist when no key
is set or this raises PlaylistApiError. Returns the same PlaylistMetadata /
PlaylistEntry shape as fetch_playlist so the sync pipeline is unchanged.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

import httpx

from app.services.playlist import PlaylistEntry, PlaylistMetadata

log = logging.getLogger(__name__)

_API_BASE = "https://www.googleapis.com/youtube/v3"
_MAX_PAGES = 40  # 40 * 50 = 2000 items — safety cap against infinite loops


class PlaylistApiError(Exception):
    """Any failure fetching/parsing the Data API response."""


def _playlist_id_from_url(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    values = qs.get("list")
    if not values or not values[0]:
        raise PlaylistApiError(f"No playlist id (list=) in URL: {url}")
    return values[0]


def _pick_thumbnail(thumbs: dict) -> str | None:
    """Highest-resolution thumbnail url from a snippet.thumbnails dict."""
    if not isinstance(thumbs, dict) or not thumbs:
        return None
    best = None
    best_w = -1
    for t in thumbs.values():
        if isinstance(t, dict) and t.get("url"):
            w = t.get("width") or 0
            if w >= best_w:
                best_w = w
                best = t["url"]
    return best


def _entry_from_item(item: dict) -> PlaylistEntry | None:
    content = item.get("contentDetails") or {}
    vid = content.get("videoId")
    if not vid:
        return None  # deleted/private placeholder
    snippet = item.get("snippet") or {}
    pos = snippet.get("position")
    position = (pos + 1) if isinstance(pos, int) else 0
    return PlaylistEntry(
        id=vid,
        title=snippet.get("title") or "",
        description=snippet.get("description") or "",
        thumbnail_url=_pick_thumbnail(snippet.get("thumbnails") or {}),
        duration_seconds=None,
        position=position,
    )


async def _fetch_title(client: httpx.AsyncClient, playlist_id: str, api_key: str):
    """Best-effort playlist title/description/thumbnail; empty on failure."""
    try:
        resp = await client.get(
            f"{_API_BASE}/playlists",
            params={"part": "snippet", "id": playlist_id, "key": api_key},
        )
        resp.raise_for_status()
        items = resp.json().get("items") or []
        if items:
            sn = items[0].get("snippet") or {}
            return (
                sn.get("title") or "",
                sn.get("description") or "",
                _pick_thumbnail(sn.get("thumbnails") or {}),
            )
    except Exception:  # noqa: BLE001 — title is cosmetic
        pass
    return ("", "", None)


async def fetch_via_api(url: str, *, api_key: str) -> PlaylistMetadata:
    """List a playlist fully via the Data API. Raises PlaylistApiError on any
    HTTP / network / parse failure (caller falls back to yt-dlp)."""
    playlist_id = _playlist_id_from_url(url)
    entries: list[PlaylistEntry] = []
    page_token: str | None = None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for _ in range(_MAX_PAGES):
                params = {
                    "part": "snippet,contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": 50,
                    "key": api_key,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(
                    f"{_API_BASE}/playlistItems", params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("items") or []:
                    entry = _entry_from_item(item)
                    if entry is not None:
                        entries.append(entry)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
            title, description, thumb = await _fetch_title(
                client, playlist_id, api_key,
            )
    except PlaylistApiError:
        raise
    except Exception as e:  # noqa: BLE001 — uniform fallback signal
        raise PlaylistApiError(f"YouTube API index failed: {e}") from e

    return PlaylistMetadata(
        id=playlist_id,
        url=url,
        title=title,
        description=description,
        thumbnail_url=thumb,
        entries=entries,
    )
