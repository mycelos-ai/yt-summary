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


def test_speaker_page_shows_candidates_block_separately(app_client):
    db = app_client.app.state.db
    from app.repos import speakers as speakers_repo
    from app.repos import speaker_source_candidates as cand

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Uma U")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) VALUES ('wU',1,'web','u','Uma U on X')"
        )
        await cand.upsert_pending(db, speaker_id=sid, source_id="wU",
                                  signal="title_match", score=0.4)
        await db.commit()
        return sid
    sid = asyncio.get_event_loop().run_until_complete(setup())

    html = app_client.get(f"/speaker/{sid}").text
    assert "Possible sources" in html
    # The candidate block must be distinct from the confirmed-sources block.
    assert 'speaker-candidates' in html


def test_track_record_peek_renders_claim_links(app_client):
    db = app_client.app.state.db
    from app.repos import speakers as speakers_repo
    from app.repos import speaker_claim_embeddings as cve

    async def setup():
        sid = await speakers_repo.resolve_speaker(db, name="Vic V")
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title, transcript) "
            "VALUES ('yV',1,'youtube','http://y/yV','Pod', 't')"
        )
        cur = await db.execute(
            "INSERT INTO speaker_claims (user_id, speaker_id, source_id, claim, "
            "evidence_start_s, extraction_method) VALUES (1,?, 'yV', "
            "'Markets are cyclical', 742, 'llm')", (sid,)
        )
        await db.commit()
        from app.services.embeddings_local import embed_text
        await cve.upsert_claim_embedding(db, cur.lastrowid, await embed_text("Markets are cyclical"))
        # Link the speaker to the video so the persona chat surface renders.
        await db.execute(
            "INSERT INTO source_speakers (source_id, speaker_id, detection_source) "
            "VALUES ('yV',?, 'manual')", (sid,)
        )
        await db.commit()
        return sid
    sid = asyncio.get_event_loop().run_until_complete(setup())

    # The video detail page hosts the persona chat + peek.
    html = app_client.get("/v/yV").text
    assert "What" in html and "said before" in html      # peek heading
    assert "Markets are cyclical" in html                 # claim line
    assert "742" in html or "12:22" in html               # timestamp deep-link
