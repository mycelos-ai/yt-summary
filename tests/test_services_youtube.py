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
    with respx.mock, patch(
        "app.services.youtube.validate_public_http_url", return_value=None,
    ):
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


async def test_download_thumbnail_rejects_private_target(tmp_path):
    from app.services.network_safety import UnsafeUrlError
    from app.services.youtube import download_thumbnail

    with pytest.raises(UnsafeUrlError, match="local or private"):
        await download_thumbnail("http://169.254.169.254/metadata", tmp_path / "x.jpg")


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
    text, segments, source, language = result
    assert "FastAPI" in text
    assert source == "manual_subs"
    # The bundled sample.vtt has no Language: header.
    assert language is None
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
    _text, _segments, source, _language = result
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


def test_vtt_to_segments_uses_inline_word_timestamps_for_auto_captions():
    """YouTube auto-captions emit per-word inline timestamps inside each
    cue (<00:00:01.040>). When we trim the rolling-window overlap and
    keep only the new tail, the tail's TRUE start time is the inline
    timestamp of its first word — not the cue's start time, which is
    when the rolling window OPENED for accumulation. Using the cue
    start makes the entire transcript drift earlier and earlier
    relative to the video; using the inline timestamp lines them up
    with the actual audio.

    This is the real-world shape from YouTube's en.vtt for auto-
    captioned videos (a Claude Code review video served as the
    canonical sample).
    """
    from app.services.youtube import vtt_to_segments
    sample = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:02.149 align:start position:0%\n"
        " \n"
        "So<00:00:00.240><c> Claude</c><00:00:00.640><c> Code</c>"
        "<00:00:00.880><c> just</c><00:00:01.040><c> released</c>"
        "<00:00:01.439><c> agents</c><00:00:01.920><c> view</c>\n"
        "\n"
        "00:00:02.149 --> 00:00:03.990 align:start position:0%\n"
        "So Claude Code just released agents view\n"
        "and<00:00:02.399><c> this</c><00:00:02.560><c> little</c>"
        "<00:00:02.720><c> feature</c><00:00:03.040><c> is</c>"
        "<00:00:03.200><c> a</c><00:00:03.360><c> godsend</c>"
        "<00:00:03.840><c> if</c>\n"
    )
    segs = vtt_to_segments(sample)
    # First cue: full opener, starts at the cue's own start (no prior
    # overlap → no inline timestamp consultation).
    assert segs[0] == (0.0, "So Claude Code just released agents view")
    # Second cue: the trimmed tail starts at the inline timestamp of
    # its first new word, "and" → 2.399. Using the cue start (2.149)
    # would be too early because the rolling window was already
    # repeating the previous sentence at that point.
    assert segs[1][0] == 2.399
    assert segs[1][1] == "and this little feature is a godsend if"


def test_vtt_to_segments_falls_back_to_cue_start_when_no_inline_timestamps():
    """Manual subtitles and older YouTube auto-caption formats don't
    carry inline word timestamps. Without them, the cue start is the
    only timing signal we have — use it. Existing behaviour preserved."""
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
    assert [s[0] for s in segs] == [0.0, 2.0, 4.0]
    assert [s[1] for s in segs] == [
        "Welcome back", "to MIT.", "Thank you.",
    ]


def test_vtt_to_segments_inline_timestamps_for_a_full_cue_with_no_trim():
    """When the cue has no overlap with the previous one (manual subs
    or the very first cue), the inline timestamps shouldn't change the
    cue's own start time — emit the cue start verbatim."""
    from app.services.youtube import vtt_to_segments
    sample = (
        "WEBVTT\n"
        "\n"
        "00:00:05.000 --> 00:00:07.000\n"
        "Hello<00:00:05.500><c> world</c>\n"
    )
    segs = vtt_to_segments(sample)
    assert segs == [(5.0, "Hello world")]


def test_base_opts_includes_remote_components_for_ejs():
    """yt-dlp 2026.x needs the EJS challenge-solver script to decode
    YouTube's signed n-parameter; without `remote_components: [ejs:github]`
    even a Deno-equipped container falls back to storyboard-only formats
    and any download fails with `Requested format is not available`."""
    from app.services.youtube import _base_opts

    opts = _base_opts(None)
    assert opts.get("remote_components") == ["ejs:github"]


def test_base_opts_attaches_cookiefile_when_provided(tmp_path):
    from app.services.youtube import _base_opts

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")
    opts = _base_opts(cookie_file)
    assert opts["cookiefile"] == str(cookie_file)
    # And remote_components is still set — every call needs it.
    assert opts["remote_components"] == ["ejs:github"]


def test_base_opts_omits_cookiefile_when_none():
    from app.services.youtube import _base_opts

    opts = _base_opts(None)
    assert "cookiefile" not in opts


async def test_fetch_subtitles_parses_language_header():
    """YouTube emits VTT files with a `Language: en` (or `de`, ...)
    header. fetch_subtitles should surface that as the 4th tuple
    element so the pipeline can stamp source_language without
    spending a Whisper run."""
    from app.services.youtube import fetch_subtitles

    fake_info = {
        "subtitles": {"de": [{"ext": "vtt", "url": "https://example.com/de.vtt"}]},
        "automatic_captions": {},
    }
    de_vtt = (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: de\n"
        "\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "Hallo Welt.\n"
    )
    with (
        patch("app.services.youtube._extract_info_with_subs", return_value=fake_info),
        patch(
            "app.services.youtube._download_text",
            AsyncMock(return_value=de_vtt),
        ),
    ):
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is not None
    _text, _segments, source, language = result
    assert source == "manual_subs"
    assert language == "de"
