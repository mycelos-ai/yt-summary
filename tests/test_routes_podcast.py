"""Route tests for the personal podcast feed (Part B)."""

import asyncio

from fastapi.testclient import TestClient

from app.main import create_app


def _seed_rendering(app, *, vid, job_audio_rel, finished="2026-06-05 10:00:00",
                    user_id=1, write_file=True):
    """Seed a video + a done tts_job, and (optionally) write a fake mp3."""
    async def setup():
        from app.repos import videos as videos_repo
        await videos_repo.upsert_metadata(
            app.state.db, video_id=vid, url=f"https://youtu.be/{vid}",
            title=f"Title {vid}", description="d", thumbnail_path=None,
            duration_seconds=None, user_id=user_id,
        )
        await videos_repo.set_summary(app.state.db, vid, "a summary", "m")
        cur = await app.state.db.execute(
            """
            INSERT INTO tts_jobs (
                video_id, source, target_language, voice, quality,
                status, audio_path, duration_seconds, finished_at
            ) VALUES (?, 'summary', 'en', 'amy', 'medium', 'done', ?, 30.0, ?)
            """,
            (vid, job_audio_rel, finished),
        )
        await app.state.db.commit()
        return cur.lastrowid
    job_id = asyncio.get_event_loop().run_until_complete(setup())
    if write_file:
        mp3 = app.state.config.tts_audio_dir / job_audio_rel.split("tts-audio/")[-1]
        mp3.parent.mkdir(parents=True, exist_ok=True)
        mp3.write_bytes(b"ID3fakeaudio" * 10)
    return job_id


def _enable_token(app, user_id=1):
    async def setup():
        from app.repos import users as users_repo
        return await users_repo.set_podcast_token(app.state.db, user_id)
    return asyncio.get_event_loop().run_until_complete(setup())


def test_feed_returns_xml_for_valid_token(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        token = _enable_token(app)
        _seed_rendering(app, vid="v1", job_audio_rel="tts-audio/v1/summary.mp3")
        resp = client.get(f"/podcast/{token}/feed.xml")
    assert resp.status_code == 200
    assert "application/" in resp.headers["content-type"] or \
        "xml" in resp.headers["content-type"]
    assert "<rss" in resp.text
    assert "Title v1" in resp.text
    assert f"/podcast/{token}/episode/" in resp.text


def test_feed_404_for_unknown_token(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/podcast/nope-not-a-token/feed.xml")
    assert resp.status_code == 404


def test_episode_streams_mp3_for_valid_token(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        token = _enable_token(app)
        job_id = _seed_rendering(
            app, vid="v1", job_audio_rel="tts-audio/v1/summary.mp3",
        )
        resp = client.get(f"/podcast/{token}/episode/{job_id}.mp3")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content.startswith(b"ID3")


def test_episode_404_for_unknown_token(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _enable_token(app)  # a token exists, but we request with a wrong one
        job_id = _seed_rendering(
            app, vid="v1", job_audio_rel="tts-audio/v1/summary.mp3",
        )
        resp = client.get(f"/podcast/wrong-token/episode/{job_id}.mp3")
    assert resp.status_code == 404


def test_episode_404_when_job_belongs_to_other_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        token = _enable_token(app, user_id=1)
        # rendering belongs to user 2 — must not be reachable via user 1's token
        job_id = _seed_rendering(
            app, vid="v2", job_audio_rel="tts-audio/v2/summary.mp3", user_id=2,
        )
        resp = client.get(f"/podcast/{token}/episode/{job_id}.mp3")
    assert resp.status_code == 404


def test_episode_supports_range_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        token = _enable_token(app)
        job_id = _seed_rendering(
            app, vid="v1", job_audio_rel="tts-audio/v1/summary.mp3",
        )
        resp = client.get(
            f"/podcast/{token}/episode/{job_id}.mp3",
            headers={"Range": "bytes=0-4"},
        )
    # FileResponse honours Range → 206 Partial Content.
    assert resp.status_code == 206
    assert len(resp.content) == 5


def test_settings_enable_podcast_creates_token(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/settings/podcast/enable", follow_redirects=False)
        assert resp.status_code == 303

        async def token():
            from app.repos import users as users_repo
            u = await users_repo.get_by_id(app.state.db, 1)
            return u.podcast_token
        tok = asyncio.get_event_loop().run_until_complete(token())
    assert tok  # a token now exists


def test_settings_disable_podcast_clears_token(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _enable_token(app)
        resp = client.post("/settings/podcast/disable", follow_redirects=False)
        assert resp.status_code == 303

        async def token():
            from app.repos import users as users_repo
            u = await users_repo.get_by_id(app.state.db, 1)
            return u.podcast_token
        tok = asyncio.get_event_loop().run_until_complete(token())
    assert tok is None


def test_settings_page_shows_feed_url_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        token = _enable_token(app)
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert f"/podcast/{token}/feed.xml" in resp.text
