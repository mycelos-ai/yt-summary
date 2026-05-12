import asyncio
from pathlib import Path

import aiosqlite

from app.config import Config
from app.models import TranscriptSource
from app.repos import tts_jobs as r
from app.repos import videos as videos_repo
from app.tts_worker import TtsWorker


async def _seed_video_with_summary(
    db: aiosqlite.Connection,
    *,
    video_id: str = "abc",
    summary_text: str = "Hello.",
    source_lang: str = "en",
    summary_lang: str = "en",
) -> None:
    await videos_repo.upsert_metadata(
        db,
        video_id=video_id,
        url=f"https://yt/{video_id}",
        title="T",
        description="",
        thumbnail_path=None,
        duration_seconds=60,
    )
    await videos_repo.set_transcript(
        db,
        video_id,
        "Some transcript.",
        TranscriptSource.AUTO_SUBS,
        language=source_lang,
    )
    await videos_repo.set_summary(
        db, video_id, summary_text, "gpt-4o", language=summary_lang,
    )


async def _drain(db: aiosqlite.Connection, worker: TtsWorker, video_id: str) -> None:
    task = asyncio.create_task(worker.run())
    try:
        for _ in range(80):
            await asyncio.sleep(0.05)
            rows = await r.list_for_video(db, video_id)
            if rows and rows[0].status in ("done", "failed"):
                break
    finally:
        worker.stop()
        await task


async def test_worker_runs_queued_job_to_done(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """End-to-end with mocked translator + renderer: a queued job
    transitions to done and writes the expected audio_path."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed_video_with_summary(
        db, summary_text="Hello.", source_lang="en", summary_lang="en",
    )
    await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")

    translator_calls: list[str] = []

    async def fake_translate(
        text: str, *, source_language: str, target_language: str, complete, **kw
    ) -> str:
        translator_calls.append(text)
        return "Hallo."

    renders: list[tuple[str, Path, Path]] = []

    async def fake_render(text: str, voice_file: Path, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKE-MP3")
        renders.append((text, voice_file, out_path))

    async def fake_ensure_voice(language: str, voice: str, quality: str) -> Path:
        return tmp_path / "fake.onnx"

    worker = TtsWorker(
        db=db,
        config=cfg,
        translate=fake_translate,
        render_text_to_mp3=fake_render,
        ensure_voice=fake_ensure_voice,
        poll_interval=0.02,
    )
    await _drain(db, worker, "abc")

    rows = await r.list_for_video(db, "abc")
    assert rows, "expected at least one tts_job row"
    job = rows[0]
    assert job.status == "done", f"job failed: {job.error}"
    assert job.audio_path is not None
    assert job.translated_text == "Hallo."
    assert translator_calls == ["Hello."]
    assert len(renders) == 1
    # Rendered with the translated text.
    rendered_text, _, rendered_out = renders[0]
    assert rendered_text == "Hallo."
    # Path layout: tts-audio/<video_id>/<source>-<target>-<voice>-<quality>.mp3
    expected_rel = Path("tts-audio") / "abc" / "summary-de-thorsten-medium.mp3"
    assert Path(job.audio_path) == expected_rel
    # The file on disk lives under config.data_dir.
    assert rendered_out == cfg.data_dir / expected_rel
    assert rendered_out.exists()


async def test_worker_skips_translation_when_languages_match(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """source_language == target_language → renderer is called with
    the original text, translator is NOT called at all."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed_video_with_summary(
        db, summary_text="Hallo Welt.", source_lang="de", summary_lang="de",
    )
    await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")

    translator_calls: list[str] = []

    async def fake_translate(
        text: str, *, source_language: str, target_language: str, complete, **kw
    ) -> str:
        translator_calls.append(text)
        return "SHOULD NOT BE CALLED"

    renders: list[str] = []

    async def fake_render(text: str, voice_file: Path, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKE-MP3")
        renders.append(text)

    async def fake_ensure_voice(language: str, voice: str, quality: str) -> Path:
        return tmp_path / "fake.onnx"

    worker = TtsWorker(
        db=db,
        config=cfg,
        translate=fake_translate,
        render_text_to_mp3=fake_render,
        ensure_voice=fake_ensure_voice,
        poll_interval=0.02,
    )
    await _drain(db, worker, "abc")

    rows = await r.list_for_video(db, "abc")
    assert rows
    job = rows[0]
    assert job.status == "done", f"job failed: {job.error}"
    assert translator_calls == []
    assert job.translated_text is None
    assert renders == ["Hallo Welt."]
