import asyncio

from fastapi.testclient import TestClient

from app.main import create_app


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_profile_edit_page_renders_imap_card(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/profiles/1/edit")
    assert resp.status_code == 200
    # The newsletter card lives on the profile page now, not Settings.
    assert "Newsletter (IMAP)" in resp.text
    assert 'action="/profiles/1/imap"' in resp.text


def test_save_imap_persists_per_profile_and_masks_password(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/profiles/1/imap",
            data={
                "imap_enabled": "1",
                "imap_host": "imap.mailbox.org",
                "imap_port": "993",
                "imap_ssl": "1",
                "imap_username": "news@example.com",
                "imap_password": "topsecretpw",
                "imap_folder": "INBOX",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        async def read():
            from app.repos import settings as settings_repo
            return await settings_repo.get_all_for_user(app.state.db, 1)

        stored = _run(read())
        assert stored["imap_host"] == "imap.mailbox.org"
        assert stored["imap_enabled"] == "1"
        assert stored["imap_password"] == "topsecretpw"

        page = client.get("/profiles/1/edit")
    assert page.status_code == 200
    assert "imap.mailbox.org" in page.text
    assert "topsecretpw" not in page.text  # secret never echoed back


def test_save_imap_scopes_to_url_profile_not_cookie(tmp_path, monkeypatch):
    """Posting to /profiles/{id}/imap must touch THAT profile, even when
    a different profile is the active (cookie) one."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def make_profile():
            from app.repos import users as users_repo
            u = await users_repo.create(app.state.db, name="Kids")
            return u.id

        kid_id = _run(make_profile())

        # Active cookie profile stays the default (1); we edit the kid's
        # mailbox by its URL id.
        client.cookies.set("yts_user_id", "1")
        client.post(
            f"/profiles/{kid_id}/imap",
            data={
                "imap_enabled": "1",
                "imap_host": "imap.kids.example",
                "imap_ssl": "1",
                "imap_username": "kids@example.com",
                "imap_password": "kidpw",
            },
            follow_redirects=False,
        )

        async def read(uid):
            from app.repos import settings as settings_repo
            return await settings_repo.get_all_for_user(app.state.db, uid)

        kid_settings = _run(read(kid_id))
        default_settings = _run(read(1))

    assert kid_settings["imap_host"] == "imap.kids.example"
    # The active (cookie) profile must NOT have been written.
    assert "imap_host" not in default_settings


def test_save_imap_404_for_missing_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/profiles/9999/imap",
            data={"imap_host": "x", "imap_username": "y", "imap_password": "z"},
            follow_redirects=False,
        )
    assert resp.status_code == 404


def test_save_imap_persists_own_addresses(tmp_path, monkeypatch):
    """The 'your own addresses' field (forward-to-summarize) is saved and
    rendered back into the form."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        client.post(
            "/profiles/1/imap",
            data={
                "imap_host": "imap.mailbox.org", "imap_ssl": "1",
                "imap_username": "news@example.com", "imap_password": "pw",
                "mail_own_addresses": "stefan@gmail.com, work@company.com",
            },
            follow_redirects=False,
        )

        async def read():
            from app.repos import settings as settings_repo
            return await settings_repo.get_all_for_user(app.state.db, 1)

        stored = _run(read())
        assert stored["mail_own_addresses"] == "stefan@gmail.com, work@company.com"

        page = client.get("/profiles/1/edit")
    assert "stefan@gmail.com" in page.text
