import asyncio
from unittest.mock import AsyncMock

import aiosqlite

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
