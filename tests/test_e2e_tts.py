"""End-to-end smoke test for the TTS pipeline.

Exercises every layer except Hugging Face: real Piper synth, real
ffmpeg, real ffprobe, real SQLite. The voice file is provided by the
session-scoped ``amy_low_voice`` fixture (cached under
``~/.cache/yt-summary-test-voices/``) and staged into
``cfg.tts_voices_dir`` so a stubbed ``ensure_voice`` returns its
path without touching the network.

To keep the test deterministic and avoid an LLM call we set the
seeded summary's language to ``en_US`` (matching the requested
``target_language``); the worker then skips translation entirely
and goes straight to render. The translator stub raises if it's
ever called, which guards the skip path against regressions.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import aiosqlite

from app.config import Config
from app.repos import tts_jobs as tts_jobs_repo
from app.repos import videos as videos_repo
from app.services.tts_render import render_text_to_mp3
from app.tts_worker import TtsWorker


async def test_end_to_end_render_for_summary(
    db: aiosqlite.Connection, tmp_path: Path, amy_low_voice: Path
) -> None:
    """Drives the full worker pipeline against the cached Piper voice
    fixture: enqueue → claim → skip-translate → ensure_voice → real
    Piper render → ffprobe → done. Asserts the MP3 lands on disk, has
    plausible size, and the row carries a positive duration."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()

    # Stage the cached test voice under the canonical filename so
    # ensure_voice can find it offline.
    onnx_json = amy_low_voice.with_suffix(amy_low_voice.suffix + ".json")
    shutil.copy(amy_low_voice, cfg.tts_voices_dir / amy_low_voice.name)
    shutil.copy(onnx_json, cfg.tts_voices_dir / onnx_json.name)

    # Seed a video with summary_language=en_US so the worker skips
    # translation (source == target) and runs straight to render.
    await videos_repo.upsert_metadata(
        db,
        video_id="abc",
        url="https://yt/abc",
        title="T",
        description="",
        thumbnail_path=None,
        duration_seconds=60,
    )
    await videos_repo.set_summary(
        db,
        "abc",
        "Hello world. This is a smoke test of text to speech rendering.",
        "gpt-4o",
        language="en_US",
    )

    async def noop_translate(*args: object, **kwargs: object) -> str:
        raise AssertionError(
            "translate should NOT be called when source == target language"
        )

    async def real_ensure_voice(language: str, voice: str, quality: str) -> Path:
        # Mirrors the on-disk layout the downloader would produce
        # without actually fetching anything: language/voice/quality
        # collapse to the canonical Piper filename.
        return cfg.tts_voices_dir / f"{language}-{voice}-{quality}.onnx"

    worker = TtsWorker(
        db=db,
        config=cfg,
        translate=noop_translate,
        render_text_to_mp3=render_text_to_mp3,
        ensure_voice=real_ensure_voice,
        poll_interval=0.05,
    )

    await tts_jobs_repo.enqueue(db, "abc", "summary", "en_US", "amy", "low")

    task = asyncio.create_task(worker.run())
    try:
        for _ in range(300):
            await asyncio.sleep(0.1)
            rows = await tts_jobs_repo.list_for_video(db, "abc")
            if rows and rows[0].status in ("done", "failed"):
                break
    finally:
        worker.stop()
        await task

    rows = await tts_jobs_repo.list_for_video(db, "abc")
    assert rows, "expected one tts_job row"
    job = rows[0]
    assert job.status == "done", f"job failed: {job.error}"
    # Translation was skipped — no translated_text stored.
    assert job.translated_text is None
    # Audio file lands on disk under the data dir.
    assert job.audio_path is not None
    mp3 = cfg.data_dir / job.audio_path
    assert mp3.exists()
    # Real Piper output is well over 1 KB for a 12-word sentence.
    assert mp3.stat().st_size > 1000
    # MP3 magic: ID3 tag header or MPEG sync byte.
    head = mp3.read_bytes()[:3]
    assert head[:3] == b"ID3" or head[0] == 0xFF
    # ffprobe should have populated a positive duration.
    assert job.duration_seconds is not None
    assert job.duration_seconds > 0
