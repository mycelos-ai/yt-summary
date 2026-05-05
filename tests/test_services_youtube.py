import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import respx
from httpx import Response

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


async def test_download_thumbnail_writes_file(tmp_path):
    from app.services.youtube import download_thumbnail
    target = tmp_path / "thumb.jpg"
    fake_jpeg = b"\xff\xd8\xff\xe0fakejpeg"
    with respx.mock:
        respx.get("https://img.example/thumb.jpg").mock(
            return_value=Response(200, content=fake_jpeg)
        )
        await download_thumbnail("https://img.example/thumb.jpg", target)
    assert target.read_bytes() == fake_jpeg


async def test_download_thumbnail_handles_missing_url(tmp_path):
    from app.services.youtube import download_thumbnail
    target = tmp_path / "thumb.jpg"
    await download_thumbnail(None, target)
    assert not target.exists()


def test_vtt_to_plain_text():
    from app.services.youtube import vtt_to_plain_text
    vtt = (FIXTURES / "sample.vtt").read_text()
    text = vtt_to_plain_text(vtt)
    assert "Hello and welcome." in text
    assert "FastAPI" in text
    assert "WEBVTT" not in text
    assert "-->" not in text


async def test_fetch_subtitles_prefers_manual():
    from app.services.youtube import fetch_subtitles
    fake_info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "https://example.com/manual.vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "https://example.com/auto.vtt"}]},
    }
    with (
        patch("app.services.youtube._extract_info_with_subs", return_value=fake_info),
        patch(
            "app.services.youtube._download_text",
            AsyncMock(return_value=(FIXTURES / "sample.vtt").read_text()),
        ),
    ):
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is not None
    text, source = result
    assert "FastAPI" in text
    assert source == "manual_subs"


async def test_fetch_subtitles_falls_back_to_auto():
    from app.services.youtube import fetch_subtitles
    fake_info = {
        "subtitles": {},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "https://example.com/auto.vtt"}]},
    }
    with (
        patch("app.services.youtube._extract_info_with_subs", return_value=fake_info),
        patch(
            "app.services.youtube._download_text",
            AsyncMock(return_value=(FIXTURES / "sample.vtt").read_text()),
        ),
    ):
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is not None
    _, source = result
    assert source == "auto_subs"


async def test_fetch_subtitles_returns_none_when_unavailable():
    from app.services.youtube import fetch_subtitles
    fake_info = {"subtitles": {}, "automatic_captions": {}}
    with patch("app.services.youtube._extract_info_with_subs", return_value=fake_info):
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is None
