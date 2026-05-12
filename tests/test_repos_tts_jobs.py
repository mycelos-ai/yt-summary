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
