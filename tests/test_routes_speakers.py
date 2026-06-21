import asyncio

from fastapi.testclient import TestClient

from app.main import create_app


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    return app, TestClient(app)


async def _seed_video(db, vid="vc1", user_id=1):
    from app.repos import videos as videos_repo
    from app.models import TranscriptSource
    # Use the real Lex Fridman channel_id so the already-seeded known_show
    # (inserted by seed_known_shows at schema-init) matches — avoids a
    # UNIQUE constraint error on (name) WHERE user_id IS NULL.
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="Elon Musk: Mars | Lex Fridman Podcast #1",
        description="", thumbnail_path=None, duration_seconds=None, user_id=user_id,
        channel_id="UCSHZKyawb77ixDdsGog4iWA",
    )
    await videos_repo.set_transcript(db, vid, "body", TranscriptSource.MANUAL_SUBS)
    # Set a summary so the chat section (and chips) render
    await videos_repo.set_summary(db, vid, "Test summary", "test-model")
    await db.commit()


def test_detect_links_and_renders_chips(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        resp = client.post("/v/vc1/speakers/detect")
        assert resp.status_code == 200
        assert "Lex Fridman" in resp.text
        assert "Elon Musk" in resp.text


def test_manual_add_creates_chip(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        resp = client.post("/v/vc1/speakers", data={"name": "Guest Person"})
        assert resp.status_code == 200
        assert "Guest Person" in resp.text


def test_unlink_removes_chip(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Temp Person"})

        async def sid():
            from app.repos import speakers as sp_repo
            s = await sp_repo.resolve_speaker(app.state.db, name="Temp Person")
            return s
        speaker_id = _run(sid())
        resp = client.post(f"/v/vc1/speakers/{speaker_id}/unlink")
        assert resp.status_code == 200
        assert "Temp Person" not in resp.text


def test_detect_foreign_video_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db, vid="vforeign", user_id=999))
        resp = client.post("/v/vforeign/speakers/detect")
        assert resp.status_code == 404


def test_add_foreign_video_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db, vid="vforeign", user_id=999))
        resp = client.post("/v/vforeign/speakers", data={"name": "Anyone"})
        assert resp.status_code == 404


def test_unlink_foreign_video_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db, vid="vforeign", user_id=999))
        resp = client.post("/v/vforeign/speakers/42/unlink")
        assert resp.status_code == 404


def test_add_blank_name_rejected(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))

        # Get speaker count before
        async def count_speakers():
            from app.repos import source_speakers as ss_repo
            speakers = await ss_repo.list_for_source(app.state.db, "vc1")
            return len(speakers)

        count_before = _run(count_speakers())

        # Attempt to add blank name
        resp = client.post("/v/vc1/speakers", data={"name": "   "})
        assert resp.status_code == 400

        # Verify no speaker was created
        count_after = _run(count_speakers())
        assert count_before == count_after


def test_detail_page_shows_chips_after_detection(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers/detect")          # creates the links
        page = client.get("/v/vc1")                     # full detail page
        assert page.status_code == 200
        assert 'id="speaker-chips"' in page.text
        assert "Lex Fridman" in page.text
