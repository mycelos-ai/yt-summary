import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

# app.main must load first so create_app() finishes wiring routers
# before we reach into app.routes.home for the monkeypatch target.
from app.main import create_app  # isort: skip
from app.repos import digests as digests_repo
from app.repos import videos as videos_repo
from app.routes import home as home_routes


@pytest.fixture(autouse=True)
def _skip_onboarding(monkeypatch):
    """Every home-route test in this file predates the onboarding
    wizard and assumes ``GET /`` renders the library directly. Stub
    the redirect helper to declare onboarding done so we don't have to
    seed ``onboarding_completed`` in every test setup block."""
    async def _not_pending(_db):
        return {"pending": False, "next_step": None}
    monkeypatch.setattr(home_routes, "_onboarding_status", _not_pending)


def test_home_submit_field_allows_curl_paste(tmp_path, monkeypatch):
    """The add-overlay input must not be type=url, or the browser would
    reject a pasted 'Copy as cURL' command before it ever reaches the
    server (where the curl is parsed for paywall cookies).
    The old submit-form has been replaced by the omnibox + add-overlay."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    # The add overlay uses a textarea (not type=url) so curl pastes are accepted.
    assert 'data-add-overlay' in resp.text
    assert 'type="url" name="url"' not in resp.text


def test_home_lists_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await videos_repo.upsert_metadata(
                app.state.db,
                video_id="v1",
                url="https://youtu.be/v1",
                title="Test Video",
                description="d",
                thumbnail_path=None,
                duration_seconds=120,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Test Video" in resp.text


def test_home_search(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="a", url="u",
                title="Python tutorial", description="fastapi",
                thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="b", url="u",
                title="Cooking", description="pasta",
                thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/?q=fastapi")
    assert "Python tutorial" in resp.text
    assert "Cooking" not in resp.text


def test_home_lists_playlists(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLhome", user_id=1, url="u",
                title="On home", description="",
                thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "On home" in resp.text
    assert "/p/PLhome" in resp.text
    assert "/playlists/new" in resp.text


def test_home_no_playlists_strip_when_none(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    # Without playlists, the strip with the cards isn't shown — instead
    # we render the queue-CTA empty state with a clear call to action.
    assert 'class="playlist-strip"' not in resp.text
    assert "queue" in resp.text.lower()
    assert "/playlists/new" in resp.text


def test_home_shows_playlist_tags_on_video_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vplt", url="u", title="VidWithPlaylist",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await playlists_repo.create(
                app.state.db, playlist_id="PLshown", user_id=1, url="u",
                title="MyAwesomePlaylist", description="",
                thumbnail_path=None,
            )
            await playlists_repo.link_video(app.state.db, "PLshown", "vplt")

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "MyAwesomePlaylist" in resp.text
    assert "/p/PLshown" in resp.text


def test_home_no_playlist_tags_when_video_unlinked(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vsolo", url="u", title="Standalone",
                description="", thumbnail_path=None, duration_seconds=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Standalone" in resp.text
    # No playlist-tags container rendered for this card
    # (the class is global, but for this video specifically there should
    # be no entry to render)
    assert 'class="playlist-tag"' not in resp.text


def test_home_with_tag_filter_shows_banner_and_filtered_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import tags as tags_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vpy", url="u", title="PythonVid",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vcook", url="u", title="CookingVid",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await tags_repo.set_tags_for_video(app.state.db, "vpy", ["python"])
            await tags_repo.set_tags_for_video(app.state.db, "vcook", ["cooking"])

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/?tag=python")
    assert resp.status_code == 200
    assert "PythonVid" in resp.text
    assert "CookingVid" not in resp.text
    assert "filter-banner" in resp.text
    # Clear-link present
    assert 'href="/"' in resp.text


def test_home_paginates_videos_with_load_more_when_over_25(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            for i in range(26):
                await videos_repo.upsert_metadata(
                    app.state.db,
                    video_id=f"v{i:04d}",
                    url="u",
                    title=f"Video {i}",
                    description="d",
                    thumbnail_path=None,
                    duration_seconds=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    # 25 of 26 rendered
    assert resp.text.count('id="video-v') == 25
    # Load-more button visible
    assert "Load more" in resp.text
    assert "/videos/load-more?offset=25" in resp.text


def test_home_no_load_more_when_exactly_25(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            for i in range(25):
                await videos_repo.upsert_metadata(
                    app.state.db,
                    video_id=f"v{i:04d}",
                    url="u",
                    title=f"Video {i}",
                    description="d",
                    thumbnail_path=None,
                    duration_seconds=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert resp.text.count('id="video-v') == 25
    assert "Load more" not in resp.text


def test_load_more_returns_next_batch_with_followup_button(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            for i in range(60):
                await videos_repo.upsert_metadata(
                    app.state.db,
                    video_id=f"v{i:04d}",
                    url="u",
                    title=f"Video {i}",
                    description="d",
                    thumbnail_path=None,
                    duration_seconds=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/videos/load-more?offset=25")
    assert resp.status_code == 200
    # 25 cards in this batch
    assert resp.text.count('id="video-v') == 25
    # Follow-up button with offset=50 since 60 > 50
    assert "/videos/load-more?offset=50" in resp.text


def test_load_more_last_batch_returns_cards_only(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            for i in range(40):
                await videos_repo.upsert_metadata(
                    app.state.db,
                    video_id=f"v{i:04d}",
                    url="u",
                    title=f"Video {i}",
                    description="d",
                    thumbnail_path=None,
                    duration_seconds=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/videos/load-more?offset=25")
    assert resp.status_code == 200
    # 15 remaining cards
    assert resp.text.count('id="video-v') == 15
    # No follow-up button — we've reached the end
    assert "Load more" not in resp.text
    assert "/videos/load-more?offset=50" not in resp.text


def test_home_caps_playlists_at_five(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import playlists as playlists_repo
            for i in range(7):
                await playlists_repo.create(
                    app.state.db, playlist_id=f"PL{i}", user_id=1, url="u",
                    title=f"Playlist {i}", description="",
                    thumbnail_path=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    # Exactly 5 playlist cards rendered (excluding the add card)
    assert resp.text.count('class="playlist-card-wrap"') == 5
    # The add-playlist tile is still visible
    assert 'class="playlist-card playlist-card-add"' in resp.text


def test_home_shows_more_link_when_over_five_playlists(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import playlists as playlists_repo
            for i in range(6):
                await playlists_repo.create(
                    app.state.db, playlist_id=f"PL{i}", user_id=1, url="u",
                    title=f"Playlist {i}", description="",
                    thumbnail_path=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/playlists"' in resp.text
    assert "More" in resp.text


def test_home_no_more_link_when_five_or_fewer_playlists(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import playlists as playlists_repo
            for i in range(3):
                await playlists_repo.create(
                    app.state.db, playlist_id=f"PL{i}", user_id=1, url="u",
                    title=f"Playlist {i}", description="",
                    thumbnail_path=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    # The "More →" link points at /playlists; when not shown, that
    # exact href should be absent.
    assert 'href="/playlists"' not in resp.text


def test_home_card_renders_tag_pills(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import tags as tags_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vpills", url="u", title="HasTags",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await tags_repo.set_tags_for_video(
                app.state.db, "vpills", ["python", "fastapi"]
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert "python" in resp.text
    assert "fastapi" in resp.text
    assert 'href="/?tag=python"' in resp.text


def test_home_shows_digests_strip_with_add_card_when_empty(tmp_path, monkeypatch):
    """No digests yet: only the '+ Generate your first digest' card is
    rendered in the Digests strip, alongside the Queues & playlists
    strip."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Digests" in resp.text
    # The add-digest card links to the candidate-selection page.
    assert 'href="/digest/new"' in resp.text
    assert "Generate your first digest" in resp.text


def test_home_shows_recent_digest_cards(tmp_path, monkeypatch):
    """Recent digests render as cards in the strip with status-aware copy."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1,
                period_start=start, period_end=end,
            )
            await digests_repo.mark_ready(
                app.state.db, digest_id=d.id,
                tldr="Today was busy.",
                top_items_json="[]", item_count=3,
            )
        asyncio.get_event_loop().run_until_complete(seed())
        resp = client.get("/")
    assert resp.status_code == 200
    # Card with item_count is rendered in the strip
    assert "3 items" in resp.text
    # Add-card label switches once at least one digest exists
    assert "New digest" in resp.text
    # "All digests →" archive link appears
    assert 'href="/digest"' in resp.text


def test_home_shows_pending_pill_on_card_while_rendering(tmp_path, monkeypatch):
    """A pending digest in the strip wears a 'building…' pill."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            await digests_repo.create_pending(
                app.state.db, user_id=1,
                period_start=start, period_end=end,
            )
        asyncio.get_event_loop().run_until_complete(seed())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "building" in resp.text
    assert "digest-card--pending" in resp.text


def test_home_shows_failed_pill_on_card(tmp_path, monkeypatch):
    """A failed digest in the strip wears a 'failed' pill and offers retry copy."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(hours=24)
            d = await digests_repo.create_pending(
                app.state.db, user_id=1,
                period_start=start, period_end=end,
            )
            await digests_repo.mark_failed(
                app.state.db, digest_id=d.id, error="LLM down",
            )
        asyncio.get_event_loop().run_until_complete(seed())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "failed" in resp.text
    assert "Click to retry" in resp.text
    assert "digest-card--failed" in resp.text


def test_archive_page_lists_archived(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="ArchivedTitle",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_archived(
                app.state.db, "v1", user_id=1, archived=True,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/archive")
    assert resp.status_code == 200
    assert "ArchivedTitle" in resp.text


def test_archive_page_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/archive")
    assert resp.status_code == 200
    assert "Nothing archived" in resp.text


def test_home_shows_archive_link_when_items_archived(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_archived(
                app.state.db, "v1", user_id=1, archived=True,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert 'href="/archive"' in resp.text
