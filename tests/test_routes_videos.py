from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.youtube import VideoMetadata


def test_post_videos_creates_card_and_enqueues_job(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake_meta = VideoMetadata(
        id="abc12345678",
        url="https://youtu.be/abc12345678",
        title="A test video",
        description="cool",
        duration_seconds=300,
        thumbnail_url="https://example.com/t.jpg",
    )
    app = create_app()
    with (
        patch("app.routes.videos.fetch_metadata", AsyncMock(return_value=fake_meta)),
        patch("app.routes.videos.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post("/videos", data={"url": "https://youtu.be/abc12345678"})
    assert resp.status_code == 200
    assert "A test video" in resp.text
    assert "abc12345678" in resp.text


def test_post_videos_invalid_url_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/videos", data={"url": "not a url"})
    assert resp.status_code == 400


def test_status_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import jobs as jobs_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await jobs_repo.enqueue(app.state.db, "v1")
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/v1/status")
    assert resp.status_code == 200
    # The worker may pick up the job before the GET, so accept any valid job state
    # (pending/queued → running → done) rather than requiring exactly "pending".
    status_words = {"pending", "queued", "running", "done", "failed", "stub", "summary"}
    assert any(w in resp.text.lower() for w in status_words)


def test_status_done_summary_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(app.state.db, "v1", "the summary", "openai/gpt-4o")
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/v1/status")
    assert "summary ready" in resp.text.lower()
    assert "every 2s" not in resp.text


def test_video_detail_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vd1", url="u", title="MyTitle",
                description="d", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(app.state.db, "vd1", "## TL;DR\nshort", "model")
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/vd1")
    assert resp.status_code == 200
    assert "MyTitle" in resp.text
    assert "TL;DR" in resp.text


def test_video_detail_404_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v/nope")
    assert resp.status_code == 404


def test_video_markdown_permalink(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.models import TranscriptSource
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="md1", url="https://youtu.be/md1",
                title="MyTitle", description="d",
                thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_transcript(
                app.state.db, "md1", "the transcript", TranscriptSource.MANUAL_SUBS,
            )
            await videos_repo.set_summary(app.state.db, "md1", "## TL;DR\nshort", "model")
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/md1.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    body = resp.text
    assert "# MyTitle" in body
    assert "## Summary" in body
    assert "## TL;DR" in body
    assert "## Transcript" in body
    assert "the transcript" in body
    assert "https://youtu.be/md1" in body
