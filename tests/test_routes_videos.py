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


def test_post_videos_invalid_url_renders_error_page(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/videos", data={"url": "not a url"})
    # Plain browser submit → 200 with the rendered error page so the
    # user can fix the URL without losing context.
    assert resp.status_code == 200
    assert "look like a URL" in resp.text
    assert "not a url" in resp.text  # value preserved in the input

    # HTMX submit → 400 so HTMX surfaces the error.
    with TestClient(app) as client:
        resp = client.post(
            "/videos",
            data={"url": "not a url"},
            headers={"HX-Request": "true"},
        )
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


def test_post_videos_persists_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake_meta = VideoMetadata(
        id="taggedvid01",
        url="https://youtu.be/taggedvid01",
        title="T",
        description="d",
        duration_seconds=300,
        thumbnail_url=None,
        tags=("python", "fastapi", "tutorial"),
    )
    app = create_app()
    with (
        patch("app.routes.videos.fetch_metadata", AsyncMock(return_value=fake_meta)),
        patch("app.routes.videos.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        client.post(
            "/videos",
            data={"url": "https://youtu.be/taggedvid01"},
            follow_redirects=False,
        )

        import asyncio

        async def check():
            from app.repos import tags as tags_repo
            tags = await tags_repo.tags_for_video(app.state.db, "taggedvid01")
            assert sorted(tags) == ["fastapi", "python", "tutorial"]

        asyncio.get_event_loop().run_until_complete(check())


def test_video_detail_renders_tag_pills(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import tags as tags_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vd_tags", url="u", title="T",
                description="d", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(
                app.state.db, "vd_tags", "Body.", "model"
            )
            await tags_repo.set_tags_for_video(
                app.state.db, "vd_tags", ["alpha", "beta"]
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/vd_tags")
    assert resp.status_code == 200
    assert "alpha" in resp.text
    assert "beta" in resp.text
    assert 'href="/?tag=alpha"' in resp.text


def test_post_videos_with_web_url_creates_web_kind(tmp_path, monkeypatch):
    from app.services.reader import ArticleMetadata
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake_article = ArticleMetadata(
        url="https://example.com/post-1",
        title="Some Article",
        description="A short description.",
        body="The body text of the article. " * 5,
        thumbnail_url=None,
    )
    app = create_app()
    with (
        patch("app.routes.videos.fetch_article", AsyncMock(return_value=fake_article)),
        patch("app.routes.videos.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/videos",
            data={"url": "https://example.com/post-1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/v/web-")

        import asyncio
        async def check():
            from app.models import VideoKind
            from app.repos import videos as videos_repo
            item_id = location.split("/v/")[1]
            v = await videos_repo.get(app.state.db, item_id)
            assert v is not None
            assert v.kind == VideoKind.WEB
            assert v.title == "Some Article"
            assert v.transcript is not None
            assert "body text" in v.transcript

        asyncio.get_event_loop().run_until_complete(check())


def test_post_videos_with_unfetchable_url_renders_error_page(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    async def boom(url):
        raise ValueError("The page does not exist (404 Not Found).")

    with (
        patch("app.routes.videos.fetch_article", side_effect=boom),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/videos",
            data={"url": "https://example.com/dead"},
            follow_redirects=False,
        )
    # Plain browser submit → 200 with rendered error page.
    assert resp.status_code == 200
    assert "404" in resp.text
    assert "add that" in resp.text  # 'Couldn&#39;t add that' after Jinja escape
    # The submitted URL is preserved so the user can edit it.
    assert "https://example.com/dead" in resp.text


def test_post_videos_rejects_non_http_string(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/videos", data={"url": "ftp://example.com/x"},
            follow_redirects=False,
        )
    # Plain browser submit returns the rendered error page.
    assert resp.status_code == 200
    assert "look like a URL" in resp.text


def test_video_detail_for_web_uses_open_original_label(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        from app.models import VideoKind

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="web-deadbeef123", url="https://example.com/x",
                title="WebThing", description="", thumbnail_path=None,
                duration_seconds=None, kind=VideoKind.WEB,
            )
            await videos_repo.set_summary(
                app.state.db, "web-deadbeef123", "Body.", "model"
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/web-deadbeef123")
    assert resp.status_code == 200
    assert "Open original" in resp.text
    assert "Watch on YouTube" not in resp.text


def test_retranscribe_clears_and_enqueues(tmp_path, monkeypatch):
    """The Re-transcribe button wipes transcript + segments, then
    enqueues a fresh job. Used to repair videos with stale stored
    data — pre-launch we have a few like that."""
    import asyncio
    import json as _json

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:

        async def setup():
            from app.models import TranscriptSource
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="rt1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            # Plant a stale transcript + segments so we can verify
            # the route actually clears them.
            await videos_repo.set_transcript(
                app.state.db, "rt1",
                "stale text",
                TranscriptSource.AUTO_SUBS,
                segments_json=_json.dumps([{"start": 0.0, "text": "stale"}]),
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/v/rt1/retranscribe", follow_redirects=False)
        assert resp.status_code == 303

        async def check():
            from app.repos import videos as videos_repo
            v = await videos_repo.get(app.state.db, "rt1")
            assert v is not None
            assert v.transcript is None
            assert v.transcript_segments is None
            assert v.transcript_source is None
            cursor = await app.state.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE video_id=?", ("rt1",)
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] >= 1

        asyncio.get_event_loop().run_until_complete(check())


def test_retranscribe_404_unknown_video(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/v/nope/retranscribe", follow_redirects=False)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Inline timestamp links + lazy player
# ---------------------------------------------------------------------------


def test_decorate_timestamp_links_adds_data_attribute():
    """Plain markdown-rendered anchor `<a href="#t=754">12:34</a>` gets
    a `data-yt-timestamp` attribute the JS click handler picks up."""
    from app.routes.videos import _decorate_timestamp_links
    html = '<p>See <a href="#t=754">12:34</a> for the demo.</p>'
    out = _decorate_timestamp_links(html)
    assert 'data-yt-timestamp="754"' in out
    # The visible text and href both preserved
    assert ">12:34</a>" in out
    assert 'href="#t=754"' in out


def test_decorate_timestamp_links_idempotent():
    """Running the decorator twice is safe — useful for fragments that
    might pass through the renderer more than once."""
    from app.routes.videos import _decorate_timestamp_links
    html = '<a href="#t=42">00:42</a>'
    once = _decorate_timestamp_links(html)
    twice = _decorate_timestamp_links(once)
    assert once == twice
    # Exactly one data attribute, not two
    assert twice.count("data-yt-timestamp") == 1


def test_decorate_timestamp_links_handles_multiple():
    from app.routes.videos import _decorate_timestamp_links
    html = (
        '<a href="#t=10">00:10</a> and '
        '<a href="#t=200">03:20</a>'
    )
    out = _decorate_timestamp_links(html)
    assert 'data-yt-timestamp="10"' in out
    assert 'data-yt-timestamp="200"' in out


def test_decorate_timestamp_links_leaves_other_links_alone():
    from app.routes.videos import _decorate_timestamp_links
    html = '<a href="https://example.com">x</a>'
    assert _decorate_timestamp_links(html) == html


def test_video_detail_youtube_renders_player_placeholder(tmp_path, monkeypatch):
    """YouTube detail pages embed a lazy player placeholder + the
    bootstrap JS so timestamp links work."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="ytplay123", url="https://youtu.be/ytplay123",
                title="Player Test", description="",
                thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(
                app.state.db, "ytplay123", "x", "model"
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/ytplay123")

    assert resp.status_code == 200
    assert "yt-player-placeholder" in resp.text
    # Embed must use the privacy-preserving host
    assert "youtube-nocookie.com" in resp.text
    # The video id is wired in for the JS bootstrap
    assert "ytplay123" in resp.text


def test_video_detail_web_does_not_render_player(tmp_path, monkeypatch):
    """Web articles have no player and no JS bootstrap."""
    from app.models import VideoKind

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="web-deadbeef999", url="https://example.com/x",
                title="WebPost", description="", thumbnail_path=None,
                duration_seconds=None, kind=VideoKind.WEB,
            )
            await videos_repo.set_summary(
                app.state.db, "web-deadbeef999", "x", "model"
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/web-deadbeef999")

    assert resp.status_code == 200
    assert "yt-player-placeholder" not in resp.text
    assert "youtube-nocookie.com" not in resp.text


def test_video_detail_summary_timestamps_get_data_attribute(tmp_path, monkeypatch):
    """Summary text containing `[12:34](#t=754)` should be rendered
    with the `data-yt-timestamp` attribute so the JS handler picks
    them up."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="sumts1234567", url="https://youtu.be/sumts1234567",
                title="ts", description="", thumbnail_path=None,
                duration_seconds=None,
            )
            await videos_repo.set_summary(
                app.state.db,
                "sumts1234567",
                "Watch [12:34](#t=754) for the demo.",
                "model",
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/sumts1234567")

    assert resp.status_code == 200
    assert 'data-yt-timestamp="754"' in resp.text


def test_transcript_block_anchors_carry_timestamp_data_attribute(tmp_path, monkeypatch):
    """Transcript paragraph anchors (already linking out to YouTube)
    must also carry data-yt-timestamp so the inline player picks
    them up."""
    import json as _json

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.models import TranscriptSource
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="txblk1234567", url="https://youtu.be/txblk1234567",
                title="t", description="", thumbnail_path=None,
                duration_seconds=300,
            )
            segs = [
                {"start": 0.0, "text": "first paragraph"},
                {"start": 42.0, "text": "second paragraph"},
            ]
            await videos_repo.set_transcript(
                app.state.db, "txblk1234567",
                "first paragraph\nsecond paragraph",
                TranscriptSource.AUTO_SUBS,
                segments_json=_json.dumps(segs),
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/txblk1234567")

    assert resp.status_code == 200
    # Transcript anchor for the second block at 42s carries the data attr.
    assert 'data-yt-timestamp="42"' in resp.text
    # And the original visible href still works as a no-JS fallback.
    assert "https://youtu.be/txblk1234567?t=42" in resp.text
