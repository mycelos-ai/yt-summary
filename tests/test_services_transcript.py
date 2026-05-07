from unittest.mock import AsyncMock, patch


async def test_obtain_transcript_uses_subs_when_available(tmp_path):
    from app.services.transcript import obtain_transcript
    with (
        patch(
            "app.services.transcript.fetch_subtitles",
            AsyncMock(return_value=("subs text", "manual_subs")),
        ),
        patch("app.services.transcript.download_audio") as audio_mock,
    ):
        text, source = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
        )
    assert text == "subs text"
    assert source.value == "manual_subs"
    audio_mock.assert_not_called()


async def test_obtain_transcript_falls_back_to_whisper(tmp_path):
    from app.services.transcript import obtain_transcript
    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"")
    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.transcribe", return_value="whispered"),
    ):
        text, source = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
        )
    assert text == "whispered"
    assert source.value == "whisper"


async def test_obtain_transcript_deletes_audio_after_whisper(tmp_path):
    from app.services.transcript import obtain_transcript
    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")
    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.transcribe", return_value="whispered"),
    ):
        await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
        )
    assert not fake_audio.exists()


async def test_obtain_transcript_calls_progress_callback_during_whisper(tmp_path):
    """obtain_transcript should funnel Whisper segment progress out to
    the caller as human-readable strings like
    'transcribing 0:30 / 1:00 (50%)'."""
    from app.services.transcript import obtain_transcript

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")

    captured: list[str] = []

    async def progress(step: str) -> None:
        captured.append(step)

    def fake_transcribe(audio_path, model_name, progress=None):
        # Whisper would report segment-end / duration as it goes.
        if progress is not None:
            progress(30.0, 60.0)
            progress(60.0, 60.0)
        return "whispered"

    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.transcribe", side_effect=fake_transcribe),
    ):
        await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
            progress_cb=progress,
        )

    assert any("transcribing" in s and "50%" in s for s in captured), captured
    assert any("100%" in s for s in captured), captured
