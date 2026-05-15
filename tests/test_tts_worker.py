import asyncio
from pathlib import Path

import aiosqlite
import pytest

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

    renders: list[tuple[list[str], Path, Path]] = []

    async def fake_render_chunks(
        chunks: list[str],
        voice_file: Path,
        out_path: Path,
        length_scale: float | None = None,
        progress=None,
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKE-MP3")
        renders.append((chunks, voice_file, out_path))
        if progress is not None:
            for i in range(len(chunks)):
                progress(i + 1, len(chunks))

    async def fake_ensure_voice(language: str, voice: str, quality: str) -> Path:
        return tmp_path / "fake.onnx"

    worker = TtsWorker(
        db=db,
        config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render_chunks,
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
    # Rendered with the translated text (as a single-chunk list, since
    # "Hallo." has no second sentence to split on).
    rendered_chunks, _, rendered_out = renders[0]
    assert rendered_chunks == ["Hallo."]
    # Path layout: tts-audio/<video_id>/<source>-<target>-<voice>-<quality>.mp3
    expected_rel = Path("tts-audio") / "abc" / "summary-de-thorsten-medium.mp3"
    assert Path(job.audio_path) == expected_rel
    # The file on disk lives under config.data_dir.
    assert rendered_out == cfg.data_dir / expected_rel
    assert rendered_out.exists()


async def test_worker_passes_length_scale_from_settings(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """The worker must read ``default_tts_length_scale`` from settings
    and forward it as a kwarg to the render function so per-install
    speech-speed tuning actually reaches Piper."""
    from app.repos import settings as settings_repo

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed_video_with_summary(
        db, summary_text="Hello.", source_lang="de", summary_lang="de",
    )
    await settings_repo.set(db, "default_tts_length_scale", "1.20")
    await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")

    async def fake_translate(
        text: str, *, source_language: str, target_language: str, complete, **kw
    ) -> str:
        return text

    render_kwargs: list[dict] = []

    async def fake_render_chunks(
        chunks: list[str],
        voice_file: Path,
        out_path: Path,
        length_scale: float | None = None,
        progress=None,
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKE-MP3")
        render_kwargs.append({
            "chunks": chunks, "voice_file": voice_file,
            "out_path": out_path, "length_scale": length_scale,
        })

    async def fake_ensure_voice(language: str, voice: str, quality: str) -> Path:
        return tmp_path / "fake.onnx"

    worker = TtsWorker(
        db=db,
        config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render_chunks,
        ensure_voice=fake_ensure_voice,
        poll_interval=0.02,
    )
    await _drain(db, worker, "abc")

    rows = await r.list_for_video(db, "abc")
    assert rows
    job = rows[0]
    assert job.status == "done", f"job failed: {job.error}"
    assert len(render_kwargs) == 1
    assert render_kwargs[0]["length_scale"] == 1.2


async def test_worker_emits_per_chunk_render_progress_steps(
    db: aiosqlite.Connection, tmp_path: Path,
) -> None:
    """The worker translates per-chunk render progress callbacks into
    ``set_step`` calls of the form ``"rendering audio chunk N/M"``.

    Seeds a multi-sentence summary so the splitter produces multiple
    chunks, then verifies the final ``step`` value persisted on the
    job row matches the last expected chunk-progress string.
    """
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    # Three sentences → splitter produces 3 chunks at default
    # sentences_per_chunk=25 (each chunk holds <= 25 sentences, but
    # since the default is 25 and we only have 3, they'd collapse to
    # one chunk — so we explicitly use a smaller chunk grouping in the
    # fake by NOT relying on the default; instead we craft enough
    # sentences with the default to land on multiple chunks).
    # Simpler: 3 sentences with sentences_per_chunk=1 — but the worker
    # uses the default. Easier path: 26 sentences → 2 chunks of 25+1.
    sentences = " ".join(
        f"Sentence number {i}." for i in range(1, 27)
    )
    await _seed_video_with_summary(
        db, summary_text=sentences, source_lang="en", summary_lang="en",
    )
    await r.enqueue(db, "abc", "summary", "en", "amy", "low")

    async def fake_translate(
        text: str, *, source_language: str, target_language: str, complete, **kw
    ) -> str:
        return text

    progress_calls: list[tuple[int, int]] = []

    async def fake_render_chunks(
        chunks: list[str],
        voice_file: Path,
        out_path: Path,
        length_scale: float | None = None,
        progress=None,
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKE-MP3")
        if progress is not None:
            for i in range(len(chunks)):
                progress(i + 1, len(chunks))
                # Yield to the loop so the run_coroutine_threadsafe
                # scheduled set_step actually runs before we move on.
                await asyncio.sleep(0)
        progress_calls.extend([(i + 1, len(chunks)) for i in range(len(chunks))])

    async def fake_ensure_voice(language: str, voice: str, quality: str) -> Path:
        return tmp_path / "fake.onnx"

    worker = TtsWorker(
        db=db,
        config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render_chunks,
        ensure_voice=fake_ensure_voice,
        poll_interval=0.02,
    )
    await _drain(db, worker, "abc")

    rows = await r.list_for_video(db, "abc")
    assert rows
    job = rows[0]
    assert job.status == "done", f"job failed: {job.error}"
    # The splitter produced 2 chunks (26 sentences at 25/chunk → 25 + 1).
    assert progress_calls == [(1, 2), (2, 2)]
    # Final step persisted on the row is the last chunk-progress string.
    assert job.step == "rendering audio chunk 2/2"


async def test_worker_persists_translation_before_render(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """The worker MUST write translated_text to the DB before the
    render step begins, so a container crash mid-render doesn't waste
    the LLM translation work. We verify this by having the fake render
    read the DB and assert the column is already populated by the time
    it runs."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed_video_with_summary(
        db, summary_text="Hello.", source_lang="en", summary_lang="en",
    )
    await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")

    async def fake_translate(
        text: str, *, source_language: str, target_language: str, complete, **kw
    ) -> str:
        return "Hallo."

    translated_at_render_time: list[str | None] = []

    async def fake_render_chunks(
        chunks: list[str],
        voice_file: Path,
        out_path: Path,
        length_scale: float | None = None,
        progress=None,
    ) -> None:
        # Capture the persisted translated_text at the moment render
        # starts — proves persistence happened before render, not at
        # job-completion time.
        rows = await r.list_for_video(db, "abc")
        translated_at_render_time.append(
            rows[0].translated_text if rows else None
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKE-MP3")

    async def fake_ensure_voice(language: str, voice: str, quality: str) -> Path:
        return tmp_path / "fake.onnx"

    worker = TtsWorker(
        db=db,
        config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render_chunks,
        ensure_voice=fake_ensure_voice,
        poll_interval=0.02,
    )
    await _drain(db, worker, "abc")

    rows = await r.list_for_video(db, "abc")
    assert rows
    job = rows[0]
    assert job.status == "done", f"job failed: {job.error}"
    # The render-time snapshot shows translated_text was already there.
    assert translated_at_render_time == ["Hallo."]
    # And of course the final row still has it.
    assert job.translated_text == "Hallo."


async def test_worker_skips_translation_when_already_persisted(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """Crash-resume case: when claim_next returns a job that already
    has translated_text set (because a prior run translated then
    crashed), the worker MUST skip translate() and use the cached text
    for rendering. We assert this by injecting a fake_translate that
    raises if invoked."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed_video_with_summary(
        db, summary_text="Hello.", source_lang="en", summary_lang="en",
    )
    job = await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    # Simulate the prior crashed run having persisted a translation.
    await r.set_translated_text(db, job.id, "Hallo aus dem Cache.")

    async def fake_translate(
        text: str, *, source_language: str, target_language: str, complete, **kw
    ) -> str:
        raise AssertionError(
            "translate() must not be called when translated_text is cached"
        )

    renders: list[list[str]] = []

    async def fake_render_chunks(
        chunks: list[str],
        voice_file: Path,
        out_path: Path,
        length_scale: float | None = None,
        progress=None,
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKE-MP3")
        renders.append(chunks)

    async def fake_ensure_voice(language: str, voice: str, quality: str) -> Path:
        return tmp_path / "fake.onnx"

    worker = TtsWorker(
        db=db,
        config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render_chunks,
        ensure_voice=fake_ensure_voice,
        poll_interval=0.02,
    )
    await _drain(db, worker, "abc")

    rows = await r.list_for_video(db, "abc")
    assert rows
    finished = rows[0]
    assert finished.status == "done", f"job failed: {finished.error}"
    # The cached translation was used for rendering — not the original
    # source text.
    assert renders == [["Hallo aus dem Cache."]]
    # And it survived into the final row.
    assert finished.translated_text == "Hallo aus dem Cache."


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

    renders: list[list[str]] = []

    async def fake_render_chunks(
        chunks: list[str],
        voice_file: Path,
        out_path: Path,
        length_scale: float | None = None,
        progress=None,
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKE-MP3")
        renders.append(chunks)

    async def fake_ensure_voice(language: str, voice: str, quality: str) -> Path:
        return tmp_path / "fake.onnx"

    worker = TtsWorker(
        db=db,
        config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render_chunks,
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
    assert renders == [["Hallo Welt."]]


async def test_tts_worker_touches_heartbeat(
    db: aiosqlite.Connection, tmp_path: Path,
) -> None:
    """Same contract as Worker: the loop must heartbeat each iteration."""
    from app.services.heartbeat import HeartbeatRegistry

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    hb = HeartbeatRegistry()

    async def fake_translate(*a, **kw): return ""
    async def fake_render(*a, **kw): return None
    async def fake_voice(*a, **kw): return tmp_path / "voice.onnx"

    worker = TtsWorker(
        db=db, config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render,
        ensure_voice=fake_voice,
        poll_interval=0.05,
        heartbeat=hb,
    )
    task = asyncio.create_task(worker.run())
    for _ in range(20):
        await asyncio.sleep(0.05)
        if "tts_worker" in hb.snapshot():
            break
    worker.stop()
    await task

    assert "tts_worker" in hb.snapshot()


def test_tts_worker_poll_interval_seconds_property():
    from app.services.heartbeat import HeartbeatRegistry
    async def _noop(*a, **kw): return None
    worker = TtsWorker(
        db=None, config=None,
        translate=_noop, render_chunks_to_mp3=_noop, ensure_voice=_noop,
        poll_interval=0.25, heartbeat=HeartbeatRegistry(),
    )
    assert worker.poll_interval_seconds == 0.25


class _SneakyTtsError(BaseException):
    """Test fixture: BaseException subclass that the inner `except
    Exception` doesn't catch. Used to verify the outer crash-survival
    wrapper in TtsWorker.run() catches it."""


async def _seed_video_with_summary_short(
    db: aiosqlite.Connection, vid: str = "v_tts",
) -> None:
    """Minimal seed for a tts_jobs queue test: video + transcript +
    summary so the worker has text to render. Mirrors the helper at
    the top of this file."""
    await videos_repo.upsert_metadata(
        db, video_id=vid, url=f"https://yt/{vid}", title="T",
        description="", thumbnail_path=None, duration_seconds=10,
    )
    await videos_repo.set_transcript(
        db, vid, "x.", TranscriptSource.AUTO_SUBS, language="en",
    )
    await videos_repo.set_summary(db, vid, "x.", "gpt-4o", language="en")


async def test_tts_worker_survives_runtime_error_in_process(
    db: aiosqlite.Connection, tmp_path,
):
    """First _process raises RuntimeError, second succeeds (mocked)."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed_video_with_summary_short(db, "vt1")
    await _seed_video_with_summary_short(db, "vt2")
    j1 = await r.enqueue(db, "vt1", "summary", "en", "amy", "low")
    j2 = await r.enqueue(db, "vt2", "summary", "en", "amy", "low")

    calls = {"n": 0}

    async def fake_translate(text, **kw):
        return text

    async def fake_render(chunks, voice_file, out_path, **kw):
        # Touch the output file so complete() doesn't trip on missing.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"")

    async def fake_voice(*a, **kw):
        return tmp_path / "voice.onnx"

    worker = TtsWorker(
        db=db, config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render,
        ensure_voice=fake_voice,
        poll_interval=0.05,
    )

    real_process = worker._process

    async def flaky_process(job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient")
        return await real_process(job)

    worker._process = flaky_process

    task = asyncio.create_task(worker.run())
    for _ in range(120):
        await asyncio.sleep(0.05)
        f1 = await r.get(db, j1.id)
        f2 = await r.get(db, j2.id)
        if f1 and f2 and f1.status in ("done", "failed") and f2.status in ("done", "failed"):
            break
    worker.stop()
    await task

    f1 = await r.get(db, j1.id)
    f2 = await r.get(db, j2.id)
    # j1 was failed by the inner except. j2 was processed successfully.
    assert f1 is not None and f1.status == "failed"
    assert f2 is not None and f2.status == "done"


async def test_tts_worker_survives_base_exception(
    db: aiosqlite.Connection, tmp_path,
):
    """A BaseException subclass from _process must NOT kill the loop."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed_video_with_summary_short(db, "vt1")
    await r.enqueue(db, "vt1", "summary", "en", "amy", "low")

    seen = {"crashed": False}

    async def fake_translate(text, **kw): return text
    async def fake_render(*a, **kw): return None
    async def fake_voice(*a, **kw): return tmp_path / "voice.onnx"

    worker = TtsWorker(
        db=db, config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render,
        ensure_voice=fake_voice,
        poll_interval=0.05,
    )

    async def boom(job):
        seen["crashed"] = True
        raise _SneakyTtsError("not an Exception subclass")

    worker._process = boom

    task = asyncio.create_task(worker.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if seen["crashed"]:
            break
    assert seen["crashed"], "_process was never invoked"
    assert not task.done(), (
        "TtsWorker died on BaseException — outer except is missing"
    )
    worker.stop()
    await task


async def test_tts_worker_survives_claim_next_db_failure(
    db: aiosqlite.Connection, tmp_path, monkeypatch,
):
    """A locked-DB error from tts_jobs_repo.claim_next must be caught
    by the outer wrapper, not crash the loop."""
    from app.repos import tts_jobs as tts_jobs_repo_mod

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed_video_with_summary_short(db, "vt1")
    j = await r.enqueue(db, "vt1", "summary", "en", "amy", "low")

    real_claim = tts_jobs_repo_mod.claim_next
    calls = {"n": 0}

    async def flaky_claim(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise aiosqlite.OperationalError("database is locked")
        return await real_claim(conn)

    monkeypatch.setattr(tts_jobs_repo_mod, "claim_next", flaky_claim)

    async def fake_translate(text, **kw): return text
    async def fake_render(chunks, voice_file, out_path, **kw):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"")
    async def fake_voice(*a, **kw): return tmp_path / "voice.onnx"

    worker = TtsWorker(
        db=db, config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render,
        ensure_voice=fake_voice,
        poll_interval=0.05,
    )
    task = asyncio.create_task(worker.run())
    for _ in range(160):
        await asyncio.sleep(0.05)
        f = await r.get(db, j.id)
        if f and f.status in ("done", "failed"):
            break
    worker.stop()
    await task

    f = await r.get(db, j.id)
    assert f is not None
    assert f.status == "done", (
        f"TtsWorker didn't recover from locked-DB; status is {f.status}"
    )


async def test_tts_worker_propagates_cancelled_error(
    db: aiosqlite.Connection, tmp_path,
):
    """CancelledError must propagate out of run() so the lifespan can
    await the task on shutdown."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()

    async def fake_translate(text, **kw): return text
    async def fake_render(*a, **kw): return None
    async def fake_voice(*a, **kw): return tmp_path / "voice.onnx"

    worker = TtsWorker(
        db=db, config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render,
        ensure_voice=fake_voice,
        poll_interval=0.05,
    )
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_tts_worker_heartbeat_marks_restart_after_crash(
    db: aiosqlite.Connection, tmp_path,
):
    """The heartbeat must show 'restarting after crash' during the
    5s sleep after a crash, so the diagnostics page reflects it."""
    from app.services.heartbeat import HeartbeatRegistry

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed_video_with_summary_short(db, "vt1")
    await r.enqueue(db, "vt1", "summary", "en", "amy", "low")

    hb = HeartbeatRegistry()

    async def fake_translate(text, **kw): return text
    async def fake_render(*a, **kw): return None
    async def fake_voice(*a, **kw): return tmp_path / "voice.onnx"

    worker = TtsWorker(
        db=db, config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render,
        ensure_voice=fake_voice,
        poll_interval=0.05,
        heartbeat=hb,
    )

    async def boom(job):
        raise _SneakyTtsError("crash to trigger restart heartbeat")

    worker._process = boom

    task = asyncio.create_task(worker.run())
    saw_restart = False
    for _ in range(40):
        await asyncio.sleep(0.05)
        snap = hb.snapshot().get("tts_worker")
        if snap and snap.current_step == "restarting after crash":
            saw_restart = True
            break
    worker.stop()
    await task
    assert saw_restart
