import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import FeedbackSource, Sentiment
from app.repos import feedback as feedback_repo
from app.repos import videos as videos_repo


def _seed_video(app, vid: str = "v1", user_id: int = 1) -> None:
    async def setup():
        await videos_repo.upsert_metadata(
            app.state.db, video_id=vid, url=f"https://youtu.be/{vid}",
            title="t", description="", thumbnail_path=None,
            duration_seconds=None, user_id=user_id,
        )
    asyncio.get_event_loop().run_until_complete(setup())


def test_post_feedback_creates_row(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, "v1", user_id=1)
        resp = client.post(
            "/feedback",
            json={
                "video_id": "v1",
                "source": "summary",
                "selected_text": "a key claim",
                "text_offset_start": 10,
                "text_offset_end": 21,
                "sentiment": "interesting",
                "comment": "matters for my use case",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["sentiment"] == "interesting"


def test_post_feedback_rejects_invalid_offsets(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, "v1")
        resp = client.post(
            "/feedback",
            json={
                "video_id": "v1", "source": "summary",
                "selected_text": "x",
                "text_offset_start": 5, "text_offset_end": 5,
                "sentiment": "interesting", "comment": None,
            },
        )
    assert resp.status_code == 422


def test_post_feedback_rejects_overlong_text(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, "v1")
        resp = client.post(
            "/feedback",
            json={
                "video_id": "v1", "source": "summary",
                "selected_text": "x" * 1500,
                "text_offset_start": 0, "text_offset_end": 1500,
                "sentiment": "interesting", "comment": None,
            },
        )
    assert resp.status_code == 422


def test_post_feedback_rejects_cross_profile_video(tmp_path, monkeypatch):
    """Video owned by user_id=2, active cookie defaults to user 1 -> 403."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed_user2():
            await app.state.db.execute(
                "INSERT INTO users (id, name) VALUES (2, 'other')"
            )
            await app.state.db.commit()
        asyncio.get_event_loop().run_until_complete(seed_user2())
        _seed_video(app, "v2", user_id=2)
        resp = client.post(
            "/feedback",
            json={
                "video_id": "v2", "source": "summary",
                "selected_text": "x",
                "text_offset_start": 0, "text_offset_end": 1,
                "sentiment": "interesting", "comment": None,
            },
        )
    assert resp.status_code == 403


def test_delete_feedback_only_for_owner(tmp_path, monkeypatch):
    """A feedback row owned by user 2 must not be deletable by the
    default (user 1) caller. Returns 404 — we don't leak that the row
    exists."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await app.state.db.execute(
                "INSERT INTO users (id, name) VALUES (2, 'other')"
            )
            await app.state.db.commit()
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v2", url="u", title="t",
                description="", thumbnail_path=None,
                duration_seconds=None, user_id=2,
            )
            fb = await feedback_repo.create(
                app.state.db, user_id=2, video_id="v2",
                source=FeedbackSource.SUMMARY,
                selected_text="x",
                text_offset_start=0, text_offset_end=1,
                sentiment=Sentiment.INTERESTING, comment=None,
            )
            return fb.id
        fb_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.delete(f"/feedback/{fb_id}")
    assert resp.status_code == 404


def test_delete_feedback_succeeds_for_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, "v1")
        async def setup():
            fb = await feedback_repo.create(
                app.state.db, user_id=1, video_id="v1",
                source=FeedbackSource.SUMMARY,
                selected_text="x",
                text_offset_start=0, text_offset_end=1,
                sentiment=Sentiment.INTERESTING, comment=None,
            )
            return fb.id
        fb_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.delete(f"/feedback/{fb_id}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_post_feedback_for_digest_tldr_creates_row(tmp_path, monkeypatch):
    """POST with digest_id (and no video_id) anchors the feedback to
    the digest's TL;DR — used by the TL;DR section of the digest page."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        from datetime import UTC, datetime, timedelta

        from app.repos import digests as digests_repo

        async def setup():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1,
                period_start=start, period_end=end,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())

        resp = client.post(
            "/feedback",
            json={
                "digest_id": digest_id,
                "source": "digest_tldr",
                "selected_text": "key thematic observation",
                "text_offset_start": 0,
                "text_offset_end": 24,
                "sentiment": "interesting",
                "comment": None,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["sentiment"] == "interesting"


def test_post_feedback_rejects_both_anchors(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video(app, "v1")
        resp = client.post(
            "/feedback",
            json={
                "video_id": "v1",
                "digest_id": 1,
                "source": "summary",
                "selected_text": "x",
                "text_offset_start": 0,
                "text_offset_end": 1,
                "sentiment": "interesting",
                "comment": None,
            },
        )
    assert resp.status_code == 422


def test_post_feedback_rejects_neither_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/feedback",
            json={
                "source": "summary",
                "selected_text": "x",
                "text_offset_start": 0,
                "text_offset_end": 1,
                "sentiment": "interesting",
                "comment": None,
            },
        )
    assert resp.status_code == 422


def test_post_feedback_rejects_cross_profile_digest(tmp_path, monkeypatch):
    """A digest belonging to user 2 must not be writable by user 1."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        from datetime import UTC, datetime, timedelta

        from app.repos import digests as digests_repo

        async def setup():
            await app.state.db.execute(
                "INSERT INTO users (id, name) VALUES (2, 'other')"
            )
            await app.state.db.commit()
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=2,
                period_start=start, period_end=end,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())

        resp = client.post(
            "/feedback",
            json={
                "digest_id": digest_id,
                "source": "digest_tldr",
                "selected_text": "x",
                "text_offset_start": 0,
                "text_offset_end": 1,
                "sentiment": "interesting",
                "comment": None,
            },
        )
    assert resp.status_code == 403
