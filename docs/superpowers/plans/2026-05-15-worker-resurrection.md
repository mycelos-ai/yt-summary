# Worker Resurrection + Realistic Heartbeat Threshold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `Worker` and `TtsWorker` survive any single-iteration crash (including non-`Exception` `BaseException` subclasses and DB-locked errors from `claim_next`), and raise the diagnostics-page "alive vs stale" threshold to a realistic 5 minutes for the polling workers.

**Architecture:** Each worker's `run()` becomes a thin outer try/except wrapper around a new `_run_iteration()` method that holds the existing single-pass loop body. The outer wrapper catches all `BaseException` except `asyncio.CancelledError` (which propagates for clean shutdown), logs with traceback, stamps a `"restarting after crash"` heartbeat, and sleeps 5 seconds before continuing. The diagnostics route swaps the per-worker `poll_interval × 3` threshold for a fixed `_POLLER_STALE_SECONDS = 300.0` constant; the scheduler's interval-based threshold stays unchanged.

**Tech Stack:** Python 3.11+, asyncio, aiosqlite, FastAPI, pytest with `asyncio_mode = "auto"`. No new dependencies.

---

## File Structure

**Modified files:**
- `app/worker.py` — split `run()` into outer crash-survival loop + inner `_run_iteration()` with the existing body. ~30 added lines, no removals.
- `app/tts_worker.py` — same pattern, name `"tts_worker"`. ~30 added lines.
- `app/routes/settings.py` — replace `poll_interval × 3` with `_POLLER_STALE_SECONDS = 300.0` for both pollers. ~5 changed lines.
- `tests/test_worker.py` — append 5 new tests (RuntimeError survival, BaseException survival, claim_next failure survival, CancelledError propagation, restart-heartbeat).
- `tests/test_tts_worker.py` — append the same 5 tests adapted to the TtsWorker shape.
- `tests/test_routes_diagnostics.py` — append 3 new tests for the new threshold semantics.

**No new files.** Everything lands in existing modules.

---

## Task 1: `Worker.run()` outer crash-survival loop

**Files:**
- Modify: `app/worker.py`
- Test: `tests/test_worker.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worker.py`:

```python
class _SneakyError(BaseException):
    """Test fixture: a BaseException subclass that's NOT an Exception.

    Mirrors what `asyncio.CancelledError` looked like in 3.7- and what
    a misbehaving C extension can still raise. The current Worker's
    `except Exception` doesn't catch this, so without the resurrection
    fix it kills the loop.
    """


async def test_worker_survives_runtime_error_and_processes_next_job(
    db: aiosqlite.Connection, tmp_path,
):
    """First process_video raises RuntimeError, second returns None.
    Worker must keep running and complete job 2."""
    config = Config(data_dir=tmp_path)
    await videos_repo.upsert_metadata(
        db, video_id="va", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="vb", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    job_a = await jobs_repo.enqueue(db, "va")
    job_b = await jobs_repo.enqueue(db, "vb")

    calls = {"n": 0}

    async def flaky(_db, _cfg, _vid, _set_step):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient")
        # Second call: succeed.

    worker = Worker(
        db=db, config=config, process_video=flaky, poll_interval=0.05,
    )
    task = asyncio.create_task(worker.run())
    for _ in range(120):
        await asyncio.sleep(0.05)
        j_b = await jobs_repo.get(db, job_b)
        if j_b and j_b.state is JobState.DONE:
            break
    worker.stop()
    await task

    # job_a was failed by the inner except, job_b was completed.
    j_a = await jobs_repo.get(db, job_a)
    j_b = await jobs_repo.get(db, job_b)
    assert j_a is not None and j_a.state is JobState.FAILED
    assert j_b is not None and j_b.state is JobState.DONE


async def test_worker_survives_base_exception_in_process_video(
    db: aiosqlite.Connection, tmp_path,
):
    """A BaseException subclass (not an Exception) from process_video
    used to kill the loop. The outer except must catch it."""
    config = Config(data_dir=tmp_path)
    await videos_repo.upsert_metadata(
        db, video_id="va", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await jobs_repo.enqueue(db, "va")

    seen = {"crashed": False}

    async def bombs_then_idle(_db, _cfg, _vid, _set_step):
        seen["crashed"] = True
        raise _SneakyError("not an Exception subclass")

    worker = Worker(
        db=db, config=config, process_video=bombs_then_idle,
        poll_interval=0.05,
    )
    task = asyncio.create_task(worker.run())
    # Wait for the worker to attempt the job, crash, and survive into
    # the 5s recovery sleep. We can't wait the full 5s in a test, so
    # we just verify: the crash happened, AND the task is still alive.
    for _ in range(40):
        await asyncio.sleep(0.05)
        if seen["crashed"]:
            break
    assert seen["crashed"], "process_video was never invoked"
    # Task must NOT be done (i.e. the BaseException didn't kill it).
    assert not task.done(), (
        "Worker died on BaseException — outer except is missing or wrong"
    )
    worker.stop()
    await task


async def test_worker_survives_claim_next_db_failure(
    db: aiosqlite.Connection, tmp_path, monkeypatch,
):
    """An OperationalError from claim_next (e.g. `database is locked`)
    used to crash the loop because claim_next was outside the inner
    try/except. The outer wrapper must catch it."""
    from app.repos import jobs as jobs_repo_mod

    config = Config(data_dir=tmp_path)
    await videos_repo.upsert_metadata(
        db, video_id="va", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    job_a = await jobs_repo.enqueue(db, "va")

    real_claim = jobs_repo_mod.claim_next
    calls = {"n": 0}

    async def flaky_claim(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise aiosqlite.OperationalError("database is locked")
        return await real_claim(conn)

    monkeypatch.setattr(jobs_repo_mod, "claim_next", flaky_claim)

    worker = Worker(
        db=db, config=config,
        process_video=AsyncMock(return_value=None),
        poll_interval=0.05,
    )
    task = asyncio.create_task(worker.run())
    for _ in range(160):  # tolerates the 5s post-crash sleep
        await asyncio.sleep(0.05)
        j = await jobs_repo.get(db, job_a)
        if j and j.state is JobState.DONE:
            break
    worker.stop()
    await task

    j = await jobs_repo.get(db, job_a)
    assert j is not None
    assert j.state is JobState.DONE, (
        f"Worker didn't recover from the locked-DB simulation; "
        f"job state is {j.state}"
    )


async def test_worker_propagates_cancelled_error_for_clean_shutdown(
    db: aiosqlite.Connection, tmp_path,
):
    """The outer except must NOT swallow CancelledError, otherwise
    the lifespan can never await the worker_task on shutdown."""
    config = Config(data_dir=tmp_path)
    worker = Worker(
        db=db, config=config,
        process_video=AsyncMock(return_value=None),
        poll_interval=0.05,
    )
    task = asyncio.create_task(worker.run())
    # Let it settle into the idle wait.
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_worker_heartbeat_marks_restart_after_crash(
    db: aiosqlite.Connection, tmp_path,
):
    """During the 5s recovery sleep after a crash, the heartbeat
    snapshot must include a `restarting after crash` step so the
    diagnostics page reflects the crash."""
    from app.services.heartbeat import HeartbeatRegistry

    config = Config(data_dir=tmp_path)
    await videos_repo.upsert_metadata(
        db, video_id="va", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await jobs_repo.enqueue(db, "va")

    hb = HeartbeatRegistry()

    async def boom(_db, _cfg, _vid, _set_step):
        raise _SneakyError("crash to trigger restart heartbeat")

    worker = Worker(
        db=db, config=config, process_video=boom,
        poll_interval=0.05, heartbeat=hb,
    )
    task = asyncio.create_task(worker.run())
    # Wait for the crash-then-restart heartbeat to land.
    saw_restart = False
    for _ in range(40):
        await asyncio.sleep(0.05)
        snap = hb.snapshot().get("summary_worker")
        if snap and snap.current_step == "restarting after crash":
            saw_restart = True
            break
    worker.stop()
    await task
    assert saw_restart, (
        "Heartbeat never showed 'restarting after crash' step"
    )
```

Add a top-of-file `pytest` import if not already present:

```python
import pytest
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker.py -v -k "survives or propagates_cancelled or restart"`
Expected: FAIL — most fail with `RuntimeError`/`_SneakyError`/`OperationalError` propagating out and killing the worker task. The `propagates_cancelled` test may pass coincidentally (CancelledError currently isn't caught either) — that's fine, the rest fail.

- [ ] **Step 3: Restructure `Worker.run()` in `app/worker.py`**

Find the existing `Worker` class. The current `run()` looks like:

```python
async def run(self) -> None:
    while not self._stopped.is_set():
        job = await jobs_repo.claim_next(self._db)
        if job is None:
            self._touch()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
            continue
        try:
            job_id_capture = job.id
            self._touch(current_job_id=job.id, current_step=job.step or "starting")

            async def set_step(step: str, _job_id: int = job_id_capture) -> None:  # noqa: B023
                await jobs_repo.set_step(self._db, _job_id, step)
                self._touch(current_job_id=_job_id, current_step=step)

            await self._process_video(self._db, self._config, job.video_id, set_step)
            await jobs_repo.complete(self._db, job.id)
        except Exception as e:
            log.exception("job %s failed", job.id)
            await jobs_repo.fail(self._db, job.id, str(e))
```

Replace with this two-method structure:

```python
async def run(self) -> None:
    """Top-level loop. Crash-resistant: any error from a single
    iteration (including BaseException subclasses other than
    CancelledError, and OperationalError from claim_next) is logged
    and survived. The worker only ever exits when stop() is called
    or the task is cancelled by the lifespan on shutdown."""
    while not self._stopped.is_set():
        try:
            await self._run_iteration()
        except asyncio.CancelledError:
            # Shutdown signal — propagate so the lifespan's
            # `await worker_task` returns cleanly.
            raise
        except BaseException:
            # Anything else: log with traceback, stamp the heartbeat
            # so the diagnostics page surfaces the crash, then sleep
            # 5s (interruptible by stop()) before resuming.
            log.exception(
                "summary_worker: unexpected crash, restarting in 5s"
            )
            self._touch(current_step="restarting after crash")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), 5.0)

async def _run_iteration(self) -> None:
    """One pass of the loop: claim or sleep, then handle the job.

    All pre-existing inner exception handling is preserved — pipeline
    failures still mark the job `failed` via the inner try/except.
    The outer crash-survival wrapper in `run()` only catches things
    that escape this method entirely.
    """
    job = await jobs_repo.claim_next(self._db)
    if job is None:
        self._touch()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
        return
    try:
        job_id_capture = job.id
        self._touch(current_job_id=job.id, current_step=job.step or "starting")

        async def set_step(step: str, _job_id: int = job_id_capture) -> None:  # noqa: B023
            await jobs_repo.set_step(self._db, _job_id, step)
            self._touch(current_job_id=_job_id, current_step=step)

        await self._process_video(self._db, self._config, job.video_id, set_step)
        await jobs_repo.complete(self._db, job.id)
    except Exception as e:
        log.exception("job %s failed", job.id)
        await jobs_repo.fail(self._db, job.id, str(e))
```

The `_touch` helper, `stop()`, `__init__`, and the `poll_interval_seconds` property all stay unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker.py -v`
Expected: PASS — all 5 new tests + every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add app/worker.py tests/test_worker.py
git commit -m "feat(worker): outer try/except so the loop survives any crash"
```

---

## Task 2: `TtsWorker.run()` outer crash-survival loop

**Files:**
- Modify: `app/tts_worker.py`
- Test: `tests/test_tts_worker.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tts_worker.py`:

```python
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
```

Add `import pytest` to the top of the file if not already imported.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tts_worker.py -v -k "survives or propagates_cancelled or restart"`
Expected: FAIL on the survival tests (loop dies on the raised exceptions). The cancelled-error test may pass coincidentally — that's fine.

- [ ] **Step 3: Restructure `TtsWorker.run()` in `app/tts_worker.py`**

Find the existing `TtsWorker.run()`:

```python
async def run(self) -> None:
    while not self._stopped.is_set():
        job = await tts_jobs_repo.claim_next(self._db)
        if job is None:
            self._touch()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopped.wait(), self._poll_interval
                )
            continue
        try:
            self._touch(current_job_id=job.id, current_step=job.step or "starting")
            audio_rel, duration, translated = await self._process(job)
            await tts_jobs_repo.complete(
                self._db,
                job.id,
                audio_path=audio_rel,
                duration_seconds=duration,
                translated_text=translated,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced via .fail()
            log.exception("tts job %s failed", job.id)
            await tts_jobs_repo.fail(self._db, job.id, str(exc))
```

Replace with:

```python
async def run(self) -> None:
    """Top-level loop. Crash-resistant: any error from a single
    iteration (including BaseException subclasses other than
    CancelledError, and OperationalError from claim_next) is logged
    and survived. The worker only ever exits when stop() is called
    or the task is cancelled by the lifespan on shutdown."""
    while not self._stopped.is_set():
        try:
            await self._run_iteration()
        except asyncio.CancelledError:
            # Shutdown signal — propagate so the lifespan's
            # `await tts_worker_task` returns cleanly.
            raise
        except BaseException:
            log.exception(
                "tts_worker: unexpected crash, restarting in 5s"
            )
            self._touch(current_step="restarting after crash")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), 5.0)

async def _run_iteration(self) -> None:
    """One pass of the loop: claim or sleep, then handle the job."""
    job = await tts_jobs_repo.claim_next(self._db)
    if job is None:
        self._touch()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._stopped.wait(), self._poll_interval
            )
        return
    try:
        self._touch(current_job_id=job.id, current_step=job.step or "starting")
        audio_rel, duration, translated = await self._process(job)
        await tts_jobs_repo.complete(
            self._db,
            job.id,
            audio_path=audio_rel,
            duration_seconds=duration,
            translated_text=translated,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced via .fail()
        log.exception("tts job %s failed", job.id)
        await tts_jobs_repo.fail(self._db, job.id, str(exc))
```

Everything else (`__init__`, `stop`, `_touch`, `poll_interval_seconds`, `_process`, `_build_complete`) stays unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tts_worker.py -v`
Expected: PASS — all 5 new tests + every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add app/tts_worker.py tests/test_tts_worker.py
git commit -m "feat(tts_worker): outer try/except so the loop survives any crash"
```

---

## Task 3: Realistic poller-stale threshold in diagnostics route

**Files:**
- Modify: `app/routes/settings.py`
- Test: `tests/test_routes_diagnostics.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routes_diagnostics.py`:

```python
def test_summary_worker_alive_within_300s_threshold(tmp_path, monkeypatch):
    """A heartbeat 250 s old must render as alive — Whisper chunks on
    a Pi can legitimately take that long without firing set_step."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Plant a heartbeat 250 s in the past.
        from app.services.heartbeat import Heartbeat
        old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=250)
        app.state.heartbeats._heartbeats["summary_worker"] = Heartbeat(
            name="summary_worker",
            last_tick_at=old,
            current_job_id=None,
            current_step="downloading audio",
        )
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200
    # Find the summary_worker row and confirm the alive marker.
    text = resp.text
    # Sloppy but adequate: find the row by name then look at the
    # nearby cells. The row contains <code>summary_worker</code>
    # and the status pill text.
    summary_row_idx = text.find("summary_worker")
    assert summary_row_idx >= 0
    near = text[summary_row_idx:summary_row_idx + 800]
    assert "✅ alive" in near, (
        f"Expected ✅ alive within 300s; got: {near[:300]!r}"
    )


def test_summary_worker_stale_past_300s_threshold(tmp_path, monkeypatch):
    """A heartbeat 350 s old must render as stale."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        from app.services.heartbeat import Heartbeat
        old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=350)
        app.state.heartbeats._heartbeats["summary_worker"] = Heartbeat(
            name="summary_worker",
            last_tick_at=old,
            current_job_id=None,
            current_step="downloading audio",
        )
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200
    summary_row_idx = resp.text.find("summary_worker")
    assert summary_row_idx >= 0
    near = resp.text[summary_row_idx:summary_row_idx + 800]
    assert "⚠ stale" in near, (
        f"Expected ⚠ stale past 300s; got: {near[:300]!r}"
    )


def test_scheduler_threshold_still_uses_interval(tmp_path, monkeypatch):
    """The scheduler keeps its interval-derived threshold (3× interval).
    A heartbeat 2 hours old with a 60-min interval = alive (under the
    180-min threshold). 4 hours old = stale."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from app.repos import settings as settings_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Set the playlist refresh interval to 60 minutes so the
        # threshold becomes 180 minutes.
        async def setup():
            await settings_repo.set(
                app.state.db,
                "playlist_refresh_interval_minutes", "60",
            )
        asyncio.get_event_loop().run_until_complete(setup())

        from app.services.heartbeat import Heartbeat
        # 2 hours old → still alive (under the 3-hour threshold).
        two_hours_ago = (
            datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        )
        app.state.heartbeats._heartbeats["scheduler"] = Heartbeat(
            name="scheduler",
            last_tick_at=two_hours_ago,
            current_job_id=None,
            current_step="sleeping",
        )
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200
    sched_idx = resp.text.find("scheduler</code>")
    assert sched_idx >= 0
    near = resp.text[sched_idx:sched_idx + 800]
    assert "✅ alive" in near, (
        f"Scheduler 2h-old heartbeat should be alive (3h threshold); "
        f"got: {near[:300]!r}"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routes_diagnostics.py -v -k "300s or scheduler_threshold"`
Expected: FAIL — the first test (`alive_within_300s`) fails because today the threshold for `summary_worker` is `1.0 × 3 = 3 s`, so a 250 s-old heartbeat renders as `⚠ stale (250s)`.

- [ ] **Step 3: Update `app/routes/settings.py:diagnostics_page`**

Find the current threshold-computation block in `diagnostics_page`:

```python
# Resolve the per-worker stale threshold = 3 × poll_interval.
# 3× gives the 1s pollers a ~3s budget and the 60-min scheduler
# a ~3h budget — matches what the spec calls for.
worker_thresholds_seconds = {
    "summary_worker": worker.poll_interval_seconds * 3,
    "tts_worker":     tts_worker.poll_interval_seconds * 3,
    "scheduler":      (await scheduler.current_interval_seconds()) * 3,
}
```

Replace with:

```python
# Polling workers (Worker, TtsWorker) can spend minutes in a single
# Whisper or render step without firing a heartbeat update. A
# pessimistic 3× poll_interval threshold (≈ 3s) would mark them
# stale every long-running job, which is the opposite of useful.
# Stick with a generous fixed window: if no tick in 5 minutes, the
# worker is genuinely stuck or dead. The scheduler keeps its
# interval-derived threshold because its expected idle is hours.
_POLLER_STALE_SECONDS = 300.0
worker_thresholds_seconds = {
    "summary_worker": _POLLER_STALE_SECONDS,
    "tts_worker":     _POLLER_STALE_SECONDS,
    "scheduler":      (await scheduler.current_interval_seconds()) * 3,
}
```

Note: the local variable `worker` and `tts_worker` (read from `request.app.state`) are no longer used after this change. Leave them — they're cheap and removing them would clutter the diff. Ruff won't complain because they're still defined a few lines up and the linter sees them assigned.

Actually verify: after the change, are `worker` and `tts_worker` still referenced anywhere else in the function body? If not, dropping their assignments is fine. Read the function and decide; if unused, also remove the two lines:

```python
worker = request.app.state.worker
tts_worker = request.app.state.tts_worker
```

If they ARE still used (e.g., for something else in the template context), leave them.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routes_diagnostics.py -v`
Expected: PASS — 3 new tests + every pre-existing test in the file.

- [ ] **Step 5: Run the full suite to confirm no regression**

Run: `pytest -q --ignore=tests/test_services_model_info.py`
Expected: ~647 PASS. The known-flaky `test_post_retry_tts_preserves_translated_text` may fail in the suite-wide run due to test isolation issues unrelated to this PR (it passes when run alone) — if that's the only failure, ignore it.

- [ ] **Step 6: Run ruff**

Run: `ruff check app/ tests/`
Expected: clean (or only pre-existing warnings unrelated to this PR).

- [ ] **Step 7: Commit**

```bash
git add app/routes/settings.py tests/test_routes_diagnostics.py
git commit -m "feat(diagnostics): 5-min staleness threshold for polling workers"
```

---

## Task 4: Final verification

- [ ] **Step 1: Full suite**

Run: `pytest -q --ignore=tests/test_services_model_info.py`
Expected: ALL PASS (or just the one known-flaky diagnostics-test failure noted in Task 3).

- [ ] **Step 2: Ruff**

Run: `ruff check app/ tests/`
Expected: clean.

- [ ] **Step 3: Manual smoke (optional but recommended)**

```bash
YTS_DATA_DIR=./data uvicorn app.main:app --port 8000 --reload
```

Visit `http://127.0.0.1:8000/settings/diagnostics`:
- Both `summary_worker` and `tts_worker` should show `✅ alive` (their heartbeats are seconds old).
- The Worker-Status table should NOT show `⚠ stale` for either polling worker, even after sitting idle for several minutes.
- Optionally, simulate a crash: edit `app/pipeline.py:process_video` to raise `BaseException("test")` at the top, restart the server, submit a new video. The job should be marked `failed` (no — wait, BaseException isn't caught by the inner except either, it'll go to outer; the worker survives, `current_step` shows `restarting after crash` for ~5s on the diagnostics page, then back to idle). Revert your test edit before committing anything.

Stop the server with Ctrl-C — confirm clean shutdown (no traceback about cancelled tasks).

- [ ] **Step 4: Confirm git state**

```bash
git status
git log --oneline -5
```

Expected: clean working tree, 3 new commits on `main`:
- `feat(diagnostics): 5-min staleness threshold for polling workers`
- `feat(tts_worker): outer try/except so the loop survives any crash`
- `feat(worker): outer try/except so the loop survives any crash`

---

## Self-review notes (post-write)

- **Spec coverage:** Goal 1 (workers survive any single-iteration exception) → Tasks 1 + 2. Goal 2 (DB-level failures during claim_next survived) → Tasks 1 + 2 (the outer wrapper covers `claim_next` automatically because that line is now inside `_run_iteration`). Goal 3 (realistic threshold) → Task 3. Goal 4 (CancelledError propagates for shutdown) → covered by the explicit `except asyncio.CancelledError: raise` clause in both Task 1 and Task 2 + a dedicated test in each.
- **Type consistency:** `_run_iteration` is the same name across both worker files. The outer-except step string is `"restarting after crash"` everywhere (worker, tts_worker, and the test assertions). The threshold constant is `_POLLER_STALE_SECONDS = 300.0` in `app/routes/settings.py` only.
- **Naming:** `_SneakyError` and `_SneakyTtsError` — separate names so the test files can be read independently. Both are `BaseException` subclasses, both used for the same purpose.
- **Test plumbing:** the new tests reuse the existing `db`, `tmp_path`, `monkeypatch` fixtures and the existing `videos_repo`/`jobs_repo`/`r` (tts_jobs) imports already at the top of each file. The Heartbeat-based tests instantiate a fresh `HeartbeatRegistry` and pass it via the worker's `heartbeat=` kwarg (existing API from prior PRs).
- **Race in `propagates_cancelled` test:** `task.cancel()` is called after a 100 ms sleep so the worker has settled into either `claim_next` or `wait_for(stop_event)`. Either way, the cancellation is delivered cleanly and `await task` raises `CancelledError`. No race.
- **The post-crash 5 s sleep in tests:** the heartbeat-mark and BaseException-survival tests don't wait the full 5 s — they check that the heartbeat shows `restarting after crash` OR that the task is still alive within ~2 s. Both conditions get satisfied within the polling loop's 40 × 50 ms = 2 s window.
