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
    from app.repos import speakers as speakers_repo
    from app.repos import speaker_source_candidates as cand

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
    from app.repos import speakers as speakers_repo
    from app.repos import speaker_source_candidates as cand

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
