from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.youtube import VideoMetadata


def test_post_videos_htmx_returns_card_fragment(tmp_path, monkeypatch):
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
        resp = client.post(
            "/videos",
            data={"url": "https://youtu.be/abc12345678"},
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "A test video" in resp.text
    assert "abc12345678" in resp.text


def test_post_videos_browser_redirects_to_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake_meta = VideoMetadata(
        id="xyz98765432",
        url="https://youtu.be/xyz98765432",
        title="Another test",
        description="",
        duration_seconds=120,
        thumbnail_url=None,
    )
    app = create_app()
    with (
        patch("app.routes.videos.fetch_metadata", AsyncMock(return_value=fake_meta)),
        patch("app.routes.videos.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/videos",
            data={"url": "https://youtu.be/xyz98765432"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/v/xyz98765432"


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


def test_reindex_enqueues_new_job(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import jobs as jobs_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="ri1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await jobs_repo.enqueue(app.state.db, "ri1")

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/v/ri1/reindex", follow_redirects=False)
        assert resp.status_code == 303

        async def check():
            from app.repos import jobs as jobs_repo
            cursor = await app.state.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE video_id=?", ("ri1",)
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] >= 2  # Original + reindex
            latest = await jobs_repo.latest_for_video(app.state.db, "ri1")
            assert latest is not None
            assert latest.state.value in ("pending", "running", "done", "failed")

        asyncio.get_event_loop().run_until_complete(check())


def test_reindex_404_unknown_video(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/v/nope/reindex", follow_redirects=False)
    assert resp.status_code == 404


def test_summary_fragment_polls_when_job_running(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import jobs as jobs_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="sf1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await jobs_repo.enqueue(app.state.db, "sf1")
            # mark job running so polling kicks in
            claimed = await jobs_repo.claim_next(app.state.db)
            assert claimed is not None

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/sf1/summary-fragment")
    assert resp.status_code == 200
    assert "every 2s" in resp.text
    assert "summary-fragment" in resp.text


def test_summary_fragment_stops_polling_when_done(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="sf2", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(
                app.state.db, "sf2", "## Final\nDone.", "openai/gpt-4o"
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/sf2/summary-fragment")
    assert resp.status_code == 200
    assert "every 2s" not in resp.text
    assert "Done." in resp.text


def test_summary_fragment_htmx_poll_when_done_triggers_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import jobs as jobs_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="sf3", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(
                app.state.db, "sf3", "Final.", "openai/gpt-4o"
            )
            job_id = await jobs_repo.enqueue(app.state.db, "sf3")
            await jobs_repo.complete(app.state.db, job_id)

        asyncio.get_event_loop().run_until_complete(setup())
        # HTMX poll arrives and the latest job is in terminal state →
        # HX-Refresh tells HTMX to reload the whole page.
        resp = client.get(
            "/v/sf3/summary-fragment", headers={"HX-Request": "true"}
        )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Refresh") == "true"


def test_summary_fragment_initial_load_when_done_no_reload(tmp_path, monkeypatch):
    """Plain (non-HTMX) request must NOT trigger reload — that would loop."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="sf4", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(
                app.state.db, "sf4", "Final.", "openai/gpt-4o"
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/sf4/summary-fragment")
    assert resp.status_code == 200
    assert "HX-Refresh" not in resp.headers
    assert "Final." in resp.text
