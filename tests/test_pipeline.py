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
            AsyncMock(return_value=("the transcript", TranscriptSource.AUTO_SUBS)),
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
            AsyncMock(return_value=("the transcript", TranscriptSource.AUTO_SUBS)),
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
    """Reindex case: transcript already cached, only summary needs (re)generation."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_transcript(db, "v1", "cached", TranscriptSource.MANUAL_SUBS)
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
            AsyncMock(return_value=("text", TranscriptSource.AUTO_SUBS)),
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
            AsyncMock(return_value=("text", TranscriptSource.AUTO_SUBS)),
        ),
        patch("app.pipeline.summarize", side_effect=fake_summarize),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "vsolo", set_step)

    # None or empty list — both are fine, the summarizer treats them
    # the same way (no header rendered).
    assert captured.get("playlist_context") in (None, [])
