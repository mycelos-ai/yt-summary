import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.repos import users as users_repo


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------- Newsletter (IMAP) tests ----------


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


def test_save_own_address_evicts_existing_candidate(tmp_path, monkeypatch):
    """Registering an own address removes it from the newsletter
    candidate list if an earlier scan had added it."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            from app.repos import mail_senders as repo
            await repo.upsert_discovered(
                app.state.db, 1, [("stefan@gmail.com", "Stefan", None, None)]
            )

        _run(seed())

        client.post(
            "/profiles/1/imap",
            data={
                "imap_host": "imap.x", "imap_ssl": "1",
                "imap_username": "u@x", "imap_password": "pw",
                "mail_own_addresses": "stefan@gmail.com",
            },
            follow_redirects=False,
        )

        async def read():
            from app.repos import mail_senders as repo
            return {s.sender_addr for s in await repo.list_for_user(app.state.db, 1)}

        addrs = _run(read())
    assert "stefan@gmail.com" not in addrs


# ---------- Interest profile + digest prefs tests ----------


def test_profile_edit_renders_interest_profile_section(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/profiles/1/edit")
    assert resp.status_code == 200
    assert "Interest profile" in resp.text
    assert "Daily digest" in resp.text


def test_post_interest_profile_updates_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/profiles/1/interest-profile",
            data={
                "markdown": "- I care about LLM cost",
                "expected_version": "0",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        show = client.get("/profiles/1/edit")
    assert "I care about LLM cost" in show.text


def test_post_interest_profile_rejects_cross_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed_user2():
            await app.state.db.execute(
                "INSERT INTO users (id, name) VALUES (2, 'other')"
            )
            await app.state.db.commit()
        _run(seed_user2())
        resp = client.post(
            "/profiles/2/interest-profile",
            data={"markdown": "x", "expected_version": "0"},
            follow_redirects=False,
        )
    assert resp.status_code == 403


def test_post_interest_profile_optimistic_lock_conflict(tmp_path, monkeypatch):
    """Posting with a stale version → 409."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # First write succeeds, bumps version to 1.
        client.post(
            "/profiles/1/interest-profile",
            data={"markdown": "v1", "expected_version": "0"},
            follow_redirects=False,
        )
        # Second write with stale version=0 → 409.
        resp = client.post(
            "/profiles/1/interest-profile",
            data={"markdown": "v2", "expected_version": "0"},
            follow_redirects=False,
        )
    assert resp.status_code == 409


def test_post_digest_prefs_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/profiles/1/digest-prefs",
            data={"digest_enabled": "on", "digest_hour_local": "8"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            enabled, hour = await users_repo.get_digest_prefs(
                app.state.db, user_id=1,
            )
            return enabled, hour
        enabled, hour = _run(check())
    assert enabled is True
    assert hour == 8


def test_post_digest_prefs_off_when_checkbox_unchecked(tmp_path, monkeypatch):
    """Browsers omit unchecked checkboxes from form data; handler must
    interpret missing key as False."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        client.post(
            "/profiles/1/digest-prefs",
            data={"digest_enabled": "on", "digest_hour_local": "8"},
            follow_redirects=False,
        )
        # Re-submit without digest_enabled → unchecked
        client.post(
            "/profiles/1/digest-prefs",
            data={"digest_hour_local": "9"},
            follow_redirects=False,
        )

        async def check():
            return await users_repo.get_digest_prefs(
                app.state.db, user_id=1,
            )
        enabled, hour = _run(check())
    assert enabled is False
    assert hour == 9


def test_post_rebuild_profile_calls_service(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    from app.routes import profiles as profiles_route
    called = {}

    async def fake_rebuild(db, *, user_id):
        called["user_id"] = user_id

    monkeypatch.setattr(profiles_route, "rebuild_profile", fake_rebuild)
    with TestClient(app) as client:
        resp = client.post(
            "/profiles/1/interest-profile/rebuild",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert called["user_id"] == 1
