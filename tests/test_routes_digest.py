import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import create_app
from app.repos import digests as digests_repo


def test_get_digest_list_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/digest")
    assert resp.status_code == 200
    assert "Daily digest" in resp.text


def test_get_digest_show_renders_ready_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            await digests_repo.mark_ready(
                app.state.db, digest_id=d.id, tldr="Hello world.",
                top_items_json="[]", item_count=0,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 200
    assert "Hello world" in resp.text


def test_get_digest_show_polls_when_pending(tmp_path, monkeypatch):
    """While the digest is pending, the template should render the
    HTMX-polling div so the browser refreshes until ready."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 200
    assert "Building digest" in resp.text
    assert 'hx-get="/digest/' in resp.text


def test_get_digest_show_404_for_foreign_profile(tmp_path, monkeypatch):
    """A digest belonging to user_id=2 must return 404 to the
    default-user-1 caller. No leak via 403."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await app.state.db.execute(
                "INSERT INTO users (id, name) VALUES (2, 'other')"
            )
            await app.state.db.commit()
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=2, period_start=start, period_end=end,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 404


def test_get_digest_show_404_for_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/digest/9999")
    assert resp.status_code == 404


def test_post_digest_generate_redirects_to_new_digest(
    tmp_path, monkeypatch,
):
    """The handler should create a pending digest synchronously and
    redirect (303) to /digest/<id>. The actual generation happens in
    a background task we don't need to wait for here — verify only
    the synchronous part."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    # Monkeypatch the route's `_enqueue_digest_job` so we don't fire
    # the real LLM-driven digest in the background.
    from app.routes import digest as digest_route

    async def fake_enqueue(db, *, user_id, period_hours):
        end = datetime.now(UTC).replace(microsecond=0)
        start = end - timedelta(hours=period_hours)
        return await digests_repo.create_pending(
            db, user_id=user_id, period_start=start, period_end=end,
        )

    monkeypatch.setattr(digest_route, "_enqueue_digest_job", fake_enqueue)

    with TestClient(app) as client:
        resp = client.post(
            "/digest/generate", data={"period_hours": "24"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/digest/")


def test_post_digest_generate_rejects_invalid_period_hours(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/digest/generate", data={"period_hours": "0"},
            follow_redirects=False,
        )
    assert resp.status_code == 422


def test_body_fragment_returns_just_the_body_no_layout(tmp_path, monkeypatch):
    """The polling fragment endpoint must NOT wrap its response in the
    base layout, otherwise HTMX nests the whole site chrome on every
    tick (header + main + ...).
    """
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}/body-fragment")
    assert resp.status_code == 200
    # The polling spinner is present...
    assert "Building digest" in resp.text
    # ...but the base layout is NOT (no <html>, no site header).
    assert "<html" not in resp.text.lower()
    assert "site-header" not in resp.text


def test_body_fragment_sends_hx_refresh_when_ready(tmp_path, monkeypatch):
    """Once the digest is ready, the fragment endpoint asks HTMX to do
    a full page reload (via HX-Refresh) so the surrounding chrome
    re-syncs. The poll's inner content is replaced by an empty body."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            await digests_repo.mark_ready(
                app.state.db, digest_id=d.id, tldr="t",
                top_items_json="[]", item_count=0,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        # Simulate an HTMX poll with the right header.
        resp = client.get(
            f"/digest/{digest_id}/body-fragment",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Refresh") == "true"


def test_ready_digest_embeds_highlight_machinery(tmp_path, monkeypatch):
    """A ready digest with sources should embed the popover markup,
    the highlight.js script tag, and per-source data-video-id targets
    so the user can highlight + give feedback on each source's hook."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import json
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="A",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            await digests_repo.mark_ready(
                app.state.db, digest_id=d.id, tldr="hello",
                top_items_json=json.dumps([
                    {"video_id": "v1", "rank": 1,
                     "hook": "Key insight here", "reason": ""},
                ]),
                item_count=1,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 200
    assert 'id="highlight-popover"' in resp.text
    assert "/static/highlight.js" in resp.text
    assert "__HIGHLIGHT_DATA__" in resp.text
    # Per-source target with the correct video id wired up
    assert 'data-highlight-target' in resp.text
    assert 'data-video-id="v1"' in resp.text
    assert 'data-feedback-source="digest"' in resp.text


def test_pending_digest_omits_highlight_machinery(tmp_path, monkeypatch):
    """Highlighting only makes sense on a ready digest with sources —
    the polling spinner has nothing to anchor feedback to."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 200
    assert 'id="highlight-popover"' not in resp.text
    assert "/static/highlight.js" not in resp.text


def test_ready_digest_restores_only_digest_source_feedback(
    tmp_path, monkeypatch,
):
    """Pre-existing feedback rows with source='summary' on the same
    video should NOT show up in the digest's restore-list — only
    feedback marked at the digest itself."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import json
    with TestClient(app) as client:
        async def setup():
            from app.models import FeedbackSource, Sentiment
            from app.repos import feedback as feedback_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="A",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await feedback_repo.create(
                app.state.db, user_id=1, video_id="v1",
                source=FeedbackSource.SUMMARY,
                selected_text="not for digest", text_offset_start=0,
                text_offset_end=14,
                sentiment=Sentiment.INTERESTING, comment=None,
            )
            await feedback_repo.create(
                app.state.db, user_id=1, video_id="v1",
                source=FeedbackSource.DIGEST,
                selected_text="from digest page", text_offset_start=0,
                text_offset_end=16,
                sentiment=Sentiment.INTERESTING, comment=None,
            )
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            await digests_repo.mark_ready(
                app.state.db, digest_id=d.id, tldr="t",
                top_items_json=json.dumps([
                    {"video_id": "v1", "rank": 1, "hook": "h", "reason": ""},
                ]),
                item_count=1,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 200
    assert "from digest page" in resp.text
    assert "not for digest" not in resp.text


def test_body_fragment_404_for_foreign_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await app.state.db.execute(
                "INSERT INTO users (id, name) VALUES (2, 'other')"
            )
            await app.state.db.commit()
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=2, period_start=start, period_end=end,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}/body-fragment")
    assert resp.status_code == 404
