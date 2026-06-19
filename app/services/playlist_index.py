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
