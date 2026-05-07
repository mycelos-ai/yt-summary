from unittest.mock import MagicMock, patch

import pytest


def test_transcribe_returns_concatenated_segments(tmp_path):
    from app.services.whisper import transcribe
    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"")

    seg1 = MagicMock()
    seg1.text = "Hello and"
    seg2 = MagicMock()
    seg2.text = " welcome."

    fake_model = MagicMock()
    fake_model.transcribe.return_value = (iter([seg1, seg2]), MagicMock())

    with patch("app.services.whisper._load_model", return_value=fake_model):
        result = transcribe(fake_audio, model_name="small")
    assert result.strip() == "Hello and welcome."


def test_load_model_caches_per_name():
    from app.services import whisper as w
    w._MODEL_CACHE.clear()
    with patch("app.services.whisper.WhisperModel") as model:
        model.return_value = MagicMock()
        w._load_model("small")
        w._load_model("small")
        assert model.call_count == 1
        w._load_model("base")
        assert model.call_count == 2


def test_transcribe_invokes_progress_callback_per_segment(tmp_path):
    """transcribe should report (current_seconds, total_seconds) as it
    consumes Whisper's segment generator, so callers can show progress."""
    from app.services.whisper import transcribe
    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"")

    seg1 = MagicMock()
    seg1.text = "Hello"
    seg1.end = 5.0
    seg2 = MagicMock()
    seg2.text = " world"
    seg2.end = 10.0

    info = MagicMock()
    info.duration = 10.0

    fake_model = MagicMock()
    fake_model.transcribe.return_value = (iter([seg1, seg2]), info)

    captured: list[tuple[float, float]] = []

    def progress(current: float, total: float) -> None:
        captured.append((current, total))

    with patch("app.services.whisper._load_model", return_value=fake_model):
        result = transcribe(fake_audio, model_name="small", progress=progress)

    assert "Hello world" in result
    # We should have at least one progress report where we're done
    assert (10.0, 10.0) in captured


async def test_transcribe_via_api_posts_audio_and_returns_text(tmp_path):
    """transcribe_via_api should POST a multipart/form-data request to
    {base_url}/audio/transcriptions in the OpenAI Whisper format and
    return the transcribed text."""
    import respx
    from httpx import Response

    from app.services.whisper import transcribe_via_api

    audio = tmp_path / "x.m4a"
    audio.write_bytes(b"fakeaudio-bytes")

    with respx.mock(base_url="https://api.example.com") as mock:
        route = mock.post("/audio/transcriptions").mock(
            return_value=Response(
                200, json={"text": "hello world from whisper"}
            )
        )
        text = await transcribe_via_api(
            audio,
            base_url="https://api.example.com",
            api_key="sk-test",
            model_name="whisper-large-v3",
        )

    assert text == "hello world from whisper"
    assert route.called
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-test"
    # Multipart body should mention the model name and the audio bytes
    body = request.content
    assert b"whisper-large-v3" in body
    assert b"fakeaudio-bytes" in body


async def test_transcribe_via_api_strips_trailing_slash_in_base_url(tmp_path):
    """A user's base_url with a trailing slash should still produce
    valid '{base}/audio/transcriptions' (no double slash)."""
    import respx
    from httpx import Response

    from app.services.whisper import transcribe_via_api

    audio = tmp_path / "x.m4a"
    audio.write_bytes(b"x")

    with respx.mock(base_url="https://api.example.com") as mock:
        route = mock.post("/audio/transcriptions").mock(
            return_value=Response(200, json={"text": "ok"})
        )
        text = await transcribe_via_api(
            audio,
            base_url="https://api.example.com/",
            api_key="k",
            model_name="m",
        )
    assert text == "ok"
    assert route.called


async def test_transcribe_via_api_omits_auth_when_no_key(tmp_path):
    """Local servers (e.g. faster-whisper-server in Docker) often don't
    require auth. If api_key is empty, send no Authorization header."""
    import respx
    from httpx import Response

    from app.services.whisper import transcribe_via_api

    audio = tmp_path / "x.m4a"
    audio.write_bytes(b"x")

    with respx.mock(base_url="http://localhost:8000") as mock:
        route = mock.post("/audio/transcriptions").mock(
            return_value=Response(200, json={"text": "ok"})
        )
        await transcribe_via_api(
            audio,
            base_url="http://localhost:8000",
            api_key="",
            model_name="whisper-1",
        )
    request = route.calls.last.request
    assert "authorization" not in {k.lower() for k in request.headers}


async def test_transcribe_via_api_raises_on_http_error(tmp_path):
    """Non-2xx responses should raise so the worker marks the job
    failed (or higher-level code can decide on a fallback)."""
    import respx
    from httpx import HTTPStatusError, Response

    from app.services.whisper import transcribe_via_api

    audio = tmp_path / "x.m4a"
    audio.write_bytes(b"x")

    with (
        respx.mock(base_url="https://api.example.com") as mock,
        pytest.raises(HTTPStatusError),
    ):
        mock.post("/audio/transcriptions").mock(
            return_value=Response(500, text="internal server error")
        )
        await transcribe_via_api(
            audio,
            base_url="https://api.example.com",
            api_key="k",
            model_name="m",
        )
