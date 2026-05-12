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


def test_save_settings_persists_playlist_fields_in_minutes(tmp_path, monkeypatch):
    """The form now stores intervals in minutes; saving 45 minutes
    should land verbatim and the legacy hours setting should be
    cleared so the scheduler has one source of truth."""
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
                "playlist_refresh_interval_minutes": "45",
                "playlist_initial_import_limit": "30",
            },
            follow_redirects=False,
        )
        import asyncio

        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            assert s["playlist_refresh_interval_minutes"] == "45"
            assert s["playlist_initial_import_limit"] == "30"
            # Legacy field should not have been written.
            assert "playlist_refresh_interval_hours" not in s

        asyncio.get_event_loop().run_until_complete(check())


def test_save_settings_legacy_hours_form_migrates_to_minutes(tmp_path, monkeypatch):
    """A client that still posts the old `_hours` field (e.g. an
    older bookmark or scripted update) should be honoured — but
    normalised to minutes in storage so there's never a mix."""
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
                "playlist_refresh_interval_hours": "2",
                "playlist_initial_import_limit": "30",
            },
            follow_redirects=False,
        )
        import asyncio

        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            assert s["playlist_refresh_interval_minutes"] == "120"
            assert "playlist_refresh_interval_hours" not in s

        asyncio.get_event_loop().run_until_complete(check())


def test_settings_form_renders_playlist_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    # New minutes-based field is rendered.
    assert "playlist_refresh_interval_minutes" in resp.text
    assert "playlist_initial_import_limit" in resp.text


def test_settings_form_shows_legacy_hours_value_as_minutes(tmp_path, monkeypatch):
    """If an install has only the old `_hours` value in settings (no
    UI save since the upgrade), the page renders it as the equivalent
    minutes value so the user sees what's actually configured."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio

    async def seed():
        from app.repos import settings as settings_repo
        await settings_repo.set(
            app.state.db, "playlist_refresh_interval_hours", "3"
        )

    with TestClient(app) as client:
        asyncio.get_event_loop().run_until_complete(seed())
        resp = client.get("/settings")
    # 3 hours → 180 minutes pre-filled in the input.
    assert 'value="180"' in resp.text


def test_settings_page_shows_scheduler_last_tick_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio

    async def seed():
        from app.repos import settings as settings_repo
        await settings_repo.set(
            app.state.db, "scheduler_last_tick_at", "2026-05-11T12:00:00+00:00"
        )

    with TestClient(app) as client:
        asyncio.get_event_loop().run_until_complete(seed())
        resp = client.get("/settings")
    assert "2026-05-11T12:00:00+00:00" in resp.text
    assert "Last scheduler tick" in resp.text


def test_settings_page_shows_no_tick_yet_message(tmp_path, monkeypatch):
    """Fresh install with no recorded tick: the user should see a
    helpful note rather than an empty field that looks broken."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert "No tick recorded yet" in resp.text


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


def test_api_key_reveal_page_includes_curl_and_mcp_snippets(tmp_path, monkeypatch):
    """After generating, the reveal page should hand the user three
    ready-to-paste blocks: the key, two curl examples, and an MCP
    config — all prefilled with the actual key and host."""
    import re

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/api-key/generate", follow_redirects=False
        )
    assert resp.status_code == 200
    text = resp.text
    # Pull the actual generated plaintext key out of the response, then
    # assert the snippets reference it verbatim.
    m = re.search(r"yts_[a-z0-9]+", text)
    assert m is not None, "expected a yts_ key in the response"
    key = m.group(0)

    # 1. The key block stays
    assert key in text

    # 2. curl health check + submit examples appear, prefilled. curl
    # itself is fine with the conventional spaced header form.
    assert f"Authorization: Bearer {key}" in text
    assert "/api/v1/health" in text
    assert "/api/v1/videos" in text

    # 3. MCP config block — both Claude Desktop JSON and Claude Code CLI.
    assert "mcp-remote" in text
    assert "/mcp/sse" in text
    assert "claude mcp add" in text
    # Claude Desktop config path hint is shown.
    assert "claude_desktop_config.json" in text

    # The host the user is reaching us from must be in the URLs. The
    # TestClient defaults to `testserver`.
    assert "http://testserver/api/v1/health" in text
    assert "http://testserver/mcp/sse" in text

    # And the back-to-settings button still goes home.
    assert 'href="/settings"' in text


def test_api_key_reveal_mcp_snippets_use_no_space_header(tmp_path, monkeypatch):
    """mcp-remote splits --header on whitespace and silently drops
    everything after the first space, leaving an empty header. The
    space-less form parses correctly. The two MCP snippets (Claude
    Code CLI and Claude Desktop JSON) must therefore use
    'Authorization:Bearer <key>' (no space)."""
    import re

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/api-key/generate", follow_redirects=False
        )
    text = resp.text
    m = re.search(r"yts_[a-z0-9]+", text)
    assert m is not None
    key = m.group(0)

    # Both MCP blocks must use the no-space form.
    assert f"Authorization:Bearer {key}" in text
    # And neither MCP block may use the spaced form. The curl block
    # uses the spaced form, so we look only inside the MCP section by
    # checking that the spaced form doesn't appear next to mcp-remote.
    # Easier: count how many times the spaced form appears (curl health
    # + curl submit = 2). If a third creeps in, it's an MCP regression.
    assert text.count(f"Authorization: Bearer {key}") == 2


def test_api_key_reveal_mcp_snippets_pass_allow_http_for_http_host(tmp_path, monkeypatch):
    """mcp-remote refuses non-https URLs unless --allow-http is passed.
    Since the TestClient runs over http (testserver), both MCP blocks
    must include --allow-http."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/api-key/generate", follow_redirects=False
        )
    text = resp.text
    # Claude Code CLI block: --allow-http appears verbatim.
    assert "--allow-http" in text
    # JSON args array: each token is its own quoted string, so the
    # flag shows up as "--allow-http" in the args.
    assert '"--allow-http"' in text


def test_api_key_reveal_claude_cli_uses_mcp_remote_with_double_dash(tmp_path, monkeypatch):
    """The Claude Code CLI snippet must invoke mcp-remote as a stdio
    command, which means the form is 'claude mcp add yt-summary --
    npx -y mcp-remote ...'. The leading -- is what tells claude mcp
    add to treat the rest as a command-to-run rather than an SSE URL."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/api-key/generate", follow_redirects=False
        )
    text = resp.text
    assert "claude mcp add yt-summary -- npx -y mcp-remote" in text


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


def test_test_whisper_without_sample_returns_warning(tmp_path, monkeypatch):
    """If the bundled sample file is missing somehow, the route still
    responds gracefully (not a 500)."""
    from unittest.mock import patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with (
        TestClient(app) as client,
        patch("app.routes.settings.WHISPER_TEST_SAMPLE", tmp_path / "missing.m4a"),
    ):
        resp = client.post("/settings/test-whisper")
    assert resp.status_code == 200
    assert "sample" in resp.text.lower()


def test_test_whisper_local_path(tmp_path, monkeypatch):
    """When no whisper_base_url is set, hits the local transcribe()."""
    from unittest.mock import patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with (
        TestClient(app) as client,
        patch(
            "app.routes.settings.transcribe",
            return_value="this is a test",
        ),
    ):
        resp = client.post("/settings/test-whisper")
    assert resp.status_code == 200
    assert "this is a test" in resp.text


def test_test_whisper_api_path(tmp_path, monkeypatch):
    """When whisper_base_url is set, hits transcribe_via_api."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(
                app.state.db, "whisper_base_url",
                "https://api.groq.com/openai/v1",
            )
            await settings_repo.set(app.state.db, "whisper_api_key", "gsk-x")
            await settings_repo.set(app.state.db, "whisper_model", "whisper-large-v3")

        asyncio.get_event_loop().run_until_complete(setup())
        with patch(
            "app.routes.settings.transcribe_via_api",
            AsyncMock(return_value="hosted whisper text"),
        ) as api_mock:
            resp = client.post("/settings/test-whisper")
    assert resp.status_code == 200
    assert "hosted whisper text" in resp.text
    api_mock.assert_called_once()
    kwargs = api_mock.call_args.kwargs
    assert kwargs["base_url"] == "https://api.groq.com/openai/v1"
    assert kwargs["api_key"] == "gsk-x"
    assert kwargs["model_name"] == "whisper-large-v3"


def test_test_whisper_reports_error(tmp_path, monkeypatch):
    """If the API path raises, the route shows the error string."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(
                app.state.db, "whisper_base_url", "https://x"
            )

        asyncio.get_event_loop().run_until_complete(setup())
        with patch(
            "app.routes.settings.transcribe_via_api",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.post("/settings/test-whisper")
    assert resp.status_code == 200
    assert "boom" in resp.text


def test_test_embedding_without_model_returns_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/settings/test-embedding")
    # No embedding_model and no LLM base URL fallback → "configure"
    assert resp.status_code == 200
    assert "embed" in resp.text.lower()


def test_test_embedding_returns_dimension(tmp_path, monkeypatch):
    """Successful embedding test reports the vector dimension and a
    short preview of the values."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(
                app.state.db, "embedding_model", "ollama/nomic-embed-text"
            )
            await settings_repo.set(
                app.state.db, "embedding_base_url", "http://localhost:11434"
            )

        asyncio.get_event_loop().run_until_complete(setup())
        with patch(
            "app.routes.settings.embed_text",
            AsyncMock(return_value=[0.1, 0.2, 0.3] * 256),  # 768-dim
        ):
            resp = client.post("/settings/test-embedding")
    assert resp.status_code == 200
    assert "768" in resp.text
    assert "ollama/nomic-embed-text" in resp.text


def test_quick_setup_anthropic_writes_llm_only(tmp_path, monkeypatch):
    """POSTing to /settings/quick-setup with provider=anthropic should
    set llm_model + llm_api_key but leave embedding/whisper alone."""
    import asyncio

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:

        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(
                app.state.db, "embedding_model", "ollama/nomic-embed-text"
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            "/settings/quick-setup",
            data={"provider": "anthropic", "api_key": "sk-ant-test"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "applied=anthropic" in resp.headers.get("location", "")

        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            assert s["llm_model"] == "anthropic/claude-sonnet-4-6"
            assert s["llm_api_key"] == "sk-ant-test"
            # Embedding untouched
            assert s["embedding_model"] == "ollama/nomic-embed-text"

        asyncio.get_event_loop().run_until_complete(check())


def test_quick_setup_groq_sets_whisper(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/quick-setup",
            data={"provider": "groq", "api_key": "gsk-test"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            assert s["whisper_base_url"] == "https://api.groq.com/openai/v1"
            assert s["whisper_model"] == "whisper-large-v3"
            assert s["whisper_api_key"] == "gsk-test"
            assert s["llm_api_key"] == "gsk-test"

        asyncio.get_event_loop().run_until_complete(check())


def test_quick_setup_blank_key_keeps_existing(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:

        async def setup():
            from app.repos import settings as settings_repo
            await settings_repo.set(
                app.state.db, "llm_api_key", "old-existing-key"
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post(
            "/settings/quick-setup",
            data={"provider": "openai", "api_key": ""},  # blank
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            # llm_api_key kept
            assert s["llm_api_key"] == "old-existing-key"
            # llm_model still got swapped to the OpenAI preset's
            # current default (without hard-coding which one — bumping
            # the default shouldn't churn tests).
            from app.services.providers import get_preset
            assert s["llm_model"] == get_preset("openai").default_llm

        asyncio.get_event_loop().run_until_complete(check())


def test_quick_setup_unknown_provider_400(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/quick-setup",
            data={"provider": "bogus", "api_key": "x"},
            follow_redirects=False,
        )
    assert resp.status_code == 400


def test_settings_page_renders_quick_setup_card(tmp_path, monkeypatch):
    """The settings page must include the Quick Setup card with all
    six provider options."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    text = resp.text
    assert "Quick setup" in text
    for provider_label in (
        "OpenAI", "Anthropic", "Google Gemini",
        "Groq", "Ollama", "OpenRouter",
    ):
        assert provider_label in text


def test_settings_page_shows_applied_banner(tmp_path, monkeypatch):
    """When ?applied=<provider> is in the query string, render a
    success banner so the user knows the preset took effect."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings?applied=groq")
    assert resp.status_code == 200
    assert "Groq" in resp.text
    # Some indicator of success
    assert "applied" in resp.text.lower() or "✓" in resp.text


def test_quick_setup_ollama_models_empty_url(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings/quick-setup/ollama-models")
    assert resp.status_code == 200
    assert "Enter a server URL" in resp.text


def test_quick_setup_ollama_models_renders_select(tmp_path, monkeypatch):
    """Successful fetch returns a <select name='llm_model'> with all
    pulled models prefixed for chat use."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with (
        TestClient(app) as client,
        patch(
            "app.routes.settings.fetch_ollama_models",
            AsyncMock(return_value=["llama3.1:latest", "qwen2.5:14b"]),
        ),
    ):
        resp = client.get(
            "/settings/quick-setup/ollama-models",
            params={"llm_base_url": "http://192.168.0.27:11434"},
        )
    assert resp.status_code == 200
    assert 'name="llm_model"' in resp.text
    assert "ollama_chat/llama3.1:latest" in resp.text
    assert "ollama_chat/qwen2.5:14b" in resp.text
    assert "Found 2 models" in resp.text


def test_quick_setup_ollama_models_handles_fetch_error(tmp_path, monkeypatch):
    """If the server isn't reachable, surface the error inline."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with (
        TestClient(app) as client,
        patch(
            "app.routes.settings.fetch_ollama_models",
            AsyncMock(side_effect=ConnectionError("refused")),
        ),
    ):
        resp = client.get(
            "/settings/quick-setup/ollama-models",
            params={"llm_base_url": "http://nope:11434"},
        )
    assert resp.status_code == 200
    assert "Cannot reach Ollama" in resp.text


def test_quick_setup_ollama_models_renders_both_dropdowns(tmp_path, monkeypatch):
    """When the server has both chat and embedding models, render two
    separate selects so the user can pick each."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with (
        TestClient(app) as client,
        patch(
            "app.routes.settings.fetch_ollama_models",
            AsyncMock(return_value=[
                "llama3.1:latest",
                "nomic-embed-text:latest",
                "qwen2.5:14b",
                "mxbai-embed-large:latest",
            ]),
        ),
    ):
        resp = client.get(
            "/settings/quick-setup/ollama-models",
            params={"llm_base_url": "http://x:11434"},
        )
    assert resp.status_code == 200
    text = resp.text
    # Two named selects
    assert 'name="llm_model"' in text
    assert 'name="embedding_model"' in text
    # Chat tags appear as ollama_chat/...
    assert "ollama_chat/llama3.1:latest" in text
    assert "ollama_chat/qwen2.5:14b" in text
    # Embedding tags appear as ollama/... (no _chat)
    assert "ollama/nomic-embed-text:latest" in text
    assert "ollama/mxbai-embed-large:latest" in text


def test_quick_setup_ollama_models_no_embedders(tmp_path, monkeypatch):
    """If the server has no embedding models, omit that dropdown but
    still render the chat one."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with (
        TestClient(app) as client,
        patch(
            "app.routes.settings.fetch_ollama_models",
            AsyncMock(return_value=["llama3.1", "qwen2.5"]),
        ),
    ):
        resp = client.get(
            "/settings/quick-setup/ollama-models",
            params={"llm_base_url": "http://x:11434"},
        )
    assert resp.status_code == 200
    assert 'name="llm_model"' in resp.text
    # No embedding select rendered
    assert 'name="embedding_model"' not in resp.text


def test_settings_page_renders_cloud_provider_dropdowns(tmp_path, monkeypatch):
    """Cloud provider Quick Setup details should expose a dropdown of
    available LLM models from LiteLLM's static map, not just a static
    'will set' summary."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    text = resp.text
    # Groq's curated default — currently llama-3.3-70b-versatile (the
    # boring, longest-running production model). We don't default to
    # whatever the latest hotness is because Groq delists those fast
    # (Kimi K2 in 2025, Llama 4 Maverick in May 2026).
    assert "llama-3.3-70b-versatile" in text
    # Other curated Groq models also in the dropdown
    assert "qwen3-32b" in text
    # Cloud presets render real selects; Anthropic shows Claude variants
    assert "claude" in text.lower()


def test_settings_page_renders_cloud_embedding_dropdowns(tmp_path, monkeypatch):
    """Providers with embedding support (OpenAI, Gemini) should render
    an embedding model dropdown, but Anthropic and Groq should not."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    text = resp.text
    # The embedding select for the wizard is named embedding_model.
    # OpenAI's default text-embedding-3-small should be visible.
    assert "text-embedding-3-small" in text


def test_settings_form_renders_tts_card(tmp_path, monkeypatch):
    """The Audio (TTS) card with default language / per-language voice /
    quality fields renders on /settings."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert "Audio (TTS)" in resp.text
    assert "default_tts_language" in resp.text
    assert "default_tts_quality" in resp.text
    # Per-language voice fields (Option A: per-language voice defaults).
    assert "default_tts_voice_de" in resp.text
    assert "default_tts_voice_en_US" in resp.text
    assert "default_tts_voice_en_GB" in resp.text
    assert "default_tts_voice_fr" in resp.text
    assert "default_tts_voice_es" in resp.text


def test_save_settings_persists_tts_defaults(tmp_path, monkeypatch):
    """POST with TTS fields persists each to the settings table; empty
    values land as deletions (the established set/delete pattern)."""
    import asyncio

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings",
            data={
                "llm_model": "openai/gpt-4o",
                "llm_api_key": "",
                "llm_base_url": "",
                "whisper_model": "small",
                "default_tts_language": "de",
                "default_tts_voice_de": "thorsten",
                "default_tts_voice_en_US": "lessac",
                "default_tts_voice_en_GB": "",
                "default_tts_voice_fr": "",
                "default_tts_voice_es": "",
                "default_tts_quality": "high",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            assert s["default_tts_language"] == "de"
            assert s["default_tts_voice_de"] == "thorsten"
            assert s["default_tts_voice_en_US"] == "lessac"
            # Empty fields should NOT exist as keys.
            assert "default_tts_voice_en_GB" not in s
            assert "default_tts_voice_fr" not in s
            assert "default_tts_voice_es" not in s
            assert s["default_tts_quality"] == "high"

        asyncio.get_event_loop().run_until_complete(check())


def test_save_settings_persists_tts_length_scale(tmp_path, monkeypatch):
    """A valid length_scale (in [0.5, 2.0]) is persisted formatted to
    two decimals — keeps storage tidy so the form re-renders the same
    value the user typed without floating-point noise."""
    import asyncio

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings",
            data={
                "llm_model": "openai/gpt-4o",
                "llm_api_key": "",
                "llm_base_url": "",
                "whisper_model": "small",
                "default_tts_length_scale": "1.20",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        async def check():
            from app.repos import settings as settings_repo
            assert (
                await settings_repo.get(
                    app.state.db, "default_tts_length_scale"
                )
                == "1.20"
            )

        asyncio.get_event_loop().run_until_complete(check())


def test_save_settings_rejects_out_of_range_length_scale(tmp_path, monkeypatch):
    """Values outside [0.5, 2.0] (here: 5.0) must clear the setting
    rather than save it — better to fall back to the voice default
    than ship the user a 5x-slow render they didn't ask for."""
    import asyncio

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Seed a prior valid value so we can confirm it gets deleted.
        async def seed():
            from app.repos import settings as settings_repo
            await settings_repo.set(
                app.state.db, "default_tts_length_scale", "1.20"
            )

        asyncio.get_event_loop().run_until_complete(seed())
        resp = client.post(
            "/settings",
            data={
                "llm_model": "openai/gpt-4o",
                "llm_api_key": "",
                "llm_base_url": "",
                "whisper_model": "small",
                "default_tts_length_scale": "5.0",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            assert "default_tts_length_scale" not in s

        asyncio.get_event_loop().run_until_complete(check())


def test_settings_shows_voice_cache_size(tmp_path, monkeypatch):
    """Pre-create two fake .onnx voice files in the cache dir; the
    settings page reports the count and total size in MB."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    voices = tmp_path / "tts-voices"
    voices.mkdir(parents=True, exist_ok=True)
    (voices / "a.onnx").write_bytes(b"\x00" * 1024 * 1024)
    (voices / "b.onnx").write_bytes(b"\x00" * 2 * 1024 * 1024)
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert "2 voices installed" in resp.text
    assert "3" in resp.text  # 3 MB
