from fastapi.testclient import TestClient

from app.main import create_app


def test_get_settings_renders_form(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "llm_model" in resp.text
    assert "whisper_model" in resp.text


def test_post_settings_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/settings", data={
            "llm_model": "openai/gpt-4o",
            "llm_api_key": "k",
            "llm_base_url": "",
            "whisper_model": "small",
        }, follow_redirects=False)
        assert resp.status_code in (200, 303)

        import asyncio
        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            assert s["llm_model"] == "openai/gpt-4o"
            assert s["whisper_model"] == "small"
        asyncio.get_event_loop().run_until_complete(check())


def test_post_youtube_curl_writes_cookie_file(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        curl_text = "curl 'https://www.youtube.com/' -H 'cookie: A=1; B=2'"
        resp = client.post(
            "/settings/youtube-curl",
            data={"curl": curl_text},
            follow_redirects=False,
        )
    assert resp.status_code in (200, 303)
    cookies_file = tmp_path / "cookies.txt"
    assert cookies_file.exists()
    assert "A\t1" in cookies_file.read_text()


def test_test_llm_without_model_returns_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/settings/test-llm")
    assert resp.status_code == 200
    assert "Configure a model first" in resp.text


def test_test_llm_with_model_calls_litellm(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "llm_model", "ollama_chat/llama3.1")
            await settings_repo.set(app.state.db, "llm_base_url", "http://localhost:11434")

        asyncio.get_event_loop().run_until_complete(setup())

        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "ok"
        with (
            patch(
                "app.routes.settings._probe_ollama_reachable",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.routes.settings.litellm.acompletion",
                AsyncMock(return_value=fake_response),
            ),
        ):
            resp = client.post("/settings/test-llm")
    assert resp.status_code == 200
    assert "ollama_chat/llama3.1" in resp.text
    assert "responded" in resp.text


def test_test_llm_ollama_reachability_error_is_clear(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "llm_model", "ollama_chat/x")
            await settings_repo.set(app.state.db, "llm_base_url", "http://1.2.3.4:11434")

        asyncio.get_event_loop().run_until_complete(setup())

        with patch(
            "app.routes.settings._probe_ollama_reachable",
            AsyncMock(return_value="ConnectError: timeout"),
        ):
            resp = client.post("/settings/test-llm")
    assert resp.status_code == 200
    assert "Cannot reach Ollama" in resp.text
    assert "ConnectError" in resp.text


def test_save_settings_with_blank_api_key_keeps_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "llm_api_key", "secret123")

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            "/settings",
            data={
                "llm_model": "openai/gpt-4o",
                "llm_api_key": "",  # blank
                "llm_base_url": "",
                "whisper_model": "small",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        async def check():
            from app.repos import settings as settings_repo
            assert await settings_repo.get(app.state.db, "llm_api_key") == "secret123"

        asyncio.get_event_loop().run_until_complete(check())


def test_settings_page_does_not_leak_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(app.state.db, "llm_api_key", "supersecret")

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "supersecret" not in resp.text


def test_save_settings_strips_trailing_slash_from_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        client.post(
            "/settings",
            data={
                "llm_model": "ollama_chat/gemma4:latest",
                "llm_api_key": "",
                "llm_base_url": "http://192.168.0.27:11434/",
                "whisper_model": "small",
            },
            follow_redirects=False,
        )

        import asyncio

        async def check():
            from app.repos import settings as settings_repo
            value = await settings_repo.get(app.state.db, "llm_base_url")
            assert value == "http://192.168.0.27:11434"

        asyncio.get_event_loop().run_until_complete(check())


def test_save_settings_persists_playlist_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        client.post(
            "/settings",
            data={
                "llm_model": "openai/gpt-4o",
                "llm_api_key": "",
                "llm_base_url": "",
                "whisper_model": "small",
                "playlist_refresh_interval_hours": "12",
                "playlist_initial_import_limit": "30",
            },
            follow_redirects=False,
        )
        import asyncio

        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            assert s["playlist_refresh_interval_hours"] == "12"
            assert s["playlist_initial_import_limit"] == "30"

        asyncio.get_event_loop().run_until_complete(check())


def test_settings_form_renders_playlist_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert "playlist_refresh_interval_hours" in resp.text
    assert "playlist_initial_import_limit" in resp.text


def test_generate_api_key_creates_key_and_shows_once(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/api-key/generate", follow_redirects=False
        )
    assert resp.status_code == 200
    assert "yts_" in resp.text
    # The reveal page warns the user it's shown once.
    text_lower = resp.text.lower()
    assert (
        "shown only once" in text_lower
        or "show only once" in text_lower
        or "copy it now" in text_lower
    )


def test_settings_page_shows_api_key_prefix_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        client.post("/settings/api-key/generate", follow_redirects=False)
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "yts_" in resp.text
    assert "..." in resp.text


def test_revoke_api_key_clears_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        client.post("/settings/api-key/generate", follow_redirects=False)
        resp = client.post(
            "/settings/api-key/revoke", follow_redirects=False
        )
        assert resp.status_code in (200, 303)

        import asyncio
        async def check():
            from app.repos import users as users_repo
            user = await users_repo.get_default_user(app.state.db)
            assert user is not None
            assert user.api_key_hash is None

        asyncio.get_event_loop().run_until_complete(check())
