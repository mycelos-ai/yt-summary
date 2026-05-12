from unittest.mock import AsyncMock, patch

from app.config import Config
from app.models import TranscriptSource
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo


async def test_pipeline_writes_transcript_and_summary(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="v1", url="https://youtu.be/v1", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "key")
    await settings_repo.set(db, "whisper_model", "small")

    steps: list[str] = []

    async def set_step(s: str) -> None:
        steps.append(s)

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("the transcript", [], TranscriptSource.AUTO_SUBS, None)),
        ),
        patch(
            "app.pipeline.summarize",
            AsyncMock(return_value="THE SUMMARY"),
        ),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "v1", set_step)

    v = await videos_repo.get(db, "v1")
    assert v is not None
    assert v.transcript == "the transcript"
    assert v.transcript_source is TranscriptSource.AUTO_SUBS
    assert v.summary == "THE SUMMARY"
    assert v.summary_model == "openai/gpt-4o"
    assert any("transcript" in s.lower() for s in steps)
    assert any("summary" in s.lower() or "summari" in s.lower() for s in steps)


async def test_pipeline_transcript_only_when_llm_unset(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )

    async def set_step(s: str) -> None:
        pass

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("the transcript", [], TranscriptSource.AUTO_SUBS, None)),
        ),
        patch("app.pipeline.summarize") as summarize_mock,
    ):
        from app.pipeline import process_video
        await process_video(db, config, "v1", set_step)

    v = await videos_repo.get(db, "v1")
    assert v is not None
    assert v.transcript == "the transcript"
    assert v.summary is None
    summarize_mock.assert_not_called()


async def test_pipeline_skips_transcript_when_already_present(db, tmp_path):
    """Reindex case: transcript + segments already cached, only summary
    needs (re)generation. The presence of segments_json is what tells
    the pipeline the transcript is in the new format and doesn't need
    re-fetching."""
    import json as _json
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_transcript(
        db, "v1", "cached",
        TranscriptSource.MANUAL_SUBS,
        segments_json=_json.dumps([{"start": 0.0, "text": "cached"}]),
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "k")

    async def set_step(s: str) -> None:
        pass

    with (
        patch("app.pipeline.obtain_transcript") as obtain_mock,
        patch("app.pipeline.summarize", AsyncMock(return_value="NEW SUMMARY")),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "v1", set_step)

    obtain_mock.assert_not_called()
    v = await videos_repo.get(db, "v1")
    assert v is not None
    assert v.summary == "NEW SUMMARY"


async def test_pipeline_uses_reader_for_web_kind(db, tmp_path):
    from app.services.reader import ArticleMetadata
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    from app.models import VideoKind
    await videos_repo.upsert_metadata(
        db, video_id="web-cafe1234567",
        url="https://example.com/post",
        title="Web post", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.WEB,
    )

    fake_article = ArticleMetadata(
        url="https://example.com/post",
        title="Web post",
        description="",
        body="The article body.",
        thumbnail_url=None,
    )

    async def set_step(s: str) -> None:
        pass

    with (
        patch("app.pipeline.fetch_article", AsyncMock(return_value=fake_article)),
        patch("app.pipeline.obtain_transcript") as obtain_mock,
        patch("app.pipeline.summarize", AsyncMock(return_value="THE SUMMARY")),
    ):
        from app.pipeline import process_video
        await settings_repo.set(db, "llm_model", "openai/gpt-4o")
        await settings_repo.set(db, "llm_api_key", "k")
        await process_video(db, config, "web-cafe1234567", set_step)

    obtain_mock.assert_not_called()
    v = await videos_repo.get(db, "web-cafe1234567")
    assert v is not None
    assert v.transcript == "The article body."
    assert v.transcript_source is not None
    assert v.transcript_source.value == "web"
    assert v.summary == "THE SUMMARY"


async def test_pipeline_passes_playlist_context_to_summarizer(db, tmp_path):
    """When a video is linked to one or more playlists, the pipeline
    must surface those playlist names to summarize() so the LLM can
    use them as topic hints."""
    from app.repos import playlists as playlists_repo

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="vctx", url="https://youtu.be/vctx", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await playlists_repo.create(
        db, playlist_id="PL_AI", user_id=1, url="u",
        title="AI", description="", thumbnail_path=None,
    )
    await playlists_repo.create(
        db, playlist_id="PL_LF", user_id=1, url="u",
        title="Long-form interviews", description="", thumbnail_path=None,
    )
    await playlists_repo.link_video(db, "PL_AI", "vctx")
    await playlists_repo.link_video(db, "PL_LF", "vctx")

    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "key")
    await settings_repo.set(db, "whisper_model", "small")

    captured: dict = {}

    async def fake_summarize(**kwargs):
        captured.update(kwargs)
        return "S"

    async def set_step(_: str) -> None:
        return None

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("text", [], TranscriptSource.AUTO_SUBS, None)),
        ),
        patch("app.pipeline.summarize", side_effect=fake_summarize),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vctx", set_step)

    assert "playlist_context" in captured
    ctx = captured["playlist_context"]
    assert ctx is not None
    assert set(ctx) == {"AI", "Long-form interviews"}


async def test_pipeline_no_playlist_context_when_video_unaffiliated(db, tmp_path):
    """A video that's been submitted directly (no playlist) should
    pass playlist_context=None so the summarizer's user message
    doesn't render an empty section."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="vsolo", url="https://youtu.be/vsolo", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "key")
    await settings_repo.set(db, "whisper_model", "small")

    captured: dict = {}

    async def fake_summarize(**kwargs):
        captured.update(kwargs)
        return "S"

    async def set_step(_: str) -> None:
        return None

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("text", [], TranscriptSource.AUTO_SUBS, None)),
        ),
        patch("app.pipeline.summarize", side_effect=fake_summarize),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vsolo", set_step)

    # None or empty list — both are fine, the summarizer treats them
    # the same way (no header rendered).
    assert captured.get("playlist_context") in (None, [])


async def test_pipeline_refetches_when_segments_missing(db, tmp_path):
    """Self-healing: a YouTube video that has plain transcript text
    but no transcript_segments JSON (legacy data from before the
    timestamps feature) should trigger a fresh fetch so segments get
    populated. Without this the user would see plain wall-of-text
    forever even after re-summarizing."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="vlegacy", url="https://youtu.be/vlegacy", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    # Simulate legacy state: transcript present, segments missing.
    await videos_repo.set_transcript(
        db, "vlegacy",
        "old plain text",
        TranscriptSource.AUTO_SUBS,
        segments_json=None,
    )

    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "k")
    await settings_repo.set(db, "whisper_model", "small")

    fresh_segments = [(0.0, "fresh"), (5.0, "and timestamped")]
    obtain_mock = AsyncMock(
        return_value=(
            "fresh\nand timestamped",
            fresh_segments,
            TranscriptSource.AUTO_SUBS,
            None,
        )
    )

    async def set_step(_: str) -> None:
        return None

    with (
        patch("app.pipeline.obtain_transcript", obtain_mock),
        patch("app.pipeline.summarize", AsyncMock(return_value="S")),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vlegacy", set_step)

    assert obtain_mock.called, "should have re-fetched the transcript"
    v = await videos_repo.get(db, "vlegacy")
    assert v is not None
    assert v.transcript_segments is not None
    assert "fresh" in (v.transcript or "")


async def test_pipeline_passes_segments_to_summarizer_for_youtube(db, tmp_path):
    """YouTube videos with cached segments must surface them to
    summarize() so the LLM can pick real timestamps."""
    import json as _json

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="vseg", url="https://youtu.be/vseg", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    segs = [
        {"start": 0.0, "text": "intro"},
        {"start": 30.0, "text": "middle"},
        {"start": 90.0, "text": "punchline"},
    ]
    await videos_repo.set_transcript(
        db, "vseg", "intro middle punchline",
        TranscriptSource.AUTO_SUBS,
        segments_json=_json.dumps(segs),
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "k")

    captured: dict = {}

    async def fake_summarize(**kwargs):
        captured.update(kwargs)
        return "OUT"

    async def set_step(_: str) -> None:
        return None

    with (
        patch("app.pipeline.obtain_transcript") as obtain_mock,
        patch("app.pipeline.summarize", side_effect=fake_summarize),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vseg", set_step)

    obtain_mock.assert_not_called()
    assert captured.get("transcript_segments") is not None
    passed = captured["transcript_segments"]
    # Same number of segments, same starts (compare on starts only —
    # implementation may pass them through as-is or normalised).
    starts = sorted(s["start"] for s in passed)
    assert starts == [0.0, 30.0, 90.0]


async def test_pipeline_does_not_pass_segments_for_web_kind(db, tmp_path):
    """Web articles have no concept of time. The pipeline must NOT
    surface segments (even legacy / accidental ones) to summarize for
    web kind, so the timestamp instruction stays out of the prompt."""
    from app.services.reader import ArticleMetadata

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    from app.models import VideoKind
    await videos_repo.upsert_metadata(
        db, video_id="web-aaaaaaa1111",
        url="https://example.com/x",
        title="Web post", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.WEB,
    )

    article = ArticleMetadata(
        url="https://example.com/x", title="Web post", description="",
        body="The article body.", thumbnail_url=None,
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "k")

    captured: dict = {}

    async def fake_summarize(**kwargs):
        captured.update(kwargs)
        return "OUT"

    async def set_step(_: str) -> None:
        return None

    with (
        patch("app.pipeline.fetch_article", AsyncMock(return_value=article)),
        patch("app.pipeline.summarize", side_effect=fake_summarize),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "web-aaaaaaa1111", set_step)

    assert captured.get("transcript_segments") in (None, [])


async def test_pipeline_reports_timestamp_verification_step(db, tmp_path):
    """After summarizing a YouTube video with segments, the pipeline
    surfaces a 'timestamps verified' progress step including counts."""
    import json as _json

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="vverify", url="https://youtu.be/vverify", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    segs = [{"start": 0.0, "text": "x"}, {"start": 60.0, "text": "y"}]
    await videos_repo.set_transcript(
        db, "vverify", "x y",
        TranscriptSource.AUTO_SUBS,
        segments_json=_json.dumps(segs),
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "k")

    steps: list[str] = []

    async def set_step(s: str) -> None:
        steps.append(s)

    summary = "Look at [00:00](#t=0) and [01:00](#t=60)."
    with (
        patch("app.pipeline.obtain_transcript"),
        patch("app.pipeline.summarize", AsyncMock(return_value=summary)),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vverify", set_step)

    # A step mentioning timestamp verification should have been emitted
    assert any("timestamp" in s.lower() for s in steps)


async def test_pipeline_skips_fetch_when_segments_already_present(db, tmp_path):
    """If transcript AND segments are both already stored, the
    pipeline should not re-fetch — re-summarize stays cheap."""
    import json as _json
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="vfresh", url="https://youtu.be/vfresh", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_transcript(
        db, "vfresh",
        "already there",
        TranscriptSource.AUTO_SUBS,
        segments_json=_json.dumps([{"start": 0.0, "text": "already there"}]),
    )

    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "k")
    await settings_repo.set(db, "whisper_model", "small")

    obtain_mock = AsyncMock()

    async def set_step(_: str) -> None:
        return None

    with (
        patch("app.pipeline.obtain_transcript", obtain_mock),
        patch("app.pipeline.summarize", AsyncMock(return_value="S")),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vfresh", set_step)

    assert not obtain_mock.called, "transcript was already complete; no fetch"


async def test_pipeline_writes_source_language_on_video(db, tmp_path):
    """A fully processed video must have source_language stamped from
    the transcript path (Whisper / VTT language signal)."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="vlang", url="https://youtu.be/vlang", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "key")
    await settings_repo.set(db, "whisper_model", "small")

    async def set_step(_: str) -> None:
        return None

    # obtain_transcript now returns a 4-tuple including the language.
    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("hello world", [], TranscriptSource.AUTO_SUBS, "en")),
        ),
        patch(
            "app.pipeline.summarize",
            AsyncMock(return_value="THE SUMMARY"),
        ),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vlang", set_step)

    v = await videos_repo.get(db, "vlang")
    assert v is not None
    assert v.source_language == "en"
    assert v.transcript_language == "en"


async def test_pipeline_writes_summary_language_matching_setting(db, tmp_path):
    """summary_language='auto' → summary_language column == source_language.
    summary_language='en' (explicit) → column == 'en' regardless of source."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    # Case 1: auto → falls back to source language ("de" from transcript).
    await videos_repo.upsert_metadata(
        db, video_id="vauto", url="https://youtu.be/vauto", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "k")
    await settings_repo.set(db, "summary_language", "auto")

    async def set_step(_: str) -> None:
        return None

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("hallo welt", [], TranscriptSource.AUTO_SUBS, "de")),
        ),
        patch("app.pipeline.summarize", AsyncMock(return_value="ZUSAMMENFASSUNG")),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vauto", set_step)

    v = await videos_repo.get(db, "vauto")
    assert v is not None
    assert v.source_language == "de"
    assert v.summary_language == "de"

    # Case 2: explicit "en" → column is "en" even though source is "de".
    await videos_repo.upsert_metadata(
        db, video_id="vfixed", url="https://youtu.be/vfixed", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await settings_repo.set(db, "summary_language", "en")

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("hallo welt", [], TranscriptSource.AUTO_SUBS, "de")),
        ),
        patch("app.pipeline.summarize", AsyncMock(return_value="THE SUMMARY")),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vfixed", set_step)

    v = await videos_repo.get(db, "vfixed")
    assert v is not None
    assert v.source_language == "de"
    assert v.summary_language == "en"


async def test_pipeline_preserves_detected_source_lang_when_summary_lang_explicit(
    db, tmp_path,
):
    """Regression: when `summary_language` is set to a concrete value
    (e.g. "en") AND the transcript path surfaced no language, the
    LLM-detect fallback's result must be persisted as source_language
    even though summary_language ends up as the explicit setting.

    Before the fix, `set_summary` was called with the SUMMARY's
    language ("en"); its COALESCE-on-summary backfill then poisoned
    source_language with "en" too, hiding the detected "fr".
    """
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="vmix",
        url="https://youtu.be/vmix", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "k")
    await settings_repo.set(db, "summary_language", "en")  # explicit

    async def set_step(_: str) -> None:
        return None

    async def fake_detect(_text, *, complete):
        return "fr"

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=(
                "le contenu", [], TranscriptSource.AUTO_SUBS, None,
            )),
        ),
        patch("app.pipeline.summarize", AsyncMock(return_value="THE SUMMARY")),
        patch("app.pipeline.detect_language", AsyncMock(side_effect=fake_detect)),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vmix", set_step)

    v = await videos_repo.get(db, "vmix")
    assert v is not None
    assert v.source_language == "fr", (
        "detected source language must survive even when summary_language "
        f"is explicit; got {v.source_language!r}"
    )
    assert v.summary_language == "en"


async def test_pipeline_falls_back_to_llm_language_detect_when_no_signal(db, tmp_path):
    """When both Whisper and VTT come back without a language, the
    pipeline asks the language_detect helper for one based on the
    summary text and stamps that as source_language + summary_language."""
    from app.services.reader import ArticleMetadata
    from app.models import VideoKind

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="web-bbbbbbbbbbb",
        url="https://example.com/post",
        title="Post", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.WEB,
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "k")
    await settings_repo.set(db, "summary_language", "auto")

    fake_article = ArticleMetadata(
        url="https://example.com/post", title="Post", description="",
        body="some plain content", thumbnail_url=None,
    )

    async def set_step(_: str) -> None:
        return None

    async def fake_detect(_text, *, complete):
        return "fr"

    with (
        patch("app.pipeline.fetch_article", AsyncMock(return_value=fake_article)),
        patch("app.pipeline.summarize", AsyncMock(return_value="LE RÉSUMÉ")),
        patch("app.pipeline.detect_language", AsyncMock(side_effect=fake_detect)),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "web-bbbbbbbbbbb", set_step)

    v = await videos_repo.get(db, "web-bbbbbbbbbbb")
    assert v is not None
    assert v.source_language == "fr"
    assert v.summary_language == "fr"
