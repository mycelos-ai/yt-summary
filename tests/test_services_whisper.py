from unittest.mock import MagicMock, patch


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
