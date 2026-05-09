"""Tests for the first-time-user onboarding wizard.

Wizard flow: /onboarding/welcome → provider → profile → first-content
→ finish. Skip link from any step jumps to /settings?onboarding=done.

The home route ("/") redirects new users to /onboarding/welcome the
first time they hit the box. Once `onboarding_completed=1` is in
settings, the redirect stops and the home page renders normally.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import _onboarding_status, create_app


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── _onboarding_status helper ──────────────────────────────────────


def test_onboarding_status_pending_when_no_key_no_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        result = _run(_onboarding_status(app.state.db))
    assert result == {"pending": True, "next_step": "/onboarding/welcome"}


def test_onboarding_status_not_pending_when_completed_marker_set(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "onboarding_completed", "1")

        _run(setup())
        result = _run(_onboarding_status(app.state.db))
    assert result == {"pending": False, "next_step": None}


def test_onboarding_status_not_pending_when_llm_model_already_set(
    tmp_path, monkeypatch
):
    """User configured a model manually before onboarding shipped —
    don't ambush them with a wizard on next launch."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "llm_model", "openai/gpt-4o")
            await settings_repo.set(
                app.state.db, "llm_api_key", "preexisting"
            )

        _run(setup())
        result = _run(_onboarding_status(app.state.db))
    assert result == {"pending": False, "next_step": None}


def test_onboarding_status_not_pending_for_ollama_no_api_key(
    tmp_path, monkeypatch
):
    """Ollama setups have a model and a base URL but no API key.
    That's a perfectly valid configuration — don't push them into
    the wizard. Regression test for the bug where the heuristic
    keyed off llm_api_key alone."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(
                app.state.db, "llm_model", "ollama_chat/llama3.1"
            )
            await settings_repo.set(
                app.state.db, "llm_base_url",
                "http://host.docker.internal:11434",
            )
            # llm_api_key intentionally left unset

        _run(setup())
        result = _run(_onboarding_status(app.state.db))
    assert result == {"pending": False, "next_step": None}


# ── home redirect ──────────────────────────────────────────────────


def test_home_redirects_to_onboarding_when_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding/welcome"


def test_home_does_not_redirect_when_onboarding_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "onboarding_completed", "1")

        _run(setup())
        resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200


def test_home_does_not_redirect_when_llm_model_set(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "llm_model", "openai/gpt-4o")
            await settings_repo.set(
                app.state.db, "llm_api_key", "preexisting"
            )

        _run(setup())
        resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200


def test_home_does_not_redirect_for_ollama_no_api_key(tmp_path, monkeypatch):
    """Regression: Ollama users have llm_model set but no llm_api_key.
    They should NOT see the onboarding wizard on next launch."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(
                app.state.db, "llm_model", "ollama_chat/llama3.1"
            )

        _run(setup())
        resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200


# ── wizard pages render even after setup ───────────────────────────


def test_welcome_renders_regardless_of_state(tmp_path, monkeypatch):
    """Hitting /onboarding/welcome directly should always render — the
    user might want to redo the flow even after setup."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "onboarding_completed", "1")

        _run(setup())
        resp = client.get("/onboarding/welcome")
    assert resp.status_code == 200
    assert "Welcome to yt-summary" in resp.text
    # Embedded promo video
    assert "wUkqSNn63Hk" in resp.text
    # Skip path is reachable
    assert "/onboarding/skip" in resp.text


def test_welcome_renders_for_fresh_user(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/onboarding/welcome")
    assert resp.status_code == 200
    assert "/onboarding/provider" in resp.text


def test_provider_step_renders_all_six_tiles(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/onboarding/provider")
    assert resp.status_code == 200
    # Each preset id shows up in the form
    for pid in ("openai", "anthropic", "gemini", "groq", "ollama", "openrouter"):
        assert f'value="{pid}"' in resp.text


def test_profile_step_prefills_current_user_name(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/onboarding/profile")
    assert resp.status_code == 200
    # The default seeded user is named "admin"
    assert 'value="admin"' in resp.text or 'value="admin"' in resp.text.lower()


def test_first_content_step_renders_two_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/onboarding/first-content")
    assert resp.status_code == 200
    # The two action endpoints are present
    assert 'action="/videos"' in resp.text
    assert 'action="/playlists"' in resp.text
    # Direct-finish form
    assert 'action="/onboarding/finish"' in resp.text


# ── POST handlers ──────────────────────────────────────────────────


def test_post_skip_sets_marker_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/onboarding/skip", follow_redirects=False)
    assert resp.status_code == 303
    # Redirects to home, not /settings — the welcome banner lives on /
    # so the user lands on the actual tool, not the config page.
    assert resp.headers["location"] == "/?onboarding=done"

    async def check():
        from app.repos import settings as settings_repo
        s = await settings_repo.get_all(app.state.db)
        assert s.get("onboarding_completed") == "1"

    with TestClient(app):
        _run(check())


def test_post_provider_writes_settings_and_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/onboarding/provider",
            data={"provider": "openai", "api_key": "sk-test-onboarding"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding/profile"

    async def check():
        from app.repos import settings as settings_repo
        s = await settings_repo.get_all(app.state.db)
        # apply_preset wrote the openai preset's defaults
        assert s["llm_api_key"] == "sk-test-onboarding"
        assert s["llm_model"].startswith("openai/")

    with TestClient(app):
        _run(check())


def test_post_provider_ollama_no_api_key_writes_base_url(tmp_path, monkeypatch):
    """Ollama doesn't require an API key — the form posts a base URL
    and that should still be saved."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/onboarding/provider",
            data={
                "provider": "ollama",
                "api_key": "",
                "llm_base_url": "http://192.168.1.42:11434",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding/profile"

    async def check():
        from app.repos import settings as settings_repo
        s = await settings_repo.get_all(app.state.db)
        assert s["llm_base_url"] == "http://192.168.1.42:11434"

    with TestClient(app):
        _run(check())


def test_post_profile_updates_name_and_avatar(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/onboarding/profile",
            data={"name": "Stefan", "avatar_image": "adult-scientist-m"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding/first-content"

    async def check():
        from app.repos import users as users_repo
        u = await users_repo.get_by_id(app.state.db, 1)
        assert u is not None
        assert u.name == "Stefan"
        assert u.avatar_image == "adult-scientist-m"

    with TestClient(app):
        _run(check())


def test_post_profile_rejects_bogus_avatar(tmp_path, monkeypatch):
    """Avatar id must be in the curated library — anything else is
    silently ignored, same pattern as the profile-edit route."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/onboarding/profile",
            data={"name": "Stefan", "avatar_image": "../etc/passwd"},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    async def check():
        from app.repos import users as users_repo
        u = await users_repo.get_by_id(app.state.db, 1)
        assert u is not None
        assert u.avatar_image == ""

    with TestClient(app):
        _run(check())


def test_post_finish_sets_marker_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/onboarding/finish", follow_redirects=False)
    assert resp.status_code == 303
    # Same as /skip: home page with the welcome banner, not /settings.
    assert resp.headers["location"] == "/?onboarding=done"

    async def check():
        from app.repos import settings as settings_repo
        s = await settings_repo.get_all(app.state.db)
        assert s.get("onboarding_completed") == "1"

    with TestClient(app):
        _run(check())


# ── home + settings page banners ───────────────────────────────────


def test_home_shows_welcome_banner_when_query_flag_present(tmp_path, monkeypatch):
    """After finish/skip, the user lands on / with the welcome banner.
    The banner copy points at /settings as the next-stop for tweaks."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import settings as settings_repo
            # Mark onboarding done so / doesn't redirect us back to it.
            await settings_repo.set(app.state.db, "onboarding_completed", "1")

        _run(setup())
        resp = client.get("/?onboarding=done")
    assert resp.status_code == 200
    assert "You&#39;re all set" in resp.text or "You're all set" in resp.text
    # Banner links to settings as the next destination.
    assert 'href="/settings"' in resp.text


def test_home_no_welcome_banner_without_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "onboarding_completed", "1")

        _run(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "You're all set" not in resp.text


def test_settings_page_shows_banner_when_query_flag_present(tmp_path, monkeypatch):
    """The /settings banner stays as a fallback for users who deep-link
    there with ?onboarding=done — but the wizard itself no longer
    routes them there."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings?onboarding=done")
    assert resp.status_code == 200
    assert "Setup complete" in resp.text


def test_settings_page_no_banner_without_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Setup complete" not in resp.text


# ── _onboarding_status invocation contract ─────────────────────────


@pytest.mark.asyncio
async def test_onboarding_status_returns_expected_keys(db):
    """Smoke test the helper signature: it must return a dict with
    'pending' and 'next_step' keys."""
    result = await _onboarding_status(db)
    assert "pending" in result
    assert "next_step" in result
