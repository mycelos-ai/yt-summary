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
