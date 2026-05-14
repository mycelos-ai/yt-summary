import asyncio

import aiosqlite

from app.repos import tts_jobs as r
from app.repos import videos as videos_repo


async def _video(db: aiosqlite.Connection, vid: str = "abc") -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )


async def test_enqueue_returns_new_job(db: aiosqlite.Connection):
    await _video(db, "abc")
    job = await r.enqueue(
        db, video_id="abc", source="summary",
        target_language="de", voice="thorsten", quality="medium",
    )
    assert job.id > 0
    assert job.status == "queued"


async def test_enqueue_returns_existing_when_duplicate(db: aiosqlite.Connection):
    """Same (video, source, lang, voice, quality) → reuse the row."""
    await _video(db, "abc")
    first = await r.enqueue(
        db, video_id="abc", source="summary",
        target_language="de", voice="thorsten", quality="medium",
    )
    second = await r.enqueue(
        db, video_id="abc", source="summary",
        target_language="de", voice="thorsten", quality="medium",
    )
    assert first.id == second.id


async def test_claim_next_transitions_queued_to_translating(db: aiosqlite.Connection):
    # Schema CHECK only allows queued/translating/rendering/done/failed.
    # claim_next lands on 'translating' as the first active phase in the
    # queued → translating → rendering → done state machine; the worker
    # advances to 'rendering' via set_status (immediately when no
    # translation is needed).
    await _video(db, "abc")
    await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    job = await r.claim_next(db)
    assert job is not None
    assert job.status == "translating"
    assert job.started_at is not None
    assert await r.claim_next(db) is None


async def test_set_step_updates_progress(db: aiosqlite.Connection):
    await _video(db, "abc")
    j = await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    await r.set_step(db, j.id, "rendering chunk 2/5")
    fresh = await r.get(db, j.id)
    assert fresh is not None
    assert fresh.step == "rendering chunk 2/5"


async def test_set_translated_text_persists_value(db: aiosqlite.Connection):
    """The mid-flight setter writes translated_text so a container
    restart between translate() and complete() doesn't lose the work."""
    await _video(db, "abc")
    job = await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    await r.set_translated_text(db, job.id, "Hallo Welt.")
    fresh = await r.get(db, job.id)
    assert fresh is not None
    assert fresh.translated_text == "Hallo Welt."


async def test_complete_marks_done_with_audio_path(db: aiosqlite.Connection):
    await _video(db, "abc")
    j = await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    await r.complete(
        db, j.id, audio_path="abc/summary-de.mp3",
        duration_seconds=128.4, translated_text="hallo",
    )
    fresh = await r.get(db, j.id)
    assert fresh is not None
    assert fresh.status == "done"
    assert fresh.audio_path == "abc/summary-de.mp3"
    assert fresh.duration_seconds == 128.4


async def test_fail_records_error(db: aiosqlite.Connection):
    await _video(db, "abc")
    j = await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    await r.fail(db, j.id, "boom")
    fresh = await r.get(db, j.id)
    assert fresh is not None
    assert fresh.status == "failed"
    assert fresh.error == "boom"


async def test_reset_orphaned_active_returns_translating_to_queued(db: aiosqlite.Connection):
    """A tts_jobs row stuck in 'translating' is moved back to 'queued'."""
    # Seed a video, enqueue, claim (which moves it to 'translating'), then reset.
    await _video(db, "abc")
    await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    claimed = await r.claim_next(db)
    assert claimed is not None
    assert claimed.status == "translating"

    n = await r.reset_orphaned_active(db)
    assert n == 1

    fresh = await r.get(db, claimed.id)
    assert fresh is not None
    assert fresh.status == "queued"
    assert fresh.started_at is None


async def test_reset_orphaned_active_returns_rendering_to_queued(db: aiosqlite.Connection):
    """A tts_jobs row stuck in 'rendering' is also moved back to 'queued'."""
    await _video(db, "abc")
    job = await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    await r.claim_next(db)
    await r.set_status(db, job.id, "rendering")

    n = await r.reset_orphaned_active(db)
    assert n == 1

    fresh = await r.get(db, job.id)
    assert fresh is not None
    assert fresh.status == "queued"


async def test_reset_orphaned_active_leaves_terminal_states_alone(db: aiosqlite.Connection):
    """Done and failed rows MUST NOT be reset."""
    await _video(db, "abc")
    j1 = await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    j2 = await r.enqueue(db, "abc", "transcript", "de", "thorsten", "medium")
    await r.complete(db, j1.id, audio_path="x.mp3", duration_seconds=1.0, translated_text=None)
    await r.fail(db, j2.id, "boom")

    n = await r.reset_orphaned_active(db)
    assert n == 0

    j1_fresh = await r.get(db, j1.id)
    j2_fresh = await r.get(db, j2.id)
    assert j1_fresh is not None and j1_fresh.status == "done"
    assert j2_fresh is not None and j2_fresh.status == "failed"


async def test_reset_orphaned_active_returns_zero_when_nothing_to_reset(db: aiosqlite.Connection):
    """Returns 0 when no orphaned rows exist."""
    n = await r.reset_orphaned_active(db)
    assert n == 0


async def test_list_for_video_returns_done_renderings_first(db: aiosqlite.Connection):
    await _video(db, "abc")
    await r.enqueue(db, "abc", "summary", "de", "thorsten", "medium")
    j2 = await r.enqueue(db, "abc", "transcript", "de", "thorsten", "medium")
    await r.complete(
        db, j2.id, audio_path="x", duration_seconds=10.0,
        translated_text=None,
    )
    rows = await r.list_for_video(db, "abc")
    assert rows[0].status == "done"


async def test_counts_collapses_translating_and_rendering_into_running(
    db: aiosqlite.Connection,
):
    """Both 'translating' and 'rendering' are active states; the
    diagnostics chip shows a single 'running' bucket."""
    await _video(db, "a")
    await _video(db, "b")
    await _video(db, "c")
    j_translating = await r.enqueue(db, "a", "summary", "de", "v", "low")
    j_rendering = await r.enqueue(db, "b", "summary", "de", "v", "low")
    j_failed = await r.enqueue(db, "c", "summary", "de", "v", "low")
    await r.set_status(db, j_translating.id, "translating")
    await r.set_status(db, j_rendering.id, "rendering")
    await r.fail(db, j_failed.id, "boom")

    counts = await r.counts(db)
    assert counts["queued"] == 0  # all three are non-queued now
    assert counts["running"] == 2
    assert counts["failed"] == 1


async def test_counts_done_24h_excludes_old_rows(db: aiosqlite.Connection):
    await _video(db, "a")
    j = await r.enqueue(db, "a", "summary", "de", "v", "low")
    await r.complete(
        db, j.id, audio_path="x.mp3", duration_seconds=1.0, translated_text=None,
    )
    await db.execute(
        "UPDATE tts_jobs SET finished_at=datetime('now','-2 days') WHERE id=?",
        (j.id,),
    )
    await db.commit()
    counts = await r.counts(db)
    assert counts["done_24h"] == 0


async def test_list_queue_returns_queued_and_running_with_title(
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
    # FIFO is by autoincrement id, not wall time — no sleep needed.
    j1 = await r.enqueue(db, "a", "summary", "de", "v", "low")
    j2 = await r.enqueue(db, "b", "summary", "de", "v", "low")
    rows = await r.list_queue(db, limit=10)
    assert [row[0].id for row in rows] == [j1.id, j2.id]
    assert [row[1] for row in rows] == ["Alpha", "Beta"]


async def test_list_recent_failed_orders_newest_first(db: aiosqlite.Connection):
    await _video(db, "a")
    await _video(db, "b")
    j1 = await r.enqueue(db, "a", "summary", "de", "v", "low")
    j2 = await r.enqueue(db, "b", "summary", "de", "v", "low")
    await r.fail(db, j1.id, "first")
    await asyncio.sleep(0.01)
    await r.fail(db, j2.id, "second")
    rows = await r.list_recent_failed(db, limit=10)
    assert [row[0].id for row in rows] == [j2.id, j1.id]


async def test_retry_resets_failed_back_to_queued_and_clears_error(
    db: aiosqlite.Connection,
):
    await _video(db, "a")
    j = await r.enqueue(db, "a", "summary", "de", "v", "low")
    await r.fail(db, j.id, "boom")
    affected = await r.retry(db, j.id)
    assert affected == 1
    fresh = await r.get(db, j.id)
    assert fresh is not None
    assert fresh.status == "queued"
    assert fresh.error is None
    assert fresh.started_at is None
    assert fresh.finished_at is None


async def test_retry_preserves_translated_text(db: aiosqlite.Connection):
    """A render-stage failure leaves translated_text populated. retry
    must NOT clear it — re-running translation costs the LLM call."""
    await _video(db, "a")
    j = await r.enqueue(db, "a", "summary", "de", "v", "low")
    await r.set_translated_text(db, j.id, "Hallo Welt")
    await r.fail(db, j.id, "render crashed")
    await r.retry(db, j.id)
    fresh = await r.get(db, j.id)
    assert fresh is not None
    assert fresh.translated_text == "Hallo Welt"


async def test_retry_refuses_non_failed_job(db: aiosqlite.Connection):
    await _video(db, "a")
    j = await r.enqueue(db, "a", "summary", "de", "v", "low")
    # status is 'queued' — retry should refuse.
    affected = await r.retry(db, j.id)
    assert affected == 0
