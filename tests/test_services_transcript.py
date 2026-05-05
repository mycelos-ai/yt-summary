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
