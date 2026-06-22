"""Tests for speaker_backfill service (Task 8, PR 4).

Key invariants:
- run_backfill reads CONFIRMED sources only (source_speakers links).
- Pending candidates in speaker_source_candidates are NEVER extracted.
- activate() flips is_active AND enqueues a backfill job.
"""
import asyncio

from app.repos import speaker_source_candidates as cand
from app.repos import speakers as speakers_repo
from app.services import speaker_backfill


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _video(db, vid, kind="youtube"):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
        "VALUES (?,1,?,?,?,?)",
        (vid, kind, f"https://example.com/{vid}", vid, "transcript"),
    )
    await db.commit()


async def _link(db, sid, vid):
    """Manually confirm a speaker-source link (detection_source='manual')."""
    from app.repos import source_speakers as ss_repo
    await ss_repo.link_speaker(db, vid, sid, detection_source="manual")


def test_backfill_extracts_confirmed_sources(db, monkeypatch):
    """run_backfill must call extract_claims_for_source once per confirmed source."""
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Nate N")
        await _video(db, "c1")
        await _video(db, "c2")
        await _link(db, sid, "c1")
        await _link(db, sid, "c2")

        calls = []

        async def fake_extract(db_, source, speaker_ids, **kw):
            calls.append(source.id)
            return []

        monkeypatch.setattr(
            "app.services.speaker_backfill.extract_claims_for_source", fake_extract
        )
        n = await speaker_backfill.run_backfill(
            db, sid, model="m", api_key="", base_url=None
        )
        assert n == 2
        assert set(calls) == {"c1", "c2"}

    _run(go())


def test_backfill_ignores_candidates(db, monkeypatch):
    """Pending candidates must NOT be extracted — only confirmed source_speakers."""
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Olive O")
        await _video(db, "confirmed")
        await _video(db, "guess", kind="web")
        await _link(db, sid, "confirmed")
        # A pending candidate must NOT be extracted by the backfill.
        await cand.upsert_pending(
            db, speaker_id=sid, source_id="guess",
            signal="title_match", score=0.4,
        )

        calls = []

        async def fake_extract(db_, source, speaker_ids, **kw):
            calls.append(source.id)
            return []

        monkeypatch.setattr(
            "app.services.speaker_backfill.extract_claims_for_source", fake_extract
        )
        await speaker_backfill.run_backfill(
            db, sid, model="m", api_key="", base_url=None
        )
        assert calls == ["confirmed"], "backfill must ignore candidate sources"

    _run(go())


def test_activate_enqueues_backfill(db):
    """speakers_svc.activate must flip is_active AND enqueue a backfill job."""
    async def go():
        from app.repos import speaker_jobs as sj
        from app.services import speakers as speakers_svc

        sid = await speakers_repo.resolve_speaker(db, name="Pam P")
        await speakers_svc.activate(db, sid)
        job = await sj.latest_for_speaker(db, sid)
        assert job is not None and job.state.value in {"pending", "running"}
        sp = await speakers_repo.get_speaker(db, sid)
        assert sp.is_active is True

    _run(go())


def test_run_pending_backfills_drains_queue(db, monkeypatch):
    async def go():
        from app.repos import speaker_jobs as sj
        sid = await speakers_repo.resolve_speaker(db, name="Quinn Q")
        await _video(db, "qv"); await _link(db, sid, "qv")
        await sj.enqueue(db, sid)

        async def fake_extract(db_, source, speaker_ids, **kw):
            return []
        monkeypatch.setattr(
            "app.services.speaker_backfill.extract_claims_for_source", fake_extract
        )
        n = await speaker_backfill.run_pending_backfills(
            db, model="m", api_key="", base_url=None, limit=5
        )
        assert n == 1
        job = await sj.latest_for_speaker(db, sid)
        assert job.state.value == "done"
    _run(go())
