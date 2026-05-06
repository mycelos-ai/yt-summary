import json
from pathlib import Path
from unittest.mock import patch

from app.services.playlist import PlaylistEntry, PlaylistMetadata, fetch_playlist

FIXTURES = Path(__file__).parent / "fixtures"


async def test_fetch_playlist_returns_dataclass():
    fixture = json.loads((FIXTURES / "yt_dlp_playlist.json").read_text())
    with patch("app.services.playlist._extract_playlist_info", return_value=fixture):
        meta = await fetch_playlist(
            "https://www.youtube.com/playlist?list=PLh9GXHYeT6w",
            cookies_path=None,
        )
    assert isinstance(meta, PlaylistMetadata)
    assert meta.id == "PLh9GXHYeT6wWS05I-U_3f1RtJKa58M9Lr"
    assert meta.title == "Sample Playlist"
    assert meta.thumbnail_url is not None
    assert len(meta.entries) == 2
    assert isinstance(meta.entries[0], PlaylistEntry)
    assert meta.entries[0].id == "vid-aaa-1234"
    assert meta.entries[0].title == "First entry"
    assert meta.entries[0].duration_seconds == 600
    assert meta.entries[0].thumbnail_url is not None


async def test_fetch_playlist_handles_missing_fields():
    minimal = {
        "id": "PLfoo",
        "title": "T",
        "webpage_url": "u",
        "entries": [
            {"id": "v1", "title": "v1"},
        ],
    }
    with patch("app.services.playlist._extract_playlist_info", return_value=minimal):
        meta = await fetch_playlist("u", cookies_path=None)
    assert meta.description == ""
    assert meta.thumbnail_url is None
    assert meta.entries[0].description == ""
    assert meta.entries[0].duration_seconds is None
    assert meta.entries[0].thumbnail_url is None


async def test_fetch_playlist_skips_empty_entries():
    """yt-dlp can yield None entries for unavailable videos."""
    payload = {
        "id": "PLfoo",
        "title": "T",
        "webpage_url": "u",
        "entries": [
            None,
            {"id": "v1", "title": "good"},
            None,
        ],
    }
    with patch("app.services.playlist._extract_playlist_info", return_value=payload):
        meta = await fetch_playlist("u", cookies_path=None)
    assert [e.id for e in meta.entries] == ["v1"]
