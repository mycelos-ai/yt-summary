from unittest.mock import AsyncMock, patch


async def test_obtain_transcript_uses_subs_when_available(tmp_path):
    from app.services.transcript import obtain_transcript
    with (
        patch(
            "app.services.transcript.fetch_subtitles",
            AsyncMock(return_value=("subs text", [(0.0, "subs text")], "manual_subs", "en")),
        ),
        patch("app.services.transcript.download_audio") as audio_mock,
    ):
        text, segments, source, language = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
        )
    assert text == "subs text"
    assert segments == [(0.0, "subs text")]
    assert source.value == "manual_subs"
    assert language == "en"
    audio_mock.assert_not_called()


async def test_obtain_transcript_falls_back_to_whisper(tmp_path):
    from app.services.transcript import obtain_transcript
    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"")
    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch(
            "app.services.transcript.transcribe",
            return_value=("whispered", [(0.0, "whispered")], "en"),
        ),
    ):
        text, segments, source, language = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
        )
    assert text == "whispered"
    assert segments == [(0.0, "whispered")]
    assert source.value == "whisper"
    assert language == "en"


async def test_obtain_transcript_deletes_audio_after_whisper(tmp_path):
    from app.services.transcript import obtain_transcript
    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")
    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch(
            "app.services.transcript.transcribe",
            return_value=("whispered", [], None),
        ),
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
        return ("whispered", [(0.0, "whispered")], "en")

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


async def test_obtain_transcript_uses_whisper_api_when_base_url_set(tmp_path):
    """When whisper_base_url is set, obtain_transcript routes audio
    through the hosted Whisper endpoint instead of running Whisper
    locally."""
    from app.services.transcript import obtain_transcript

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")

    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch(
            "app.services.transcript.transcribe_via_api",
            AsyncMock(
                return_value=(
                    "hosted whisper text",
                    [(1.5, "hosted whisper text")],
                    "en",
                )
            ),
        ) as api_mock,
        patch("app.services.transcript.transcribe") as local_mock,
    ):
        text, segments, source, language = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="whisper-large-v3",
            whisper_base_url="https://api.groq.com/openai/v1",
            whisper_api_key="gsk-test",
        )

    assert text == "hosted whisper text"
    assert segments == [(1.5, "hosted whisper text")]
    assert source.value == "whisper"
    assert language == "en"
    api_mock.assert_called_once()
    kwargs = api_mock.call_args.kwargs
    assert kwargs["base_url"] == "https://api.groq.com/openai/v1"
    assert kwargs["api_key"] == "gsk-test"
    assert kwargs["model_name"] == "whisper-large-v3"
    local_mock.assert_not_called()


async def test_obtain_transcript_local_when_no_base_url(tmp_path):
    """Empty whisper_base_url means: run Whisper locally (status quo)."""
    from app.services.transcript import obtain_transcript

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")

    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.transcribe", return_value=("local", [], None)),
        patch("app.services.transcript.transcribe_via_api") as api_mock,
    ):
        text, segments, _src, _language = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
            whisper_base_url="",
            whisper_api_key="",
        )
    assert text == "local"
    assert segments == []
    api_mock.assert_not_called()


async def test_obtain_transcript_api_path_deletes_audio_after(tmp_path):
    """The audio file should be cleaned up regardless of which Whisper
    backend was used."""
    from app.services.transcript import obtain_transcript

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")

    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch(
            "app.services.transcript.transcribe_via_api",
            AsyncMock(return_value=("x", [], None)),
        ),
    ):
        await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="m",
            whisper_base_url="https://api.example.com",
            whisper_api_key="k",
        )
    assert not fake_audio.exists()
