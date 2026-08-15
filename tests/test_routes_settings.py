from fastapi.testclient import TestClient

from app.main import create_app


def test_get_settings_renders_form(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Configured models" in resp.text
    assert "whisper_model" in resp.text


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


def test_settings_page_does_not_leak_llm_model_key(tmp_path, monkeypatch):
    """A configured-model row's api_key must never reach the browser —
    neither in the model list nor in the edit form. The form is
    write-only ('leave blank to keep'), mirroring the Whisper card."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    secret = "sk-modelsecret-zzz9"
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import llm_models as llm_models_repo
            return await llm_models_repo.insert(
                app.state.db, label="Mine", provider_id="openai",
                model="openai/gpt-4o", api_key=secret, base_url="",
                make_default=True,
            )
        model_id = asyncio.get_event_loop().run_until_complete(setup())
        list_resp = client.get("/settings")
        edit_resp = client.get(f"/settings?edit={model_id}")
    assert list_resp.status_code == 200
    assert secret not in list_resp.text
    assert edit_resp.status_code == 200
    assert secret not in edit_resp.text


def test_settings_route_keeps_model_keys_out_of_template_context(
    tmp_path, monkeypatch,
):
    """Defense in depth: the plaintext api_key must not even enter the
    Jinja render context — so no future template edit can leak it. The
    route should pass sanitized view-models (has_key boolean) instead of
    the raw rows, mirroring the Whisper card's has_whisper_key pattern."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    secret = "sk-context-leak-7"

    captured: dict = {}
    from app.routes import settings as settings_route
    real_response = settings_route.templates.TemplateResponse

    def _spy(request, name, context, *a, **kw):
        captured["context"] = context
        return real_response(request, name, context, *a, **kw)

    monkeypatch.setattr(
        settings_route.templates, "TemplateResponse", _spy,
    )

    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import llm_models as llm_models_repo
            await llm_models_repo.insert(
                app.state.db, label="Mine", provider_id="openai",
                model="openai/gpt-4o", api_key=secret, base_url="",
                make_default=True,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        client.get("/settings")

    ctx = captured["context"]
    # The secret must not appear anywhere in the rendered context.
    import json as _json
    blob = _json.dumps(ctx, default=str)
    assert secret not in blob, "plaintext api_key leaked into template context"
    # And the sanitized view-models must expose a has_key flag instead.
    models = ctx["llm_models"]
    assert models, "expected the seeded model in context"
    assert all(getattr(m, "has_key", None) is True for m in models)


def test_settings_page_signals_model_key_is_set(tmp_path, monkeypatch):
    """The edit form must still tell the user a key is on file (so they
    know blank = keep), without revealing it — same has_key contract as
    the Whisper card."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import llm_models as llm_models_repo
            return await llm_models_repo.insert(
                app.state.db, label="Mine", provider_id="openai",
                model="openai/gpt-4o", api_key="sk-set", base_url="",
                make_default=True,
            )
        model_id = asyncio.get_event_loop().run_until_complete(setup())
        edit_resp = client.get(f"/settings?edit={model_id}")
    assert edit_resp.status_code == 200
    # "leave blank to keep" wording (case-insensitive) confirms the
    # write-only field is present for an existing key.
    assert "leave blank" in edit_resp.text.lower()


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
    assert "/mcp" in text
    assert "claude mcp add" in text
    # Claude Desktop config path hint is shown.
    assert "claude_desktop_config.json" in text

    # The host the user is reaching us from must be in the URLs. The
    # TestClient defaults to `testserver`.
    assert "http://testserver/api/v1/health" in text
    assert "http://testserver/mcp" in text

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
    """When the server has both chat and embedding models, the chat select
    is rendered and the summary mentions the embedding count."""
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
    # Chat select is rendered
    assert 'name="llm_model"' in text
    # No embedding select (embed_block removed)
    assert 'name="embedding_model"' not in text
    # Chat tags appear as ollama_chat/...
    assert "ollama_chat/llama3.1:latest" in text
    assert "ollama_chat/qwen2.5:14b" in text
    # Summary still mentions the embedding count
    assert "2 embedding" in text


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
    """The settings page renders without error even though the embedding
    model dropdowns have been removed (embedding config is now internal)."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    # Embedding section is gone — no embedding_model select.
    assert 'name="embedding_model"' not in resp.text


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


def test_settings_page_links_to_diagnostics(tmp_path, monkeypatch):
    """The diagnostics subpage must be reachable from /settings."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "/settings/diagnostics" in resp.text


def test_post_llm_models_inserts_row(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/llm-models",
            data={
                "label": "Claude",
                "provider_id": "anthropic",
                # Quick Setup's form names — see the alias= on the route.
                "llm_model": "anthropic/claude-sonnet-4-6",
                "api_key": "sk-test",
                "llm_base_url": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        import asyncio
        async def check():
            from app.repos import llm_models as llm_models_repo
            rows = await llm_models_repo.list_all(app.state.db)
            assert len(rows) == 1
            assert rows[0].label == "Claude"
            assert rows[0].is_default is True  # first insert auto-defaults
        asyncio.get_event_loop().run_until_complete(check())


def test_post_llm_models_id_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        mid_holder: dict = {}

        async def setup():
            from app.repos import llm_models as llm_models_repo
            mid = await llm_models_repo.insert(
                app.state.db, label="A", provider_id="openai",
                model="openai/gpt-5.5", api_key="k", base_url="",
                make_default=True,
            )
            mid_holder["id"] = mid
        asyncio.get_event_loop().run_until_complete(setup())

        resp = client.post(
            f"/settings/llm-models/{mid_holder['id']}",
            data={
                "label": "A renamed",
                "llm_model": "openai/gpt-5.4",
                "api_key": "",  # blank = keep existing
                "llm_base_url": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            from app.repos import llm_models as llm_models_repo
            row = await llm_models_repo.get(app.state.db, mid_holder["id"])
            assert row is not None
            assert row.label == "A renamed"
            assert row.model == "openai/gpt-5.4"
            # Blank api_key in form → keep existing key.
            assert row.api_key == "k"
        asyncio.get_event_loop().run_until_complete(check())


def test_post_llm_models_default_flips(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        ids: dict = {}

        async def setup():
            from app.repos import llm_models as llm_models_repo
            a = await llm_models_repo.insert(
                app.state.db, label="A", provider_id="openai",
                model="openai/gpt-5.5", api_key="k", base_url="",
                make_default=True,
            )
            b = await llm_models_repo.insert(
                app.state.db, label="B", provider_id="ollama",
                model="ollama_chat/llama3.1", api_key="", base_url="x",
                make_default=False,
            )
            ids["a"] = a
            ids["b"] = b
        asyncio.get_event_loop().run_until_complete(setup())

        resp = client.post(
            f"/settings/llm-models/{ids['b']}/default",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            from app.repos import llm_models as llm_models_repo
            new_default = await llm_models_repo.get_default(app.state.db)
            assert new_default is not None
            assert new_default.id == ids["b"]
            row_a = await llm_models_repo.get(app.state.db, ids["a"])
            assert row_a is not None
            assert row_a.is_default is False
        asyncio.get_event_loop().run_until_complete(check())


def test_post_llm_models_delete_non_default(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        ids: dict = {}

        async def setup():
            from app.repos import llm_models as llm_models_repo
            a = await llm_models_repo.insert(
                app.state.db, label="A", provider_id="openai",
                model="openai/gpt-5.5", api_key="k", base_url="",
                make_default=True,
            )
            b = await llm_models_repo.insert(
                app.state.db, label="B", provider_id="ollama",
                model="ollama_chat/llama3.1", api_key="", base_url="x",
                make_default=False,
            )
            ids["a"] = a
            ids["b"] = b
        asyncio.get_event_loop().run_until_complete(setup())

        resp = client.post(
            f"/settings/llm-models/{ids['b']}/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            from app.repos import llm_models as llm_models_repo
            assert await llm_models_repo.get(app.state.db, ids["b"]) is None
            assert await llm_models_repo.get(app.state.db, ids["a"]) is not None
        asyncio.get_event_loop().run_until_complete(check())


def test_post_llm_models_delete_default_returns_409(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        ids: dict = {}

        async def setup():
            from app.repos import llm_models as llm_models_repo
            a = await llm_models_repo.insert(
                app.state.db, label="A", provider_id="openai",
                model="openai/gpt-5.5", api_key="k", base_url="",
                make_default=True,
            )
            ids["a"] = a
        asyncio.get_event_loop().run_until_complete(setup())

        resp = client.post(
            f"/settings/llm-models/{ids['a']}/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 409


def test_post_llm_models_test_missing_row_returns_warning_fragment(
    tmp_path, monkeypatch,
):
    """The /test endpoint is HTMX-driven, so a missing row returns
    an HTML fragment (not an HTTP 404). The fragment carries the
    'status-failed' class the surrounding card styles consume."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/llm-models/9999/test", follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "Model not found" in resp.text
        assert "status-failed" in resp.text


def test_post_llm_models_test_existing_row_calls_litellm(
    tmp_path, monkeypatch,
):
    """Happy path: the /test endpoint feeds the row's
    model / api_key / base_url to litellm.acompletion and renders
    the response in a status-done fragment."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch

        ids: dict = {}

        async def setup():
            from app.repos import llm_models as llm_models_repo
            mid = await llm_models_repo.insert(
                app.state.db, label="X", provider_id="anthropic",
                model="anthropic/claude-sonnet-4-6",
                api_key="sk-test", base_url="", make_default=True,
            )
            ids["id"] = mid
        asyncio.get_event_loop().run_until_complete(setup())

        # Build a minimal response object shaped like
        # litellm.ModelResponse so the route can read
        # response.choices[0].message.content.
        fake_response = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="ok"))
            ]
        )

        async def fake_acompletion(**_kw):
            return fake_response

        with patch(
            "app.routes.settings.litellm.acompletion",
            side_effect=fake_acompletion,
        ):
            resp = client.post(
                f"/settings/llm-models/{ids['id']}/test",
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert "status-done" in resp.text
        assert "responded: ok" in resp.text


def test_settings_renders_configured_models_card_with_rows(
    tmp_path, monkeypatch,
):
    """The Configured models card lists every llm_models row and
    badges the default."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import llm_models as llm_models_repo
            await llm_models_repo.insert(
                app.state.db,
                label="My Claude", provider_id="anthropic",
                model="anthropic/claude-sonnet-4-6",
                api_key="k", base_url="", make_default=True,
            )
            await llm_models_repo.insert(
                app.state.db,
                label="Local Llama", provider_id="ollama",
                model="ollama_chat/llama3.1",
                api_key="", base_url="http://192.168.0.5:11434",
                make_default=False,
            )
        asyncio.get_event_loop().run_until_complete(setup())

        resp = client.get("/settings")
        assert resp.status_code == 200
        body = resp.text
        # Both rows render with label + model identifier.
        assert "My Claude" in body
        assert "Local Llama" in body
        assert "anthropic/claude-sonnet-4-6" in body
        assert "ollama_chat/llama3.1" in body
        # The non-default row's base_url surfaces in the meta line.
        assert "192.168.0.5" in body
        # Default badge is present for the default row.
        assert "Default ✓" in body


def test_settings_renders_empty_state_when_no_models(tmp_path, monkeypatch):
    """A fresh install (no rows) renders the empty-state copy + the
    quick-setup link, not an empty <ul>."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
        assert resp.status_code == 200
        body = resp.text
        assert "No models configured yet" in body
        assert "#quick-setup" in body


def test_pexels_api_key_saved_and_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        client.post("/settings", data={"pexels_api_key": "PKEY"},
                    follow_redirects=False)

        async def get_key():
            from app.repos import settings as settings_repo
            return await settings_repo.get(app.state.db, "pexels_api_key")
        assert asyncio.get_event_loop().run_until_complete(get_key()) == "PKEY"

        client.post("/settings", data={"pexels_api_key": ""},
                    follow_redirects=False)
        assert asyncio.get_event_loop().run_until_complete(get_key()) is None


def test_save_youtube_api_key_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        client.post("/settings", data={"youtube_api_key": "YTKEY"},
                    follow_redirects=False)

        async def get_key():
            from app.repos import settings as settings_repo
            return await settings_repo.get(app.state.db, "youtube_api_key")
        assert asyncio.get_event_loop().run_until_complete(get_key()) == "YTKEY"

        # Verify it also renders on the settings page.
        page = client.get("/settings")
        assert "YTKEY" in page.text

        # Clearing the field removes the key.
        client.post("/settings", data={"youtube_api_key": ""},
                    follow_redirects=False)
        assert asyncio.get_event_loop().run_until_complete(get_key()) is None


# ── API key test-button endpoints ──────────────────────────────

def test_test_pexels_no_key_returns_no_key_fragment(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/settings/test-pexels")
    assert resp.status_code == 200
    assert "No key" in resp.text
    assert "status-failed" in resp.text


def test_test_pexels_with_stored_key_calls_probe(tmp_path, monkeypatch):
    import asyncio

    from app.repos import settings as settings_repo
    from app.services import stock_images

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    async def fake_probe(api_key):
        return (True, "Pexels key works")
    monkeypatch.setattr(stock_images, "test_pexels_key", fake_probe)

    with TestClient(app) as client:
        asyncio.get_event_loop().run_until_complete(
            settings_repo.set(app.state.db, "pexels_api_key", "KEY")
        )
        resp = client.post("/settings/test-pexels")
    assert resp.status_code == 200
    assert "Pexels key works" in resp.text
    assert "status-done" in resp.text


def test_test_youtube_with_stored_key_calls_probe(tmp_path, monkeypatch):
    import asyncio

    from app.repos import settings as settings_repo
    from app.services import playlist_index

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    async def fake_probe(api_key):
        return (True, "YouTube key works")
    monkeypatch.setattr(playlist_index, "test_youtube_key", fake_probe)

    with TestClient(app) as client:
        asyncio.get_event_loop().run_until_complete(
            settings_repo.set(app.state.db, "youtube_api_key", "KEY")
        )
        resp = client.post("/settings/test-youtube")
    assert resp.status_code == 200
    assert "YouTube key works" in resp.text
    assert "status-done" in resp.text


def test_settings_page_shows_test_buttons(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "/settings/test-pexels" in resp.text
    assert "/settings/test-youtube" in resp.text
