import logging

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.heartbeat import HeartbeatRegistry


def test_app_state_exposes_heartbeats_and_log_buffer(tmp_path, monkeypatch):
    """The diagnostics page reads these off app.state; lifespan must set them."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        # TestClient enters lifespan on __enter__.
        assert hasattr(app.state, "heartbeats")
        assert isinstance(app.state.heartbeats, HeartbeatRegistry)
        assert hasattr(app.state, "log_buffer")
        # And the log buffer is wired to the root logger.
        root = logging.getLogger()
        assert app.state.log_buffer in root.handlers
    # After shutdown, the handler must be removed so the next
    # create_app() doesn't accumulate handlers.
    assert app.state.log_buffer not in logging.getLogger().handlers


def test_log_buffer_captures_emitted_lines(tmp_path, monkeypatch):
    """Smoke: any logger.info() should land in the ring buffer."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        logging.getLogger("yt_summary.test").info("hello-from-test")
        lines = app.state.log_buffer.snapshot()
        assert any("hello-from-test" in line for line in lines)


def test_get_diagnostics_renders_page(tmp_path, monkeypatch):
    """GET /settings/diagnostics returns 200 with all the section
    headers a fresh install should show."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200
    text = resp.text
    # Three worker rows by name.
    assert "summary_worker" in text or "Summary-Worker" in text
    assert "tts_worker" in text or "TTS-Worker" in text
    assert "scheduler" in text or "Scheduler" in text
    # Both queue cards.
    assert "Summary-Queue" in text or "summary queue" in text.lower()
    assert "TTS-Queue" in text or "tts queue" in text.lower()
    # Log tail block.
    assert "Log" in text


def test_get_diagnostics_with_no_data_does_not_crash(tmp_path, monkeypatch):
    """Empty queues, empty log buffer, no heartbeats → still renders."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200


def test_get_diagnostics_shows_queued_video_title(tmp_path, monkeypatch):
    """A pending job should appear in the 'Als Nächstes' list."""
    import asyncio

    from app.repos import jobs as jobs_repo
    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vQ", url="u", title="MyQueuedVideo",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await jobs_repo.enqueue(app.state.db, "vQ")
        # Match the get_event_loop().run_until_complete() pattern used
        # throughout the existing test suite. asyncio.run() would close
        # the loop and break subsequent get_event_loop() callers in the
        # same pytest session — that's why a handful of route tests
        # rely on the shared default loop.
        asyncio.get_event_loop().run_until_complete(seed())
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200
    assert "MyQueuedVideo" in resp.text


def test_post_retry_job_resets_failed_to_pending(tmp_path, monkeypatch):
    import asyncio

    from app.models import JobState
    from app.repos import jobs as jobs_repo
    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            jid = await jobs_repo.enqueue(app.state.db, "v1")
            await jobs_repo.fail(app.state.db, jid, "oops")
            return jid
        jid = asyncio.get_event_loop().run_until_complete(seed())

        resp = client.post(
            f"/settings/diagnostics/retry-job/{jid}", follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            j = await jobs_repo.get(app.state.db, jid)
            return j
        job = asyncio.get_event_loop().run_until_complete(check())
        assert job is not None
        assert job.state is JobState.PENDING


def test_post_retry_job_unknown_id_returns_404(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/diagnostics/retry-job/99999", follow_redirects=False,
        )
    assert resp.status_code == 404


def test_post_delete_job_removes_failed_row(tmp_path, monkeypatch):
    import asyncio

    from app.repos import jobs as jobs_repo
    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            jid = await jobs_repo.enqueue(app.state.db, "v1")
            await jobs_repo.fail(app.state.db, jid, "boom")
            return jid
        jid = asyncio.get_event_loop().run_until_complete(seed())

        resp = client.post(
            f"/settings/diagnostics/delete-job/{jid}", follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            return await jobs_repo.get(app.state.db, jid)
        assert asyncio.get_event_loop().run_until_complete(check()) is None


def test_post_retry_tts_preserves_translated_text(tmp_path, monkeypatch):
    import asyncio

    from app.repos import tts_jobs as r
    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            j = await r.enqueue(
                app.state.db, "v1", "summary", "de", "v", "low",
            )
            await r.set_translated_text(app.state.db, j.id, "Hallo Welt")
            await r.fail(app.state.db, j.id, "render crashed")
            return j.id
        jid = asyncio.get_event_loop().run_until_complete(seed())

        resp = client.post(
            f"/settings/diagnostics/retry-tts/{jid}", follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            return await r.get(app.state.db, jid)
        fresh = asyncio.get_event_loop().run_until_complete(check())
        assert fresh is not None
        assert fresh.status == "queued"
        assert fresh.translated_text == "Hallo Welt"


def test_post_delete_tts_removes_row(tmp_path, monkeypatch):
    import asyncio

    from app.repos import tts_jobs as r
    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            j = await r.enqueue(
                app.state.db, "v1", "summary", "de", "v", "low",
            )
            await r.fail(app.state.db, j.id, "x")
            return j.id
        jid = asyncio.get_event_loop().run_until_complete(seed())

        resp = client.post(
            f"/settings/diagnostics/delete-tts/{jid}", follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            return await r.get(app.state.db, jid)
        assert asyncio.get_event_loop().run_until_complete(check()) is None


def test_post_tick_scheduler_returns_303(tmp_path, monkeypatch):
    """POST /tick-scheduler wakes the PlaylistScheduler.

    We assert the 303 redirect, not the _tick_requested flag, because
    the running scheduler may already have woken and cleared the flag
    by the time the assertion runs. The wake-up behaviour itself is
    proven by tests/test_scheduler.py::test_request_tick_wakes_a_long_sleep.
    """
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/diagnostics/tick-scheduler", follow_redirects=False,
        )
        assert resp.status_code == 303
