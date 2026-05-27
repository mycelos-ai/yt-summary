import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import FeedbackSource, Sentiment
from app.repos import feedback as feedback_repo
from app.repos import videos as videos_repo


def _seed_video_with_summary(app, vid: str = "v1") -> None:
    async def setup():
        await videos_repo.upsert_metadata(
            app.state.db, video_id=vid, url=f"https://youtu.be/{vid}",
            title="Test video", description="",
            thumbnail_path=None, duration_seconds=None,
        )
        await videos_repo.set_summary(
            app.state.db, vid, "## TL;DR\nA short summary.", "openai/gpt-4o",
            language="en",
        )
    asyncio.get_event_loop().run_until_complete(setup())


def test_video_detail_includes_highlight_popover_when_summary_present(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "v1")
        resp = client.get("/v/v1")
    assert resp.status_code == 200
    assert 'id="highlight-popover"' in resp.text
    assert "/static/highlight.js" in resp.text
    assert "__HIGHLIGHT_DATA__" in resp.text
    assert "data-highlight-target" in resp.text


def test_video_detail_omits_highlight_popover_when_no_summary(
    tmp_path, monkeypatch,
):
    """A video without a summary has no highlight target to anchor to —
    skip the popover machinery."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v_nosum", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/v_nosum")
    assert resp.status_code == 200
    assert 'id="highlight-popover"' not in resp.text


def test_video_detail_embeds_existing_feedback_for_restore(
    tmp_path, monkeypatch,
):
    """The JS uses window.__HIGHLIGHT_DATA__.existing to restore prior
    highlights on page load. Verify the route serialises them."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "v1")
        async def seed_fb():
            await feedback_repo.create(
                app.state.db, user_id=1, video_id="v1",
                source=FeedbackSource.SUMMARY,
                selected_text="A short summary",
                text_offset_start=10, text_offset_end=25,
                sentiment=Sentiment.INTERESTING,
                comment="memorable",
            )
        asyncio.get_event_loop().run_until_complete(seed_fb())
        resp = client.get("/v/v1")
    assert resp.status_code == 200
    # The page should embed the feedback as JSON inside __HIGHLIGHT_DATA__.
    assert "A short summary" in resp.text
    assert '"sentiment": "interesting"' in resp.text or "&#34;interesting&#34;" in resp.text
