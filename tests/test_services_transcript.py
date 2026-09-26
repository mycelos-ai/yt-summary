import asyncio
from unittest.mock import AsyncMock, patch


def _fake_extract(n_chunks: int, seen_dirs: list | None = None):
    """extract_chunk stand-in: writes n_chunks files, then reports end."""
    async def fake(audio_path, out_path, *, start_s, length_s):
        index = int(out_path.stem.split("_")[1])
        if index >= n_chunks:
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"c")
        if seen_dirs is not None:
            seen_dirs.append(out_path.parent)
        return out_path
    return fake


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

    def fake_transcribe(audio_path, model_name, progress=None, cpu_threads=0, language=None):
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


async def test_obtain_transcript_refuses_local_whisper_over_max_duration(tmp_path):
    """Local Whisper on a Pi can take hours on a long video and pin
    every core. When the video is longer than the cap we fail fast
    with a clear message — before the audio download even starts."""
    import pytest

    from app.services.transcript import TranscriptTooLongError, obtain_transcript

    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock()) as dl,
        patch("app.services.transcript.transcribe") as local_mock,
        pytest.raises(TranscriptTooLongError) as exc,
    ):
        await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
            duration_seconds=3600,
            max_whisper_duration_s=1800,
        )
    assert "60 min" in str(exc.value)
    assert "30 min" in str(exc.value)
    dl.assert_not_called()
    local_mock.assert_not_called()


async def test_obtain_transcript_duration_cap_ignored_for_hosted_whisper(tmp_path):
    """The cap protects the local CPU only. Groq / a Mac mini can
    handle long audio, so a configured base_url bypasses it."""
    from app.services.transcript import obtain_transcript

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")
    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.extract_chunk", side_effect=_fake_extract(1)),
        patch(
            "app.services.transcript.transcribe_via_api",
            AsyncMock(return_value=("hosted", [], "en")),
        ) as api_mock,
    ):
        text, _, _, _ = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="whisper-large-v3",
            whisper_base_url="https://api.groq.com/openai/v1",
            duration_seconds=3600,
            max_whisper_duration_s=1800,
        )
    assert text == "hosted"
    api_mock.assert_called_once()


async def test_obtain_transcript_cap_zero_or_unknown_duration_allows_local(tmp_path):
    from app.services.transcript import obtain_transcript

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"")
    for duration, cap in ((3600, 0), (None, 1800), (1800, 1800)):
        with (
            patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
            patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
            patch("app.services.transcript.extract_chunk", side_effect=_fake_extract(1)),
            patch(
                "app.services.transcript.transcribe",
                return_value=("ok", [], None),
            ) as local_mock,
        ):
            text, _, _, _ = await obtain_transcript(
                url="https://youtu.be/x",
                video_id="x",
                audio_dir=tmp_path,
                cookies_path=None,
                whisper_model="small",
                duration_seconds=duration,
                max_whisper_duration_s=cap,
                whisper_cpu_threads=2,
            )
        assert text == "ok"
        assert local_mock.call_args.kwargs["cpu_threads"] == 2
        fake_audio.write_bytes(b"")


async def test_obtain_transcript_unknown_duration_probed_after_download(tmp_path):
    """Playlist entries from the YouTube Data API carry no duration, so
    the pre-download cap can't fire. The downloaded audio is probed and
    the cap enforced before local Whisper starts; the audio is deleted
    and the measured duration is reported back for persisting."""
    import pytest

    from app.services.transcript import TranscriptTooLongError, obtain_transcript

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")
    reported: list[int] = []

    async def on_duration(seconds: int) -> None:
        reported.append(seconds)

    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.probe_duration_seconds", AsyncMock(return_value=3600)),
        patch("app.services.transcript.transcribe") as local_mock,
        pytest.raises(TranscriptTooLongError) as exc,
    ):
        await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
            duration_seconds=None,
            max_whisper_duration_s=1800,
            duration_cb=on_duration,
        )
    assert "60 min" in str(exc.value)
    local_mock.assert_not_called()
    assert not fake_audio.exists()
    assert reported == [3600]


async def test_obtain_transcript_probed_duration_reported_for_hosted_whisper(tmp_path):
    from app.services.transcript import obtain_transcript

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")
    reported: list[int] = []

    async def on_duration(seconds: int) -> None:
        reported.append(seconds)

    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.extract_chunk", side_effect=_fake_extract(1)),
        patch("app.services.transcript.probe_duration_seconds", AsyncMock(return_value=3600)),
        patch(
            "app.services.transcript.transcribe_via_api",
            AsyncMock(return_value=("hosted", [], "en")),
        ),
    ):
        text, _, _, _ = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="whisper-large-v3",
            whisper_base_url="https://api.groq.com/openai/v1",
            duration_seconds=None,
            max_whisper_duration_s=1800,
            duration_cb=on_duration,
        )
    assert text == "hosted"
    assert reported == [3600]


async def test_obtain_transcript_known_duration_not_probed(tmp_path):
    from app.services.transcript import obtain_transcript

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"")
    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.probe_duration_seconds", AsyncMock()) as probe,
        patch("app.services.transcript.transcribe", return_value=("ok", [], None)),
    ):
        await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
            duration_seconds=600,
            max_whisper_duration_s=1800,
        )
    probe.assert_not_called()


async def test_obtain_transcript_long_audio_is_chunked_for_local_whisper(tmp_path):
    """Long audio goes to local Whisper in fixed-length chunks so RAM
    stays flat: timestamps are shifted by the chunk offset, language
    comes from the first chunk that reports one, progress spans the
    whole video, and chunk files are removed afterwards."""
    from app.services import transcript as mod

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")
    chunk_dir_seen: list = []

    results = {
        "chunk_000.flac": ("one", [(1.0, "one")], None),
        "chunk_001.flac": ("two", [(2.0, "two")], "de"),
        "chunk_002.flac": ("three", [(3.0, "three")], "en"),
    }
    progress_seen: list[tuple[float, float]] = []
    languages_passed: list[str | None] = []

    def fake_transcribe(path, model, *, progress=None, cpu_threads=0, language=None):
        languages_passed.append(language)
        if progress is not None:
            progress(5.0, 600.0)
            progress_seen.append((5.0, 600.0))
        return results[path.name]

    messages: list[str] = []

    async def on_progress(msg: str) -> None:
        messages.append(msg)

    with (
        patch.object(mod, "CHUNK_S", 600),
        patch.object(mod, "_PROGRESS_MIN_INTERVAL_S", 0.0),
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch(
            "app.services.transcript.extract_chunk",
            side_effect=_fake_extract(3, chunk_dir_seen),
        ),
        patch("app.services.transcript.transcribe", side_effect=fake_transcribe),
    ):
        text, segments, source, language = await mod.obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
            duration_seconds=1500,
            max_whisper_duration_s=10800,
            progress_cb=on_progress,
        )
        await asyncio.sleep(0)

    assert text == "one two three"
    assert segments == [(1.0, "one"), (602.0, "two"), (1203.0, "three")]
    assert language == "de"
    # Once a chunk detects the language, later chunks are pinned to it
    # so a quiet or accented stretch can't flip the transcript mid-video.
    assert languages_passed == [None, None, "de"]
    assert source.value == "whisper"
    assert not fake_audio.exists()
    assert not chunk_dir_seen[0].exists()
    assert any("20:05 / 25:00" in m for m in messages)


async def test_obtain_transcript_long_audio_is_chunked_for_hosted_whisper(tmp_path):
    from app.services import transcript as mod

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")

    api = AsyncMock(side_effect=[
        ("a", [(0.5, "a")], "en"),
        ("b", [(0.5, "b")], "en"),
    ])
    with (
        patch.object(mod, "CHUNK_S", 600),
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.extract_chunk", side_effect=_fake_extract(2)),
        patch("app.services.transcript.transcribe_via_api", api),
    ):
        text, segments, _, language = await mod.obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="whisper-large-v3",
            whisper_base_url="https://api.groq.com/openai/v1",
            duration_seconds=1100,
        )
    assert text == "a b"
    assert segments == [(0.5, "a"), (600.5, "b")]
    assert language == "en"
    assert api.await_count == 2
    assert [c.kwargs.get("language") for c in api.await_args_list] == [None, "en"]


async def test_obtain_transcript_short_audio_not_chunked(tmp_path):
    from app.services import transcript as mod

    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"")
    with (
        patch.object(mod, "CHUNK_S", 600),
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.extract_chunk", AsyncMock()) as split,
        patch("app.services.transcript.transcribe", return_value=("ok", [], None)),
    ):
        await mod.obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
            duration_seconds=600,
        )
    split.assert_not_called()
