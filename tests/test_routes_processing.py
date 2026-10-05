"""Routes for the per-profile processing view: ``GET /processing``,
its HTMX fragment, and the owner-scoped cancel / retry / dismiss
actions.

The lifespan starts the real summary worker, which would claim any
pending job within a second. Tests here need pending jobs to *stay*
pending, so the worker's loop is replaced with an idle wait that still
honours ``stop()`` on shutdown.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import PROFILE_COOKIE, create_app
from app.models import JobState
from app.repos import jobs as jobs_repo
from app.repos import users as users_repo
from app.repos import videos as videos_repo
from app.worker import Worker


@pytest.fixture(autouse=True)
def _idle_worker(monkeypatch):
    async def _run(self) -> None:
        await self._stopped.wait()
    monkeypatch.setattr(Worker, "run", _run)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def _seed_video(db, vid: str, user_id: int = 1, title: str | None = None):
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title=title or vid, description="",
        thumbnail_path=None, duration_seconds=None, user_id=user_id,
    )


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    return create_app()


# ── GET /processing ──────────────────────────────────────────────


def test_processing_page_renders_empty(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.get("/processing")
    assert resp.status_code == 200
    assert "Processing" in resp.text
    assert "Nothing in progress" in resp.text


def test_processing_page_lists_own_running_pending_failed(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            other = await users_repo.create(db, name="Other")
            await _seed_video(db, "run-1", title="RunningTitle")
            await _seed_video(db, "wait-1", title="WaitingTitle")
            await _seed_video(db, "fail-1", title="FailedTitle")
            await _seed_video(db, "theirs-1", other.id, title="ForeignTitle")
            await jobs_repo.enqueue(db, "run-1")
            await jobs_repo.enqueue(db, "theirs-1")
            await jobs_repo.enqueue(db, "wait-1")
            jf = await jobs_repo.enqueue(db, "fail-1")
            await jobs_repo.fail(db, jf, "transcript unavailable")
            claimed = await jobs_repo.claim_next(db)
            assert claimed is not None
            await jobs_repo.set_step(db, claimed.id, "summarizing chunk 2/4")
        _run(seed())
        resp = client.get("/processing")
    assert resp.status_code == 200
    text = resp.text
    assert "RunningTitle" in text
    assert "Summary · Part 2 of 4 · 20%" in text
    assert "progress-dial-medium" in text
    assert 'aria-current="step"' in text
    assert "WaitingTitle" in text
    assert "#3 in line" in text
    assert "FailedTitle" in text
    assert "transcript unavailable" in text
    assert "ForeignTitle" not in text


def test_processing_page_shows_others_ahead(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            other = await users_repo.create(db, name="Other")
            await _seed_video(db, "theirs-1", other.id)
            await _seed_video(db, "theirs-2", other.id)
            await _seed_video(db, "mine-1")
            await jobs_repo.enqueue(db, "theirs-1")
            await jobs_repo.enqueue(db, "theirs-2")
            await jobs_repo.enqueue(db, "mine-1")
        _run(seed())
        resp = client.get("/processing")
    assert "2 from other profiles ahead" in resp.text


# ── GET /processing/fragment ─────────────────────────────────────


def test_fragment_strip_hidden_when_idle(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.get("/processing/fragment?view=strip")
    assert resp.status_code == 200
    # Idle strip is an invisible poller, not a visible section.
    assert "processing-strip-idle" in resp.text
    assert "Summarizing" not in resp.text


def test_fragment_strip_shows_activity(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            await _seed_video(db, "run-1", title="RunningTitle")
            await _seed_video(db, "wait-1")
            await _seed_video(db, "wait-2")
            await jobs_repo.enqueue(db, "run-1")
            await jobs_repo.enqueue(db, "wait-1")
            await jobs_repo.enqueue(db, "wait-2")
            claimed = await jobs_repo.claim_next(db)
            assert claimed is not None
            await jobs_repo.set_step(db, claimed.id, "fetching transcript")
        _run(seed())
        resp = client.get("/processing/fragment?view=strip")
    text = resp.text
    assert "RunningTitle" in text
    assert "Transcript · Checking subtitles" in text
    assert "progress-dial-small is-indeterminate" in text
    assert "2 waiting" in text
    assert 'href="/processing"' in text


def test_fragment_strip_shows_whisper_position_and_eta(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            await _seed_video(db, "run-1", title="RunningTitle")
            await jobs_repo.enqueue(db, "run-1")
            claimed = await jobs_repo.claim_next(db)
            assert claimed is not None
            await jobs_repo.set_step(
                db, claimed.id, "transcribing 3:12 / 42:10 (8%) · ~12:40 left",
            )
        _run(seed())
        resp = client.get("/processing/fragment?view=strip")
    assert "Whisper · 3:12 / 42:10 · 8%" in resp.text
    assert "about 12:40 left" in resp.text


def test_fragment_rejects_unknown_view(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.get("/processing/fragment?view=nope")
    assert resp.status_code == 400


# ── POST cancel ──────────────────────────────────────────────────


def test_cancel_own_pending_job(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            await _seed_video(db, "wait-1")
            return await jobs_repo.enqueue(db, "wait-1")
        jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/cancel", follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/processing"
        assert _run(jobs_repo.get(app.state.db, jid)) is None


def test_cancel_returns_fragment_for_htmx(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            await _seed_video(db, "wait-1")
            return await jobs_repo.enqueue(db, "wait-1")
        jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/cancel",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "Nothing in progress" in resp.text


def test_cancel_foreign_job_404_and_untouched(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            other = await users_repo.create(db, name="Other")
            await _seed_video(db, "theirs-1", other.id)
            return await jobs_repo.enqueue(db, "theirs-1")
        jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/cancel", follow_redirects=False,
        )
        assert resp.status_code == 404
        job = _run(jobs_repo.get(app.state.db, jid))
        assert job is not None and job.state is JobState.PENDING


def test_cancel_running_job_409(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            await _seed_video(db, "run-1")
            jid = await jobs_repo.enqueue(db, "run-1")
            assert await jobs_repo.claim_next(db) is not None
            return jid
        jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/cancel", follow_redirects=False,
        )
        assert resp.status_code == 409
        job = _run(jobs_repo.get(app.state.db, jid))
        assert job is not None and job.state is JobState.RUNNING


def test_cancel_respects_profile_cookie(tmp_path, monkeypatch):
    """The active profile comes from the cookie; the other profile's
    job is cancellable only when that profile is active."""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            other = await users_repo.create(db, name="Other")
            await _seed_video(db, "theirs-1", other.id)
            return other.id, await jobs_repo.enqueue(db, "theirs-1")
        other_id, jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/cancel",
            cookies={PROFILE_COOKIE: str(other_id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _run(jobs_repo.get(app.state.db, jid)) is None


# ── POST retry / dismiss ─────────────────────────────────────────


def test_retry_own_failed_job(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            await _seed_video(db, "fail-1")
            jid = await jobs_repo.enqueue(db, "fail-1")
            await jobs_repo.fail(db, jid, "boom")
            return jid
        jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/retry", follow_redirects=False,
        )
        assert resp.status_code == 303
        job = _run(jobs_repo.get(app.state.db, jid))
        assert job is not None and job.state is JobState.PENDING


def test_retry_foreign_job_404(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            other = await users_repo.create(db, name="Other")
            await _seed_video(db, "theirs-1", other.id)
            jid = await jobs_repo.enqueue(db, "theirs-1")
            await jobs_repo.fail(db, jid, "boom")
            return jid
        jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/retry", follow_redirects=False,
        )
        assert resp.status_code == 404
        job = _run(jobs_repo.get(app.state.db, jid))
        assert job is not None and job.state is JobState.FAILED


def test_retry_pending_job_409(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            await _seed_video(db, "wait-1")
            return await jobs_repo.enqueue(db, "wait-1")
        jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/retry", follow_redirects=False,
        )
        assert resp.status_code == 409


def test_dismiss_own_failed_job(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            await _seed_video(db, "fail-1")
            jid = await jobs_repo.enqueue(db, "fail-1")
            await jobs_repo.fail(db, jid, "boom")
            return jid
        jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/dismiss", follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _run(jobs_repo.get(app.state.db, jid)) is None


def test_dismiss_foreign_job_404(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            other = await users_repo.create(db, name="Other")
            await _seed_video(db, "theirs-1", other.id)
            jid = await jobs_repo.enqueue(db, "theirs-1")
            await jobs_repo.fail(db, jid, "boom")
            return jid
        jid = _run(seed())
        resp = client.post(
            f"/processing/jobs/{jid}/dismiss", follow_redirects=False,
        )
        assert resp.status_code == 404
        assert _run(jobs_repo.get(app.state.db, jid)) is not None


def test_retry_disabled_when_video_already_summarized(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        async def seed():
            db = app.state.db
            await _seed_video(db, "fail-1", title="StaleFailure")
            jid = await jobs_repo.enqueue(db, "fail-1")
            await jobs_repo.fail(db, jid, "boom")
            await videos_repo.set_summary(db, "fail-1", "s", "m")
        _run(seed())
        resp = client.get("/processing")
    assert "StaleFailure" in resp.text
    assert "already summarized" in resp.text
