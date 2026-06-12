import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import create_app
from app.repos import digests as digests_repo


def test_enqueue_marks_digest_failed_when_run_crashes(tmp_path, monkeypatch):
    """Safety net (mirrors the ask flow): a crashing background digest job
    must leave the row 'failed', not stuck 'pending'/'rendering'."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    from app.routes import digest as digest_route
    from app.services import digest as digest_service

    async def boom(db, *, digest_id, user_id):
        raise RuntimeError("simulated digest crash")
    monkeypatch.setattr(digest_service, "run_for_existing_digest", boom)

    with TestClient(app):
        async def scenario():
            end = datetime.now(UTC).replace(microsecond=0)
            d = await digest_route._enqueue_digest_job(
                app.state.db, user_id=1,
                period_start=end - timedelta(hours=96), period_end=end,
                video_ids=["v1"],
            )
            for t in list(digest_route._PENDING_JOBS):
                await t
            return await digests_repo.get(app.state.db, d.id)
        got = asyncio.get_event_loop().run_until_complete(scenario())
    assert got is not None
    assert got.status.value == "failed"
    assert "simulated digest crash" in (got.error or "")


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


def test_digest_feedback_json_escapes_script_breakout(tmp_path, monkeypatch):
    """Digest feedback selected_text is embedded in a <script> block; a
    `</script><script>…` payload must be escaped so it can't break out."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    payload = "</script><script>window.__pwned=1</script>"
    with TestClient(app) as client:
        async def setup():
            from app.models import FeedbackSource, Sentiment
            from app.repos import feedback as feedback_repo
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1, period_start=start, period_end=end,
            )
            await digests_repo.mark_ready(
                app.state.db, digest_id=d.id, tldr="hi",
                top_items_json="[]", item_count=0,
            )
            await feedback_repo.create(
                app.state.db, user_id=1, digest_id=d.id,
                source=FeedbackSource.DIGEST,
                selected_text=payload,
                text_offset_start=0, text_offset_end=len(payload),
                sentiment=Sentiment.INTERESTING, comment=None,
            )
            return d.id
        digest_id = asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get(f"/digest/{digest_id}")
    assert resp.status_code == 200
    assert "</script><script>window.__pwned" not in resp.text


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


def test_post_digest_generate_persists_selection_and_redirects(
    tmp_path, monkeypatch,
):
    """The handler validates the picked ids against the current
    candidate window, persists them on the digest row, and redirects
    (303) to /digest/<id>. Generation runs in background — neutralize
    it so no LLM call fires."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    from app.services import digest as digest_service

    async def noop(db, *, digest_id, user_id):
        return None
    monkeypatch.setattr(digest_service, "run_for_existing_digest", noop)

    with TestClient(app) as client:
        _seed_route_video(app, "v1")
        _seed_route_video(app, "v2")
        resp = client.post(
            "/digest/generate", data={"video_ids": ["v1"]},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/digest/")
        digest_id = int(location.rsplit("/", 1)[1])

        async def fetch():
            return await digests_repo.get(app.state.db, digest_id)
        d = asyncio.get_event_loop().run_until_complete(fetch())
    assert d is not None
    assert d.selected_video_ids_json == '["v1"]'


def test_post_digest_generate_filters_foreign_ids(tmp_path, monkeypatch):
    """Ids outside the candidate window (unknown, other profile, no
    highlights) are dropped server-side."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    from app.services import digest as digest_service

    async def noop(db, *, digest_id, user_id):
        return None
    monkeypatch.setattr(digest_service, "run_for_existing_digest", noop)

    with TestClient(app) as client:
        _seed_route_video(app, "v1")
        resp = client.post(
            "/digest/generate", data={"video_ids": ["v1", "evil"]},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        digest_id = int(resp.headers["location"].rsplit("/", 1)[1])

        async def fetch():
            return await digests_repo.get(app.state.db, digest_id)
        d = asyncio.get_event_loop().run_until_complete(fetch())
    assert d is not None
    assert d.selected_video_ids_json == '["v1"]'


def test_post_digest_generate_empty_selection_redirects_back(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/digest/generate", data={}, follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/digest/new?error=no-selection"


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
    # TL;DR section is its own highlight target, anchored to the
    # digest (NOT a video) so feedback there uses digest_id.
    assert f'data-digest-id="{digest_id}"' in resp.text
    assert 'data-feedback-source="digest_tldr"' in resp.text


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


def _seed_route_video(app, video_id, *, highlights=True, hours_ago=1):
    async def setup():
        created = (
            datetime.now(UTC) - timedelta(hours=hours_ago)
        ).replace(microsecond=0)
        await app.state.db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title,"
            " highlights_json, created_at) VALUES (?, 1, 'youtube', 'u',"
            " ?, ?, ?)",
            (
                video_id, f"Title {video_id}",
                '[{"text": "t", "rank": 1}]' if highlights else None,
                created.isoformat(),
            ),
        )
        await app.state.db.commit()
    asyncio.get_event_loop().run_until_complete(setup())


def test_get_digest_new_lists_candidates_prechecked(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_route_video(app, "v1")
        resp = client.get("/digest/new")
    assert resp.status_code == 200
    assert "Title v1" in resp.text
    assert 'name="video_ids"' in resp.text
    assert "checked" in resp.text
    assert 'action="/digest/generate"' in resp.text


def test_get_digest_new_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/digest/new")
    assert resp.status_code == 200
    assert 'name="video_ids"' not in resp.text
    assert "No new items" in resp.text


def test_get_digest_new_footnotes_missing_highlights(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_route_video(app, "v1")
        _seed_route_video(app, "v2", highlights=False)
        resp = client.get("/digest/new")
    assert resp.status_code == 200
    assert "Title v1" in resp.text
    assert "Title v2" not in resp.text
    assert "1 more item" in resp.text
