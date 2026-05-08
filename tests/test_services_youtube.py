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
    text, segments, source = result
    assert "FastAPI" in text
    assert source == "manual_subs"
    # vtt_to_segments yields (start_seconds, text) tuples
    assert len(segments) > 0
    assert all(isinstance(s, tuple) and len(s) == 2 for s in segments)


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
    _text, _segments, source = result
    assert source == "auto_subs"


async def test_fetch_subtitles_returns_none_when_unavailable():
    from app.services.youtube import fetch_subtitles
    fake_info = {"subtitles": {}, "automatic_captions": {}}
    with patch("app.services.youtube._extract_info_with_subs", return_value=fake_info):
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is None


async def test_download_audio_calls_yt_dlp_with_correct_opts(tmp_path):
    from app.services.youtube import download_audio
    captured: dict = {}

    def fake_download(opts, url):
        captured["opts"] = opts
        captured["url"] = url
        # Simulate file creation by yt-dlp
        (tmp_path / "vid.m4a").write_bytes(b"fakeaudio")

    with patch("app.services.youtube._run_yt_dlp_download", side_effect=fake_download):
        path = await download_audio("https://youtu.be/x", "vid", tmp_path, cookies_path=None)
    assert path == tmp_path / "vid.m4a"
    assert captured["opts"]["format"].startswith("bestaudio")


async def test_fetch_metadata_includes_tags():
    fixture = json.loads((FIXTURES / "yt_dlp_metadata.json").read_text())
    fixture["tags"] = ["alpha", "beta"]
    with patch("app.services.youtube._extract_info", return_value=fixture):
        meta = await fetch_metadata("https://youtu.be/x", cookies_path=None)
    assert meta.tags == ("alpha", "beta")


async def test_fetch_metadata_handles_missing_tags():
    fixture = json.loads((FIXTURES / "yt_dlp_metadata.json").read_text())
    fixture.pop("tags", None)
    with patch("app.services.youtube._extract_info", return_value=fixture):
        meta = await fetch_metadata("https://youtu.be/x", cookies_path=None)
    assert meta.tags == ()


async def test_fetch_metadata_filters_non_string_tags():
    fixture = json.loads((FIXTURES / "yt_dlp_metadata.json").read_text())
    fixture["tags"] = ["good", "", None, 42, "  ", "fine"]
    with patch("app.services.youtube._extract_info", return_value=fixture):
        meta = await fetch_metadata("https://youtu.be/x", cookies_path=None)
    assert meta.tags == ("good", "fine")


def test_pick_best_thumbnail_picks_highest_width():
    from app.services.youtube import _pick_best_thumbnail
    info = {
        "thumbnail": "https://example.com/hqdefault.jpg",
        "thumbnails": [
            {"url": "https://example.com/sddefault.jpg", "width": 640, "height": 480},
            {"url": "https://example.com/maxres.jpg", "width": 1280, "height": 720},
            {"url": "https://example.com/hqdefault.jpg", "width": 480, "height": 360},
        ],
    }
    assert _pick_best_thumbnail(info) == "https://example.com/maxres.jpg"


def test_pick_best_thumbnail_falls_back_to_top_level_when_no_thumbnails_list():
    from app.services.youtube import _pick_best_thumbnail
    info = {"thumbnail": "https://example.com/hq.jpg"}
    assert _pick_best_thumbnail(info) == "https://example.com/hq.jpg"


def test_pick_best_thumbnail_returns_none_when_empty():
    from app.services.youtube import _pick_best_thumbnail
    assert _pick_best_thumbnail({}) is None


def test_pick_best_thumbnail_takes_last_when_no_width_info():
    from app.services.youtube import _pick_best_thumbnail
    info = {
        "thumbnails": [
            {"url": "https://example.com/a.jpg"},
            {"url": "https://example.com/b.jpg"},
        ],
    }
    assert _pick_best_thumbnail(info) == "https://example.com/b.jpg"


async def test_fetch_subtitles_returns_none_on_429():
    """YouTube ratelimits the timedtext endpoint occasionally. We treat
    that as 'no subtitles available' so the worker falls back to
    Whisper instead of failing the whole job."""
    from httpx import HTTPStatusError, Request, Response

    from app.services.youtube import fetch_subtitles
    fake_info = {
        "subtitles": {},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "https://example.com/auto.vtt"}]},
    }
    rate_limited = HTTPStatusError(
        "429 Too Many Requests",
        request=Request("GET", "https://example.com/auto.vtt"),
        response=Response(429),
    )
    with (
        patch("app.services.youtube._extract_info_with_subs", return_value=fake_info),
        patch(
            "app.services.youtube._download_text",
            AsyncMock(side_effect=rate_limited),
        ),
    ):
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is None


async def test_fetch_subtitles_returns_none_on_5xx():
    """Same fallback behaviour for transient YouTube server errors."""
    from httpx import HTTPStatusError, Request, Response

    from app.services.youtube import fetch_subtitles
    fake_info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "https://example.com/manual.vtt"}]},
        "automatic_captions": {},
    }
    server_error = HTTPStatusError(
        "503 Service Unavailable",
        request=Request("GET", "https://example.com/manual.vtt"),
        response=Response(503),
    )
    with (
        patch("app.services.youtube._extract_info_with_subs", return_value=fake_info),
        patch(
            "app.services.youtube._download_text",
            AsyncMock(side_effect=server_error),
        ),
    ):
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is None


async def test_fetch_subtitles_propagates_other_4xx():
    """A 401/403 means our cookies are bad — that's not transient,
    don't silently fall back to Whisper. Let it bubble."""
    from httpx import HTTPStatusError, Request, Response

    from app.services.youtube import fetch_subtitles
    fake_info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "https://example.com/manual.vtt"}]},
        "automatic_captions": {},
    }
    auth_error = HTTPStatusError(
        "403 Forbidden",
        request=Request("GET", "https://example.com/manual.vtt"),
        response=Response(403),
    )
    with (
        patch("app.services.youtube._extract_info_with_subs", return_value=fake_info),
        patch(
            "app.services.youtube._download_text",
            AsyncMock(side_effect=auth_error),
        ),
        pytest.raises(HTTPStatusError),
    ):
        await fetch_subtitles("https://youtu.be/x", cookies_path=None)


def test_vtt_to_segments_decodes_html_entities():
    """YouTube wraps speaker markers `>>` as `&gt;&gt;` in their VTT.
    The parser must unescape these, otherwise &gt;&gt; leaks into the
    rendered transcript as literal characters."""
    from app.services.youtube import vtt_to_segments
    sample = """WEBVTT

00:00:00.000 --> 00:00:02.000
&gt;&gt; Hello there. Don&#39;t go.
"""
    segs = vtt_to_segments(sample)
    assert segs == [(0.0, ">> Hello there. Don't go.")]


def test_vtt_to_segments_collapses_rolling_window_dupes():
    """YouTube auto-captions emit cumulative cues. Each new cue should
    only contribute the *new* words, not re-emit the whole window."""
    from app.services.youtube import vtt_to_segments
    sample = """WEBVTT

00:00:00.000 --> 00:00:02.000
Welcome back

00:00:02.000 --> 00:00:04.000
Welcome back to MIT.

00:00:04.000 --> 00:00:06.000
to MIT. Thank you.
"""
    segs = vtt_to_segments(sample)
    starts = [s[0] for s in segs]
    texts = [s[1] for s in segs]
    # First cue: full opener
    assert "Welcome back" in texts[0]
    # Second cue: only the *new* tail "to MIT."
    # (The first cue contained "Welcome back", so "to MIT." is the new part)
    assert texts[1] == "to MIT."
    # Third cue: only "Thank you." — the "to MIT." prefix is the overlap
    assert texts[2] == "Thank you."
    # All starts preserved
    assert starts == [0.0, 2.0, 4.0]


def test_vtt_to_segments_drops_fully_duplicate_repeats():
    """When a cue has no new content compared to the previous one
    (pure repeat without growth), drop it entirely."""
    from app.services.youtube import vtt_to_segments
    sample = """WEBVTT

00:00:00.000 --> 00:00:02.000
Hello world.

00:00:02.000 --> 00:00:04.000
Hello world.
"""
    segs = vtt_to_segments(sample)
    # Only one block survives
    assert len(segs) == 1
    assert segs[0] == (0.0, "Hello world.")
