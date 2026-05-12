"""HTMX modal + render/status/delete/file routes for TTS audio.

The tests follow the established sync TestClient + run_until_complete
pattern from ``tests/test_routes_videos.py`` / ``tests/test_routes_chat.py``.
``_seed_*`` helpers run on the same event loop as the TestClient
lifespan, so ``app.state.db`` is populated by the time we touch it.
"""
import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import TranscriptSource


def _await(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _seed_video(app, *, id: str, source_language: str | None = None):
    """Insert a video owned by user 1 (matches default cookie)."""
    from app.repos import videos as videos_repo

    async def setup():
        await videos_repo.upsert_metadata(
            app.state.db,
            video_id=id,
            url=f"https://yt/{id}",
            title="T",
            description="",
            thumbnail_path=None,
            duration_seconds=60,
        )
        if source_language is not None:
            # set_transcript with `language=...` also writes source_language
            # per the Task 7 migration semantics — exactly what the modal
            # uses as the default target_language fallback.
            await videos_repo.set_transcript(
                app.state.db, id, "x", TranscriptSource.AUTO_SUBS,
                language=source_language,
            )

    _await(setup())


def _seed_summary(app, *, video_id: str, text: str, language: str | None = None):
    from app.repos import videos as videos_repo

    async def setup():
        await videos_repo.set_summary(
            app.state.db, video_id, text, "gpt-4o", language=language,
        )

    _await(setup())


def _seed_tts_job(
    app,
    *,
    video_id: str,
    source: str,
    target_language: str,
    voice: str,
    quality: str,
    status: str = "queued",
    step: str | None = None,
    audio_path: str | None = None,
) -> int:
    """Raw SQL insert so we can pre-stage rows in non-queued states
    (the repo's enqueue only knows how to land at 'queued')."""
    async def setup():
        cursor = await app.state.db.execute(
            """
            INSERT INTO tts_jobs (
                video_id, source, target_language, voice, quality,
                status, step, audio_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (video_id, source, target_language, voice, quality,
             status, step, audio_path),
        )
        await app.state.db.commit()
        return cursor.lastrowid

    return _await(setup())


# -------------------------------------------------------------------- tests


def test_audio_modal_renders_form_for_video(tmp_path, monkeypatch):
    """GET /v/{id}/audio returns the modal form with voice options."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en")
        resp = client.get("/v/abc/audio")
    assert resp.status_code == 200
    assert "thorsten" in resp.text  # de voice available in catalogue
    assert "lessac" in resp.text    # en voice available
    assert "<select" in resp.text


def test_render_endpoint_enqueues_job_returns_polling_block(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en")
        _seed_summary(app, video_id="abc", text="Hi.", language="en")
        resp = client.post(
            "/v/abc/audio/render",
            data={"source": "summary", "target_language": "de",
                  "voice": "thorsten", "quality": "medium"},
            follow_redirects=False,
        )
    assert resp.status_code == 200
    # Response is the polling fragment
    assert "every 2s" in resp.text
    # The fresh job is in 'queued' (worker may not have claimed yet)
    # or 'translating' (if it did). Either is fine.
    body_lower = resp.text.lower()
    assert "queued" in body_lower or "translating" in body_lower or "preparing" in body_lower


def test_render_endpoint_returns_cached_done_block_when_repeated(tmp_path, monkeypatch):
    """Second submit with the same params should return the existing
    done block immediately, with the audio player."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc", source_language="en")
        _seed_summary(app, video_id="abc", text="Hi.", language="en")
        # Pre-fabricate a done job. enqueue's UPSERT conflict clause
        # will return this existing row rather than inserting a new one.
        _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten",
            quality="medium", status="done",
            audio_path="tts-audio/abc/summary-de-thorsten-medium.mp3",
        )
        resp = client.post(
            "/v/abc/audio/render",
            data={"source": "summary", "target_language": "de",
                  "voice": "thorsten", "quality": "medium"},
        )
    assert "<audio" in resp.text
    assert "/audio/file/" in resp.text  # download link present


def test_status_endpoint_returns_current_step(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        # 'rendering' is the in-progress status that holds a step label —
        # the schema only allows queued/translating/rendering/done/failed.
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="rendering", step="rendering audio",
        )
        resp = client.get(f"/v/abc/audio/status/{job_id}")
    assert resp.status_code == 200
    assert "rendering audio" in resp.text
    # Still polling
    assert "every 2s" in resp.text


def test_status_endpoint_stops_polling_when_done(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done",
            audio_path="tts-audio/abc/summary-de-thorsten-medium.mp3",
        )
        resp = client.get(f"/v/abc/audio/status/{job_id}")
    assert "<audio" in resp.text
    assert "every 2s" not in resp.text


def test_delete_rendering_removes_row_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    rel = "tts-audio/abc/summary-de-thorsten-medium.mp3"
    mp3 = tmp_path / rel
    mp3.parent.mkdir(parents=True)
    mp3.write_bytes(b"x")
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done", audio_path=rel,
        )
        resp = client.post(
            f"/v/abc/audio/{job_id}/delete", follow_redirects=False,
        )
    assert resp.status_code == 200
    assert not mp3.exists()


def test_audio_file_endpoint_serves_mp3(tmp_path, monkeypatch):
    """GET /v/{id}/audio/file/{job_id} → 200, content-type audio/mpeg."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    rel = "tts-audio/abc/summary-de-thorsten-medium.mp3"
    mp3 = tmp_path / rel
    mp3.parent.mkdir(parents=True)
    mp3.write_bytes(b"ID3FAKE")
    with TestClient(app) as client:
        _seed_video(app, id="abc")
        job_id = _seed_tts_job(
            app, video_id="abc", source="summary",
            target_language="de", voice="thorsten", quality="medium",
            status="done", audio_path=rel,
        )
        resp = client.get(f"/v/abc/audio/file/{job_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/")
    assert resp.content == b"ID3FAKE"
