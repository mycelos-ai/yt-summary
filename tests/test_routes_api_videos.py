from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.youtube import VideoMetadata


def _meta(vid: str = "apivid12345") -> VideoMetadata:
    return VideoMetadata(
        id=vid,
        url=f"https://youtu.be/{vid}",
        title="API Test Video",
        description="d",
        duration_seconds=120,
        thumbnail_url=None,
    )


def test_post_videos_async_returns_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with (
        patch("app.services.api.fetch_metadata", AsyncMock(return_value=_meta())),
        patch("app.services.api.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/api/v1/videos",
            json={"url": "https://youtu.be/apivid12345"},
        )
    assert resp.status_code == 202
    body = resp.json()
    # Composite id under the household admin (id=1)
    assert body["video_id"] == "1:apivid12345"
    assert body["summary_ready"] is False
    assert body["kind"] == "youtube"


def test_post_videos_invalid_url_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/api/v1/videos", json={"url": "ftp://x"})
    assert resp.status_code == 400


def test_get_video_returns_resource(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="getapi1", url="u", title="GotIt",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos/getapi1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "getapi1"
    assert body["title"] == "GotIt"


def test_get_video_404(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/videos/nope")
    assert resp.status_code == 404


def test_list_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            for i in range(3):
                await videos_repo.upsert_metadata(
                    app.state.db, video_id=f"l{i}", url="u", title=f"T{i}",
                    description="", thumbnail_path=None, duration_seconds=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert "videos" in body
    assert len(body["videos"]) == 2


def test_get_summary_404_when_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="nosum", url="u", title="X",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos/nosum/summary")
    assert resp.status_code == 404


def test_get_summary_returns_text_when_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="hassum", url="u", title="X",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(
                app.state.db, "hassum", "## TL;DR\nyes", "model"
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos/hassum/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert "TL;DR" in body["summary"]
    assert body["model"] == "model"


def test_reindex_returns_202(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="rev", url="u", title="X",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/api/v1/videos/rev/reindex")
    assert resp.status_code == 202


def test_api_requires_key_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Generate a key
        client.post("/settings/api-key/generate", follow_redirects=False)
        # Without auth header, GET /api/v1/videos should 401
        resp = client.get("/api/v1/videos")
        assert resp.status_code == 401
        # Health stays open
        resp_h = client.get("/api/v1/health")
        assert resp_h.status_code == 200


def test_api_accepts_valid_bearer_after_key_set(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        gen = client.post("/settings/api-key/generate", follow_redirects=False)
        # Extract plaintext from the reveal page (yts_…)
        import re
        m = re.search(r"(yts_[a-z0-9]+)", gen.text)
        assert m is not None
        plaintext = m.group(1)
        resp = client.get(
            "/api/v1/videos",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------- audio API


def _seed_video_and_summary(app, vid, source_language="en"):
    async def go():
        from app.models import TranscriptSource
        from app.repos import videos as vrepo
        db = app.state.db
        await vrepo.upsert_metadata(
            db, video_id=vid, url=f"https://yt/{vid}",
            title="T", description="", thumbnail_path=None,
            duration_seconds=60,
        )
        await vrepo.set_transcript(
            db, vid, "x", TranscriptSource.AUTO_SUBS, language=source_language
        )
        await vrepo.set_summary(
            db, vid, "Hello.", "gpt-4o", language=source_language
        )
    import asyncio
    asyncio.get_event_loop().run_until_complete(go())


def _seed_tts_job(app, *, video_id, source, target_language, voice, quality,
                  status="queued", audio_path=None):
    async def go():
        db = app.state.db
        cur = await db.execute(
            "INSERT INTO tts_jobs (video_id, source, target_language, voice, "
            "quality, status, audio_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (video_id, source, target_language, voice, quality, status,
             audio_path),
        )
        await db.commit()
        return cur.lastrowid
    import asyncio
    return asyncio.get_event_loop().run_until_complete(go())


def test_api_post_audio_enqueues_and_returns_202(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_and_summary(app, "aud1")
        resp = client.post(
            "/api/v1/videos/aud1/audio",
            json={
                "source": "summary",
                "target_language": "de",
                "voice": "thorsten",
                "quality": "medium",
            },
        )
    assert resp.status_code == 202
    body = resp.json()
    assert isinstance(body["job_id"], int)
    assert body["cached"] is False
    assert body["audio_url"] is None


def test_api_post_audio_returns_200_when_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_and_summary(app, "aud2")
        job_id = _seed_tts_job(
            app, video_id="aud2", source="summary", target_language="de",
            voice="thorsten", quality="medium",
            status="done", audio_path="tts/aud2.mp3",
        )
        resp = client.post(
            "/api/v1/videos/aud2/audio",
            json={
                "source": "summary",
                "target_language": "de",
                "voice": "thorsten",
                "quality": "medium",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["cached"] is True
    assert body["audio_url"] == f"/v/aud2/audio/file/{job_id}"


def test_api_get_audio_status_returns_step_and_url_when_done(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_and_summary(app, "aud3")
        done_id = _seed_tts_job(
            app, video_id="aud3", source="summary", target_language="de",
            voice="thorsten", quality="medium",
            status="done", audio_path="tts/aud3.mp3",
        )
        queued_id = _seed_tts_job(
            app, video_id="aud3", source="transcript", target_language="de",
            voice="thorsten", quality="medium",
            status="queued",
        )
        resp_done = client.get(f"/api/v1/videos/aud3/audio/{done_id}")
        resp_queued = client.get(f"/api/v1/videos/aud3/audio/{queued_id}")
    assert resp_done.status_code == 200
    done_body = resp_done.json()
    assert done_body["status"] == "done"
    assert done_body["audio_url"] == f"/v/aud3/audio/file/{done_id}"
    assert resp_queued.status_code == 200
    queued_body = resp_queued.json()
    assert queued_body["status"] == "queued"
    assert queued_body["audio_url"] is None


def test_api_get_audio_status_404_when_job_belongs_to_other_video(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_and_summary(app, "audA")
        _seed_video_and_summary(app, "audB")
        job_id = _seed_tts_job(
            app, video_id="audA", source="summary", target_language="de",
            voice="thorsten", quality="medium", status="queued",
        )
        resp = client.get(f"/api/v1/videos/audB/audio/{job_id}")
    assert resp.status_code == 404


def test_api_post_audio_404_when_video_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/videos/nope/audio",
            json={
                "source": "summary",
                "target_language": "de",
                "voice": "thorsten",
                "quality": "medium",
            },
        )
    assert resp.status_code == 404


def test_api_post_audio_400_when_source_not_in_set(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_and_summary(app, "audS")
        resp = client.post(
            "/api/v1/videos/audS/audio",
            json={
                "source": "bogus",
                "target_language": "de",
                "voice": "thorsten",
                "quality": "medium",
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "INVALID_SOURCE"


def test_api_post_audio_400_when_voice_invalid_for_language(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_and_summary(app, "audV")
        resp = client.post(
            "/api/v1/videos/audV/audio",
            json={
                "source": "summary",
                "target_language": "de",
                "voice": "lessac",  # en_US voice, not de
                "quality": "medium",
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "INVALID_VOICE"


def test_api_post_audio_400_when_target_language_invalid(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_and_summary(app, "audL")
        resp = client.post(
            "/api/v1/videos/audL/audio",
            json={
                "source": "summary",
                "target_language": "xx",
                "voice": "thorsten",
                "quality": "medium",
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "INVALID_TARGET_LANGUAGE"


def test_api_post_audio_400_when_quality_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_and_summary(app, "audQ")
        resp = client.post(
            "/api/v1/videos/audQ/audio",
            json={
                "source": "summary",
                "target_language": "de",
                "voice": "kerstin",  # only ships "low"
                "quality": "high",
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "INVALID_QUALITY"


def test_api_post_audio_400_when_source_not_ready(tmp_path, monkeypatch):
    """Video exists but the requested source text is empty/NULL."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as vrepo
            await vrepo.upsert_metadata(
                app.state.db, video_id="audN", url="u", title="T",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            "/api/v1/videos/audN/audio",
            json={
                "source": "summary",
                "target_language": "de",
                "voice": "thorsten",
                "quality": "medium",
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "SOURCE_NOT_READY"
