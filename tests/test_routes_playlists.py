from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.playlist import PlaylistEntry, PlaylistMetadata


def _meta(plid: str = "PLtest", entries: list[PlaylistEntry] | None = None) -> PlaylistMetadata:
    return PlaylistMetadata(
        id=plid,
        url=f"https://youtube.com/playlist?list={plid}",
        title="Test playlist",
        description="",
        thumbnail_url=None,
        entries=entries or [],
    )


def test_post_playlists_imports_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake = _meta(
        entries=[
            PlaylistEntry(
                id=f"v{i:010d}", title=f"v{i}", description="",
                thumbnail_url=None, duration_seconds=None,
            )
            for i in range(3)
        ]
    )
    app = create_app()
    with (
        patch("app.routes.playlists.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        patch("app.routes.playlists.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/playlists",
            data={"url": "https://www.youtube.com/playlist?list=PLtest"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/PLtest"


def test_post_playlists_invalid_url_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/playlists", data={"url": "not-a-url"})
    assert resp.status_code == 400


def test_get_playlist_detail_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            from app.repos import videos as videos_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLshow", user_id=1,
                url="u", title="Show me", description="",
                thumbnail_path=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1show", url="u", title="Inner",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await playlists_repo.link_video(app.state.db, "PLshow", "v1show")

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/p/PLshow")
    assert resp.status_code == 200
    assert "Show me" in resp.text
    assert "Inner" in resp.text


def test_get_playlist_404_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/p/PLnope")
    assert resp.status_code == 404


def test_post_playlist_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake = _meta("PLref")
    app = create_app()
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLref", user_id=1,
                url="https://youtube.com/playlist?list=PLref",
                title="r", description="", thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/p/PLref/refresh", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/PLref"


def test_post_playlist_load_older(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    entries = [
        PlaylistEntry(
            id=f"v{i:010d}", title="t", description="",
            thumbnail_url=None, duration_seconds=None,
        )
        for i in range(10)
    ]
    fake = _meta("PLold", entries=entries)
    app = create_app()
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLold", user_id=1,
                url="https://youtube.com/playlist?list=PLold",
                title="o", description="", thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/p/PLold/load-older", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/PLold"


def test_post_playlist_remove_deletes(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLgone", user_id=1, url="u",
                title="x", description="", thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/p/PLgone/remove", follow_redirects=False)
        assert resp.status_code == 303

        async def check():
            from app.repos import playlists as playlists_repo
            assert await playlists_repo.get(app.state.db, "PLgone") is None

        asyncio.get_event_loop().run_until_complete(check())


def test_get_new_playlist_form(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/playlists/new")
    assert resp.status_code == 200
    assert 'name="url"' in resp.text
    assert 'action="/playlists"' in resp.text


def test_get_playlists_lists_all_with_video_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            from app.repos import videos as videos_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLfull", user_id=1, url="u",
                title="With videos", description="", thumbnail_path=None,
            )
            await playlists_repo.create(
                app.state.db, playlist_id="PLempty", user_id=1, url="u",
                title="Empty queue", description="", thumbnail_path=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vp1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vp2", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await playlists_repo.link_video(app.state.db, "PLfull", "vp1")
            await playlists_repo.link_video(app.state.db, "PLfull", "vp2")

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/playlists")
    assert resp.status_code == 200
    assert "With videos" in resp.text
    assert "Empty queue" in resp.text
    # Counts: "2 videos" and "0 videos"
    assert "2 videos" in resp.text
    assert "0 videos" in resp.text


def test_get_playlists_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/playlists")
    assert resp.status_code == 200
    assert "No playlists yet" in resp.text
    assert 'href="/playlists/new"' in resp.text


def test_add_source_page_has_tabs_and_email_needs_mailbox(tmp_path, monkeypatch):
    """The /playlists/new page is now a tabbed 'Add a source' page. With
    no mailbox connected, the Newsletter tab points the user at the
    profile page."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/playlists/new")
    assert resp.status_code == 200
    assert "Add a source" in resp.text
    assert "Newsletter" in resp.text
    assert "/profiles/1/edit" in resp.text  # "connect a mailbox first" link


def test_scan_without_mailbox_returns_friendly_message(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/playlists/new/mail/scan")
    assert resp.status_code == 200
    assert "No mailbox connected" in resp.text


def test_scan_lists_discovered_senders(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    from app.services.mailbox import Discovery, SenderInfo

    discovery = Discovery(
        senders=[
            SenderInfo(addr="news@acme.com", name="Acme", count=3,
                       last_subject="Weekly", last_date=None),
        ],
        max_uid=42,
        scanned=137,
    )
    with TestClient(app) as client:
        # Connect a mailbox for the active profile.
        client.post(
            "/profiles/1/imap",
            data={
                "imap_enabled": "1", "imap_host": "imap.acme.com",
                "imap_ssl": "1", "imap_username": "u@acme.com",
                "imap_password": "pw",
            },
            follow_redirects=False,
        )
        with patch(
            "app.routes.playlists.discover_senders",
            AsyncMock(return_value=discovery),
        ):
            resp = client.post("/playlists/new/mail/scan")
    assert resp.status_code == 200
    assert "news@acme.com" in resp.text
    assert "Acme" in resp.text
    # Feedback summary: messages scanned + senders found (+ new count).
    assert "Scanned 137 messages" in resp.text
    assert "1 sender found" in resp.text
    assert "1 new" in resp.text


def test_subscribe_persists_selected_senders(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            from app.repos import mail_senders as repo
            await repo.upsert_discovered(
                app.state.db, 1,
                [("a@x.com", "A", None, None), ("b@x.com", "B", None, None)],
            )

        asyncio.get_event_loop().run_until_complete(seed())

        client.post(
            "/playlists/new/mail/subscribe",
            data={"sender": ["a@x.com"]},
            follow_redirects=False,
        )

        async def read():
            from app.repos import mail_senders as repo
            return await repo.subscribed_addrs(app.state.db, 1)

        subscribed = asyncio.get_event_loop().run_until_complete(read())
    assert subscribed == {"a@x.com"}


def test_saved_creds_without_polling_still_count_as_connected(tmp_path, monkeypatch):
    """Saving IMAP creds with the polling toggle OFF must NOT read as
    'no mailbox connected' — the sender UI shows, with a polling-off
    hint."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # imap_enabled omitted → polling off, but creds are saved.
        client.post(
            "/profiles/1/imap",
            data={
                "imap_host": "imap.mailbox.org", "imap_ssl": "1",
                "imap_username": "u@mailbox.org", "imap_password": "pw",
            },
            follow_redirects=False,
        )
        resp = client.get("/playlists/new")
    assert resp.status_code == 200
    assert "No mailbox connected" not in resp.text
    assert "Scan recent senders" in resp.text
    # Polling-off hint is shown (text wraps, so match contiguous markers).
    assert "Mailbox connected, but" in resp.text
    assert "Enable newsletter polling" in resp.text


def test_scan_excludes_and_evicts_own_addresses(tmp_path, monkeypatch):
    """The profile's own addresses must never appear as scan candidates —
    forwarded copies in the mailbox shouldn't make 'me' look like a
    newsletter — and a leftover from an earlier scan is evicted."""
    import asyncio

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    from app.services.mailbox import Discovery, SenderInfo

    discovery = Discovery(
        senders=[
            SenderInfo(addr="news@acme.com", name="Acme", count=2,
                       last_subject="Hi", last_date=None),
            SenderInfo(addr="stefan@gmail.com", name="Stefan", count=1,
                       last_subject="Fwd: thing", last_date=None),
        ],
        max_uid=10,
    )
    with TestClient(app) as client:
        client.post(
            "/profiles/1/imap",
            data={
                "imap_host": "imap.x", "imap_ssl": "1",
                "imap_username": "u@x", "imap_password": "pw",
                "mail_own_addresses": "stefan@gmail.com",
            },
            follow_redirects=False,
        )

        async def seed():
            from app.repos import mail_senders as repo
            await repo.upsert_discovered(
                app.state.db, 1, [("stefan@gmail.com", "Stefan", None, None)]
            )

        asyncio.get_event_loop().run_until_complete(seed())

        with patch(
            "app.routes.playlists.discover_senders",
            AsyncMock(return_value=discovery),
        ):
            resp = client.post("/playlists/new/mail/scan")
    assert resp.status_code == 200
    assert "news@acme.com" in resp.text
    assert "stefan@gmail.com" not in resp.text
