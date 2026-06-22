"""Tests for speaker source candidate repo (T6) and discovery service (T7)."""

import asyncio

from app.repos import speaker_source_candidates as cand
from app.repos import speakers as speakers_repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _video(db, vid, kind="web", title="t", url="u", description=""):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, description) "
        "VALUES (?,1,?,?,?,?)",
        (vid, kind, url, title, description),
    )
    await db.commit()


def test_upsert_pending_is_idempotent(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Kim K")
        await _video(db, "v1")
        a = await cand.upsert_pending(db, speaker_id=sid, source_id="v1",
                                      signal="title_match", score=0.4)
        b = await cand.upsert_pending(db, speaker_id=sid, source_id="v1",
                                      signal="title_match", score=0.6)
        assert a == b   # UNIQUE(speaker_id, source_id) — one row, score updated
        rows = await cand.list_for_speaker(db, sid)
        assert len(rows) == 1 and rows[0].state == "pending"
    _run(go())


def test_set_state(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Lou L")
        await _video(db, "v2")
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="v2",
                                        signal="email_from", score=0.9)
        await cand.set_state(db, cid, "dismissed")
        assert (await cand.get(db, cid)).state == "dismissed"
        assert await cand.list_for_speaker(db, sid, state="pending") == []
    _run(go())


def test_dismissed_stays_dismissed_on_reupsert(db):
    """A re-discovered candidate that was dismissed must STAY dismissed."""
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Pat P")
        await _video(db, "v3")
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="v3",
                                        signal="title_match", score=0.5)
        await cand.set_state(db, cid, "dismissed")
        # Re-upsert — discovery fires again for same (speaker, source)
        cid2 = await cand.upsert_pending(db, speaker_id=sid, source_id="v3",
                                         signal="embedding", score=0.8)
        assert cid == cid2, "must return the same row id"
        row = await cand.get(db, cid)
        assert row.state == "dismissed", "state must not be reset by re-upsert"
        assert row.signal == "embedding", "signal should be updated"
        assert row.score == 0.8, "score should be updated"
    _run(go())


def test_list_for_speaker_orders_by_score_desc(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Sam S")
        await _video(db, "v4")
        await _video(db, "v5", url="u5")
        await cand.upsert_pending(db, speaker_id=sid, source_id="v4",
                                  signal="fulltext", score=0.3)
        await cand.upsert_pending(db, speaker_id=sid, source_id="v5",
                                  signal="fulltext", score=0.7)
        rows = await cand.list_for_speaker(db, sid)
        assert rows[0].score == 0.7
        assert rows[1].score == 0.3
    _run(go())


def test_set_state_rejects_invalid_state(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Val V")
        await _video(db, "v6", url="u6")
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="v6",
                                        signal="embedding", score=0.5)
        try:
            await cand.set_state(db, cid, "approved")  # not a valid state
            assert False, "expected ValueError"
        except ValueError:
            pass
    _run(go())


# --- T7: speaker_discovery ---

from app.services import speaker_discovery  # noqa: E402


def test_title_match_creates_pending_candidate_not_link(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Morgan Housel")
        await _video(db, "wA", kind="web",
                     title="An interview with Morgan Housel on risk", url="http://x/a")
        ids = await speaker_discovery.discover_candidates(db, sid)
        assert ids, "title match should yield a candidate"
        rows = await cand.list_for_speaker(db, sid)
        assert any(r.source_id == "wA" and r.signal in {"title_match", "fulltext"}
                   for r in rows)
        # CRITICAL: nothing was auto-linked into the dossier.
        link = await db.execute(
            "SELECT COUNT(*) FROM source_speakers WHERE speaker_id=?", (sid,)
        )
        assert (await link.fetchone())[0] == 0
    _run(go())


def test_email_from_is_a_strong_signal(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Packy McCormick")
        # Seed an email-kind video the way real ingest stores it:
        # sender name+addr go in DESCRIPTION as "From {name} <{addr}>".
        await _video(db, "eA", kind="email", title="Not Boring: weekly",
                     description="From Packy McCormick <packy@notboring.co>", url="")
        ids = await speaker_discovery.discover_candidates(db, sid)
        rows = await cand.list_for_speaker(db, sid)
        em = [r for r in rows if r.signal == "email_from"]
        assert em, "newsletter sender should produce an email_from candidate"
        # Strong signal => higher score than a weak title hit.
        assert (em[0].score or 0) >= 0.7
    _run(go())


def test_discovery_never_touches_youtube(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Lex Fridman")
        await _video(db, "yA", kind="youtube", title="Lex Fridman Podcast #1",
                     url="http://y/yA")
        await speaker_discovery.discover_candidates(db, sid)
        rows = await cand.list_for_speaker(db, sid)
        assert all(r.source_id != "yA" for r in rows), \
            "youtube is handled by show-match, not discovery candidates"
    _run(go())
