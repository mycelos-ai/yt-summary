import asyncio

import aiosqlite

from app.models import JobState
from app.repos import jobs as jobs_repo
from app.repos import llm_models as llm_models_repo
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


async def test_claim_next_does_not_leave_open_transaction(
    db: aiosqlite.Connection,
):
    """Regression for the prod crash on the Pi: claim_next used to do
    BEGIN IMMEDIATE / SELECT / UPDATE / COMMIT and left the connection
    in a half-open transaction if any step raised between BEGIN and
    COMMIT. The next call then crashed the worker loop with:

        OperationalError: cannot start a transaction within a transaction

    The fix is a single atomic UPDATE…RETURNING — no manual BEGIN.
    Ensure two consecutive calls work even when the first found no
    pending job AND no commit was needed.
    """
    # Empty queue → no rows. Today this returns None cleanly.
    assert await jobs_repo.claim_next(db) is None
    # Without the fix, this second call would crash if claim_next had
    # left an open transaction. We don't manually trigger an error in
    # between — the bug was that even the success path left state
    # behind. Two clean calls in a row prove the contract.
    assert await jobs_repo.claim_next(db) is None
    # Add a pending job and confirm we can still claim it.
    await _video(db, "v1")
    await jobs_repo.enqueue(db, "v1")
    claimed = await jobs_repo.claim_next(db)
    assert claimed is not None
    assert claimed.video_id == "v1"
    assert claimed.state is JobState.RUNNING


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


async def test_counts_buckets_match_state(db: aiosqlite.Connection):
    """One job in each of the four buckets, all reachable via the
    public repo API (no raw SQL fiddling)."""
    await _video(db, "a")
    await _video(db, "b")
    await _video(db, "c")
    await _video(db, "d")
    # 1 pending — enqueue and leave it alone.
    await jobs_repo.enqueue(db, "a")
    # 1 running — enqueue then claim_next promotes oldest pending to running.
    await jobs_repo.enqueue(db, "b")
    claimed = await jobs_repo.claim_next(db)
    assert claimed is not None and claimed.video_id == "a"
    # claim_next moved "a" → running, leaving "b" pending.
    # 1 failed
    j_failed = await jobs_repo.enqueue(db, "c")
    await jobs_repo.fail(db, j_failed, "boom")
    # 1 done within 24h
    j_done = await jobs_repo.enqueue(db, "d")
    await jobs_repo.complete(db, j_done)

    counts = await jobs_repo.counts(db)
    assert counts["pending"] == 1   # "b"
    assert counts["running"] == 1   # "a"
    assert counts["failed"] == 1    # "c"
    assert counts["done_24h"] == 1  # "d"


async def test_counts_done_24h_excludes_old_rows(db: aiosqlite.Connection):
    await _video(db, "old")
    j = await jobs_repo.enqueue(db, "old")
    await jobs_repo.complete(db, j)
    # Backdate the row past the 24h window.
    await db.execute(
        "UPDATE jobs SET updated_at=datetime('now','-2 days') WHERE id=?",
        (j,),
    )
    await db.commit()
    counts = await jobs_repo.counts(db)
    assert counts["done_24h"] == 0


async def test_list_queue_returns_pending_and_running_with_title(
    db: aiosqlite.Connection,
):
    await videos_repo.upsert_metadata(
        db, video_id="a", url="u", title="Alpha", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="b", url="u", title="Beta", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await jobs_repo.enqueue(db, "a")
    await asyncio.sleep(0.01)  # ensure created_at differs
    await jobs_repo.enqueue(db, "b")
    rows = await jobs_repo.list_queue(db, limit=10)
    assert len(rows) == 2
    # FIFO: oldest first.
    assert rows[0][1] == "Alpha"
    assert rows[1][1] == "Beta"


async def test_list_queue_falls_back_to_video_id_when_video_missing(
    db: aiosqlite.Connection,
):
    """If the video row was deleted but a job lingers, the queue view
    must still render something."""
    await _video(db, "v1")
    await jobs_repo.enqueue(db, "v1")
    # Simulate the video being deleted while a job exists. Temporarily
    # disable FK enforcement so we can delete the video without the
    # jobs.video_id FK blocking us — the repo's LEFT JOIN must handle
    # this orphan gracefully.
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.execute("DELETE FROM videos WHERE id='v1'")
    await db.commit()
    await db.execute("PRAGMA foreign_keys = ON")
    rows = await jobs_repo.list_queue(db, limit=10)
    assert len(rows) == 1
    _, title = rows[0]
    assert title == "v1"  # fell back to id


async def test_list_recent_failed_orders_newest_first(db: aiosqlite.Connection):
    await _video(db, "a")
    await _video(db, "b")
    ja = await jobs_repo.enqueue(db, "a")
    jb = await jobs_repo.enqueue(db, "b")
    await jobs_repo.fail(db, ja, "first")
    await asyncio.sleep(0.01)
    await jobs_repo.fail(db, jb, "second")
    rows = await jobs_repo.list_recent_failed(db, limit=10)
    assert len(rows) == 2
    assert rows[0][0].id == jb  # newest
    assert rows[1][0].id == ja


async def test_list_recent_failed_marks_video_done(db: aiosqlite.Connection):
    """A failed job whose video already has a summary is flagged so
    the diagnostics page can disable the Retry button."""
    await _video(db, "with_summary")
    await _video(db, "no_summary")
    j_done = await jobs_repo.enqueue(db, "with_summary")
    j_pending = await jobs_repo.enqueue(db, "no_summary")
    # The video gets a summary AFTER the job failed (e.g. a later
    # successful re-attempt that didn't go through the diagnostics
    # retry path).
    await jobs_repo.fail(db, j_done, "stale")
    await jobs_repo.fail(db, j_pending, "still broken")
    await db.execute(
        "UPDATE videos SET summary='hello world' WHERE id='with_summary'"
    )
    await db.commit()

    rows = await jobs_repo.list_recent_failed(db, limit=10)
    by_id = {job.id: video_done for job, _, video_done in rows}
    assert by_id[j_done] is True
    assert by_id[j_pending] is False


async def test_retry_flips_failed_to_pending_and_clears_error(
    db: aiosqlite.Connection,
):
    await _video(db)
    j = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.fail(db, j, "oops")
    await jobs_repo.retry(db, j)
    job = await jobs_repo.get(db, j)
    assert job is not None
    assert job.state is JobState.PENDING
    assert job.error_message is None


async def test_retry_refuses_non_failed_job(db: aiosqlite.Connection):
    """retry on a running/pending/done row must be a no-op (caller will
    see rowcount==0 and 404)."""
    await _video(db)
    j = await jobs_repo.enqueue(db, "v1")
    # Currently pending — retry should refuse.
    affected = await jobs_repo.retry(db, j)
    assert affected == 0
    job = await jobs_repo.get(db, j)
    assert job is not None
    assert job.state is JobState.PENDING  # unchanged


async def test_delete_removes_failed_row(db: aiosqlite.Connection):
    await _video(db)
    j = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.fail(db, j, "x")
    affected = await jobs_repo.delete(db, j)
    assert affected == 1
    assert await jobs_repo.get(db, j) is None


async def test_delete_refuses_running_row(db: aiosqlite.Connection):
    await _video(db)
    j = await jobs_repo.enqueue(db, "v1")
    claimed = await jobs_repo.claim_next(db)
    assert claimed is not None and claimed.id == j
    affected = await jobs_repo.delete(db, j)
    assert affected == 0
    assert await jobs_repo.get(db, j) is not None


async def test_enqueue_without_overrides_defaults_to_null(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.llm_model_id is None
    assert job.additional_prompt is None


async def test_enqueue_with_overrides_persists_them(db: aiosqlite.Connection):
    await _video(db)
    mid = await llm_models_repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    job_id = await jobs_repo.enqueue(
        db, "v1",
        llm_model_id=mid,
        additional_prompt="be terse",
    )
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.llm_model_id == mid
    assert job.additional_prompt == "be terse"
