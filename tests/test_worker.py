import asyncio
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from app.config import Config
from app.models import JobState
from app.repos import jobs as jobs_repo
from app.repos import videos as videos_repo
from app.worker import Worker


async def test_worker_processes_pending_job(db: aiosqlite.Connection, tmp_path):
    config = Config(data_dir=tmp_path)

    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    job_id = await jobs_repo.enqueue(db, "v1")

    process_video = AsyncMock(return_value=None)
    worker = Worker(db=db, config=config, process_video=process_video, poll_interval=0.05)
    task = asyncio.create_task(worker.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        job = await jobs_repo.get(db, job_id)
        if job and job.state is JobState.DONE:
            break
    worker.stop()
    await task

    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.DONE
    process_video.assert_awaited_once()


async def test_worker_marks_failed_on_exception(db: aiosqlite.Connection, tmp_path):
    config = Config(data_dir=tmp_path)

    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    job_id = await jobs_repo.enqueue(db, "v1")

    async def boom(db, config, video_id, set_step):
        raise RuntimeError("kaboom")

    worker = Worker(db=db, config=config, process_video=boom, poll_interval=0.05)
    task = asyncio.create_task(worker.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        job = await jobs_repo.get(db, job_id)
        if job and job.state is JobState.FAILED:
            break
    worker.stop()
    await task

    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.FAILED
    assert job.error_message is not None
    assert "kaboom" in job.error_message


async def test_worker_touches_heartbeat_each_iteration(
    db: aiosqlite.Connection, tmp_path,
):
    """The diagnostics page needs proof that the loop is alive even
    when the queue is empty. touch() must fire on every iteration."""
    from app.services.heartbeat import HeartbeatRegistry

    config = Config(data_dir=tmp_path)
    hb = HeartbeatRegistry()
    worker = Worker(
        db=db, config=config, process_video=AsyncMock(return_value=None),
        poll_interval=0.05, heartbeat=hb,
    )
    task = asyncio.create_task(worker.run())
    # Give it enough time to loop at least once on an empty queue.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if "summary_worker" in hb.snapshot():
            break
    worker.stop()
    await task

    snap = hb.snapshot()
    assert "summary_worker" in snap
    assert snap["summary_worker"].current_job_id is None  # idle


async def test_worker_heartbeat_records_current_job(
    db: aiosqlite.Connection, tmp_path,
):
    from app.services.heartbeat import HeartbeatRegistry

    config = Config(data_dir=tmp_path)
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    job_id = await jobs_repo.enqueue(db, "v1")

    seen_job_ids: list[int | None] = []
    hb = HeartbeatRegistry()

    async def slow_process(db_, config_, video_id, set_step):
        # Capture the heartbeat as it would be observed by the page.
        await asyncio.sleep(0.05)
        snap = hb.snapshot()
        if "summary_worker" in snap:
            seen_job_ids.append(snap["summary_worker"].current_job_id)

    worker = Worker(
        db=db, config=config, process_video=slow_process,
        poll_interval=0.05, heartbeat=hb,
    )
    task = asyncio.create_task(worker.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        j = await jobs_repo.get(db, job_id)
        if j and j.state is JobState.DONE:
            break
    worker.stop()
    await task

    assert job_id in seen_job_ids


def test_worker_poll_interval_seconds_property():
    """Expose poll_interval as a public read-only attribute so the
    diagnostics view doesn't reach into _poll_interval."""
    from app.services.heartbeat import HeartbeatRegistry
    worker = Worker(
        db=None, config=None, process_video=AsyncMock(),
        poll_interval=0.5, heartbeat=HeartbeatRegistry(),
    )
    assert worker.poll_interval_seconds == 0.5


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
