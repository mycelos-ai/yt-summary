import asyncio

from app.models import SpeakerJobState
from app.repos import speaker_jobs as sj
from app.repos import speakers as speakers_repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_enqueue_and_claim_next(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Hank H")
        jid = await sj.enqueue(db, sid)
        assert jid > 0
        job = await sj.claim_next(db)
        assert job is not None
        assert job.id == jid
        assert job.state == SpeakerJobState.RUNNING
        assert await sj.claim_next(db) is None   # queue now empty
    _run(go())


def test_set_step_complete_fail(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Ivy I")
        jid = await sj.enqueue(db, sid)
        await sj.set_step(db, jid, "extracting source 1/3")
        got = await sj.get(db, jid)
        assert got.step == "extracting source 1/3"
        await sj.complete(db, jid)
        assert (await sj.get(db, jid)).state == SpeakerJobState.DONE
        jid2 = await sj.enqueue(db, sid)
        await sj.fail(db, jid2, "boom")
        failed = await sj.get(db, jid2)
        assert failed.state == SpeakerJobState.FAILED and failed.error_message == "boom"
    _run(go())


def test_reset_orphaned_running(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Jo J")
        jid = await sj.enqueue(db, sid)
        await sj.claim_next(db)   # -> running
        await sj.reset_orphaned_running(db)
        assert (await sj.get(db, jid)).state == SpeakerJobState.PENDING
    _run(go())


def test_enqueue_dedups_pending(db):
    """Re-enqueueing while a job is pending/running must NOT create a second row."""
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Kate K")
        jid1 = await sj.enqueue(db, sid)
        jid2 = await sj.enqueue(db, sid)   # duplicate while pending
        assert jid2 == jid1, "second enqueue must return the existing job id"
        cur = await db.execute(
            "SELECT COUNT(*) FROM speaker_jobs "
            "WHERE speaker_id=? AND state IN ('pending','running')",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == 1, "only one pending/running job must exist"
    _run(go())


def test_enqueue_after_completion_allowed(db):
    """After a job completes, a new enqueue for the same speaker IS allowed."""
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Leo L")
        jid1 = await sj.enqueue(db, sid)
        job = await sj.claim_next(db)   # -> running
        await sj.complete(db, job.id)   # -> done
        jid2 = await sj.enqueue(db, sid)
        assert jid2 != jid1, "a new job must be created after completion"
        new_job = await sj.get(db, jid2)
        assert new_job.state == SpeakerJobState.PENDING
    _run(go())
