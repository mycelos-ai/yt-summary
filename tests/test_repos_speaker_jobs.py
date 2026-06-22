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
