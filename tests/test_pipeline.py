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
