import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    from app.main import create_app
    app = create_app()
    with TestClient(app) as client:
        yield client


def test_confirm_promotes_candidate_to_link(app_client):
    app = app_client.app
    db = app.state.db
    from app.repos import speaker_source_candidates as cand
    from app.repos import speakers as speakers_repo

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Rita R")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('wR',1,'web','u','Rita R interview')"
        )
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="wR",
                                        signal="title_match", score=0.4)
        await db.commit()
        return sid, cid
    sid, cid = asyncio.get_event_loop().run_until_complete(setup())

    r = app_client.post(f"/speaker/{sid}/candidates/{cid}/confirm")
    assert r.status_code == 200

    async def check():
        cur = await db.execute(
            "SELECT detection_source FROM source_speakers "
            "WHERE speaker_id=? AND source_id='wR'", (sid,)
        )
        row = await cur.fetchone()
        assert row is not None and row["detection_source"] == "manual"
        from app.repos import speaker_source_candidates as c2
        assert (await c2.get(db, cid)).state == "confirmed"
    asyncio.get_event_loop().run_until_complete(check())


def test_dismiss_sets_state(app_client):
    db = app_client.app.state.db
    from app.repos import speaker_source_candidates as cand
    from app.repos import speakers as speakers_repo

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Sam S")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) VALUES ('wS',1,'web','u','t')"
        )
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="wS",
                                        signal="fulltext", score=0.3)
        await db.commit()
        return sid, cid
    sid, cid = asyncio.get_event_loop().run_until_complete(setup())

    r = app_client.post(f"/speaker/{sid}/candidates/{cid}/dismiss")
    assert r.status_code == 200

    async def check():
        from app.repos import speaker_source_candidates as c2
        assert (await c2.get(db, cid)).state == "dismissed"
    asyncio.get_event_loop().run_until_complete(check())


def test_candidates_foreign_profile_404(app_client):
    db = app_client.app.state.db
    from app.repos import speakers as speakers_repo

    async def setup():
        # Speaker owned by user 2; default request acts as user 1.
        return await speakers_repo.resolve_speaker(db, user_id=2, name="Tom T")
    sid = asyncio.get_event_loop().run_until_complete(setup())

    assert app_client.get(f"/speaker/{sid}/candidates").status_code == 404


def test_confirm_rejects_candidate_belonging_to_other_speaker(app_client):
    """Cross-speaker guard: a candidate owned by speaker A cannot be confirmed
    under speaker B's URL, even if both speakers belong to the same user."""
    db = app_client.app.state.db
    from app.repos import speaker_source_candidates as cand
    from app.repos import speakers as speakers_repo

    async def setup():
        sid_a = await speakers_repo.resolve_speaker(db, name="Alice A")
        sid_b = await speakers_repo.resolve_speaker(db, name="Bob B")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) VALUES ('wA',1,'web','u','t')"
        )
        cid = await cand.upsert_pending(db, speaker_id=sid_a, source_id="wA",
                                        signal="title_match", score=0.5)
        await db.commit()
        return sid_b, cid  # cid belongs to A, posted under B

    sid_b, cid = asyncio.get_event_loop().run_until_complete(setup())
    assert app_client.post(f"/speaker/{sid_b}/candidates/{cid}/confirm").status_code == 404

    async def check_no_link():
        cur = await db.execute(
            "SELECT 1 FROM source_speakers WHERE speaker_id=?", (sid_b,)
        )
        row = await cur.fetchone()
        assert row is None, "confirm must not have written a source_speakers link for speaker B"
    asyncio.get_event_loop().run_until_complete(check_no_link())


def test_dismiss_rejects_candidate_belonging_to_other_speaker(app_client):
    """Cross-speaker guard: a candidate owned by speaker A cannot be dismissed
    under speaker B's URL, even if both speakers belong to the same user."""
    db = app_client.app.state.db
    from app.repos import speaker_source_candidates as cand
    from app.repos import speakers as speakers_repo

    async def setup():
        sid_a = await speakers_repo.resolve_speaker(db, name="Carol C")
        sid_b = await speakers_repo.resolve_speaker(db, name="Dave D")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) VALUES ('wC',1,'web','u','t')"
        )
        cid = await cand.upsert_pending(db, speaker_id=sid_a, source_id="wC",
                                        signal="fulltext", score=0.4)
        await db.commit()
        return sid_b, cid  # cid belongs to A, posted under B

    sid_b, cid = asyncio.get_event_loop().run_until_complete(setup())
    assert app_client.post(f"/speaker/{sid_b}/candidates/{cid}/dismiss").status_code == 404


def test_confirm_shows_not_extracted_note(app_client):
    """Confirming a candidate returns a fragment that includes a note telling
    the user that claims aren't extracted automatically."""
    db = app_client.app.state.db
    from app.repos import speaker_source_candidates as cand
    from app.repos import speakers as speakers_repo

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Eve E")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('wE',1,'web','u','Eve E talk')"
        )
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="wE",
                                        signal="title_match", score=0.6)
        await db.commit()
        return sid, cid

    sid, cid = asyncio.get_event_loop().run_until_complete(setup())

    r = app_client.post(f"/speaker/{sid}/candidates/{cid}/confirm")
    assert r.status_code == 200
    assert "Extract" in r.text, "confirm response must contain the not-extracted note"

    # Existing confirm behavior must still hold
    async def check():
        cur = await db.execute(
            "SELECT detection_source FROM source_speakers "
            "WHERE speaker_id=? AND source_id='wE'", (sid,)
        )
        row = await cur.fetchone()
        assert row is not None and row["detection_source"] == "manual"
        from app.repos import speaker_source_candidates as c2
        assert (await c2.get(db, cid)).state == "confirmed"

    asyncio.get_event_loop().run_until_complete(check())


def test_dismiss_has_no_note(app_client):
    """Dismissing a candidate returns a fragment WITHOUT the not-extracted note."""
    db = app_client.app.state.db
    from app.repos import speaker_source_candidates as cand
    from app.repos import speakers as speakers_repo

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Frank F")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('wF',1,'web','u','Frank F talk')"
        )
        cid = await cand.upsert_pending(db, speaker_id=sid, source_id="wF",
                                        signal="title_match", score=0.5)
        await db.commit()
        return sid, cid

    sid, cid = asyncio.get_event_loop().run_until_complete(setup())

    r = app_client.post(f"/speaker/{sid}/candidates/{cid}/dismiss")
    assert r.status_code == 200
    assert "aren't extracted" not in r.text, (
        "dismiss response must NOT contain the not-extracted note"
    )
