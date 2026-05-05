import aiosqlite

from app.models import JobState
from app.repos import jobs as jobs_repo
from app.repos import videos as videos_repo


async def _video(db: aiosqlite.Connection, vid: str = "v1") -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )


async def test_enqueue_creates_pending_job(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.PENDING
    assert job.video_id == "v1"


async def test_claim_next_returns_oldest_pending(db: aiosqlite.Connection):
    await _video(db, "a")
    await _video(db, "b")
    j1 = await jobs_repo.enqueue(db, "a")
    j2 = await jobs_repo.enqueue(db, "b")
    claimed = await jobs_repo.claim_next(db)
    assert claimed is not None
    assert claimed.id == j1
    assert claimed.state is JobState.RUNNING
    # Second claim picks the next one
    claimed2 = await jobs_repo.claim_next(db)
    assert claimed2 is not None
    assert claimed2.id == j2


async def test_claim_next_returns_none_when_no_pending(db: aiosqlite.Connection):
    assert await jobs_repo.claim_next(db) is None


async def test_set_step_updates_step(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.set_step(db, job_id, "downloading audio")
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.step == "downloading audio"


async def test_complete_marks_done(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.complete(db, job_id)
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.DONE


async def test_fail_marks_failed_with_message(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.fail(db, job_id, "oops")
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.FAILED
    assert job.error_message == "oops"


async def test_reset_running_to_pending(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    claimed = await jobs_repo.claim_next(db)
    assert claimed is not None
    await jobs_repo.reset_orphaned_running(db)
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.PENDING


async def test_latest_for_video(db: aiosqlite.Connection):
    await _video(db)
    await jobs_repo.enqueue(db, "v1")
    j2 = await jobs_repo.enqueue(db, "v1")
    latest = await jobs_repo.latest_for_video(db, "v1")
    assert latest is not None
    assert latest.id == j2
