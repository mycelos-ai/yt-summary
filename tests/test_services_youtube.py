import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.youtube import VideoMetadata, fetch_metadata, parse_video_id

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_video_id_short_url():
    assert parse_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_video_id_watch_url():
    assert parse_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s") == "dQw4w9WgXcQ"


def test_parse_video_id_shorts_url():
    assert parse_video_id("https://youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_video_id_invalid_raises():
    with pytest.raises(ValueError):
        parse_video_id("https://example.com/foo")


async def test_fetch_metadata_returns_dataclass():
    fixture = json.loads((FIXTURES / "yt_dlp_metadata.json").read_text())
    with patch("app.services.youtube._extract_info", return_value=fixture):
        meta = await fetch_metadata("https://youtu.be/dQw4w9WgXcQ", cookies_path=None)
    assert isinstance(meta, VideoMetadata)
    assert meta.id == "dQw4w9WgXcQ"
    assert meta.title == "Sample Title"
    assert meta.duration_seconds == 212
    assert meta.thumbnail_url is not None
    assert meta.thumbnail_url.endswith(".jpg")
