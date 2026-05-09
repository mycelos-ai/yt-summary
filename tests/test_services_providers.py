"""Tests for the provider-preset registry that drives Quick Setup."""

import pytest

from app.services.providers import (
    PROVIDER_PRESETS,
    ProviderPreset,
    apply_preset,
    get_preset,
    list_chat_models,
)


def test_preset_registry_has_six_majors():
    expected = {"openai", "anthropic", "gemini", "groq", "ollama", "openrouter"}
    assert set(PROVIDER_PRESETS.keys()) == expected


def test_get_preset_known_provider():
    p = get_preset("anthropic")
    assert isinstance(p, ProviderPreset)
    assert p.name == "Anthropic"
    assert p.default_llm  # has a default
    assert p.default_embedding is None  # but no embedding


def test_get_preset_unknown_raises():
    with pytest.raises(KeyError):
        get_preset("nonsense-provider")


def test_openai_supports_everything():
    p = get_preset("openai")
    assert p.default_llm
    assert p.default_embedding
    assert p.whisper_base_url
    assert p.whisper_model


def test_groq_supports_llm_and_whisper_but_no_embedding():
    """Groq is fast for both LLM and Whisper, but has no first-party
    embeddings model — leave embedding alone in apply()."""
    p = get_preset("groq")
    assert p.default_llm
    assert p.whisper_base_url
    assert p.whisper_model
    assert p.default_embedding is None


def test_anthropic_is_llm_only():
    p = get_preset("anthropic")
    assert p.default_llm
    assert p.default_embedding is None
    assert not p.whisper_base_url
    assert not p.whisper_model


def test_ollama_has_no_api_key_url():
    """Ollama is local-only; no console URL to point users at for keys."""
    p = get_preset("ollama")
    assert p.api_key_url == ""
    assert p.requires_api_key is False


def test_apply_preset_writes_only_llm_for_anthropic():
    """Anthropic preset must NOT clobber existing embedding settings,
    because Anthropic has no embedding API. apply_preset returns only
    keys to write — fields it omits stay untouched in the DB."""
    state = {
        "embedding_model": "ollama/nomic-embed-text",
        "embedding_base_url": "http://192.168.0.27:11434",
    }
    out = apply_preset(
        provider_id="anthropic",
        api_key="sk-ant-x",
        current_settings=state,
    )
    # LLM fields written
    assert out["llm_model"] == "anthropic/claude-sonnet-4-6"
    assert out["llm_api_key"] == "sk-ant-x"
    # Embedding NOT in the output dict — caller leaves DB row alone
    assert "embedding_model" not in out
    assert "embedding_base_url" not in out
    # Same for whisper
    assert "whisper_base_url" not in out
    assert "whisper_model" not in out


def test_apply_preset_groq_sets_whisper_too():
    state = {}
    out = apply_preset(
        provider_id="groq", api_key="gsk-x", current_settings=state
    )
    assert out["llm_model"].startswith("groq/")
    assert out["llm_api_key"] == "gsk-x"
    assert out["whisper_base_url"] == "https://api.groq.com/openai/v1"
    assert out["whisper_model"]
    assert out["whisper_api_key"] == "gsk-x"


def test_apply_preset_openai_sets_everything():
    out = apply_preset(
        provider_id="openai", api_key="sk-x", current_settings={}
    )
    assert out["llm_model"].startswith("openai/")
    assert out["llm_api_key"] == "sk-x"
    assert "embedding_model" in out
    assert out["whisper_base_url"] == "https://api.openai.com/v1"
    assert out["whisper_model"] == "whisper-1"
    assert out["whisper_api_key"] == "sk-x"


def test_apply_preset_blank_api_key_keeps_existing():
    """User left the API-key input blank → keep whatever's already in
    settings (consistent with the regular Save form's behaviour)."""
    state = {"llm_api_key": "existing-key"}
    out = apply_preset(
        provider_id="openai", api_key="", current_settings=state
    )
    # llm_api_key not in the output → caller knows not to write it
    assert "llm_api_key" not in out or out.get("llm_api_key") == ""


def test_apply_preset_ollama_no_api_key_needed():
    """Ollama preset must work even with empty key — local install."""
    out = apply_preset(
        provider_id="ollama", api_key="", current_settings={}
    )
    assert out["llm_model"].startswith("ollama")
    assert out["llm_base_url"]  # default base url is set


def test_apply_preset_overrides_existing_llm():
    """Switching providers should overwrite llm_model — that's the
    whole point of 'apply preset'."""
    state = {"llm_model": "openai/gpt-4o"}
    out = apply_preset(
        provider_id="anthropic", api_key="x", current_settings=state
    )
    assert out["llm_model"] == "anthropic/claude-sonnet-4-6"


def test_apply_preset_with_custom_llm_model():
    """If the user picked a non-default model in the wizard dropdown,
    honour it instead of the hardcoded default."""
    out = apply_preset(
        provider_id="openai", api_key="sk", current_settings={},
        llm_model_override="openai/gpt-4o-mini",
    )
    assert out["llm_model"] == "openai/gpt-4o-mini"


def test_list_chat_models_filters_provider_and_mode():
    """Should return chat-capable models for a provider, sorted in some
    stable order, with non-chat / fine-tune entries removed."""
    models = list_chat_models("anthropic")
    # All entries match the provider
    assert all("claude" in m or "anthropic" in m for m in models)
    # No fine-tune templates
    assert not any(m.startswith("ft:") for m in models)
    # Non-empty
    assert len(models) > 0


def test_list_chat_models_unknown_provider_returns_empty():
    assert list_chat_models("nonsense") == []


def test_list_chat_models_default_first():
    """The provider's default model should appear first so it's
    pre-selected in the dropdown."""
    models = list_chat_models("openai")
    preset = get_preset("openai")
    # Default is the LLM stripped of provider prefix
    default_bare = preset.default_llm.removeprefix("openai/")
    assert models[0] in (default_bare, preset.default_llm)


def test_list_chat_models_curated_short_by_default():
    """Default behavior returns the curated short list — not hundreds
    of legacy entries from the LiteLLM cost map."""
    models = list_chat_models("openai")
    # Curated lists are intentionally small (under 10).
    assert len(models) < 10
    # All curated OpenAI entries are 5.x — no legacy 4o, no o1/o3 leak.
    assert all("gpt-5" in m for m in models)


def test_list_chat_models_include_legacy_returns_more():
    """include_legacy=True falls back to the full LiteLLM map for
    power users."""
    short = list_chat_models("openai")
    full = list_chat_models("openai", include_legacy=True)
    assert len(full) > len(short)
    # The curated entries still come first in the full list.
    preset = get_preset("openai")
    assert full[0] in (
        preset.default_llm,
        preset.default_llm.removeprefix("openai/"),
    )


def test_list_chat_models_curated_excludes_kimi_for_groq():
    """Kimi K2 was delisted by Groq — it must not appear in the
    curated Groq list (or it'd be a dead default option)."""
    models = list_chat_models("groq")
    assert not any("kimi" in m.lower() for m in models)
    # Llama 4 Maverick is the new default
    assert any("llama-4-maverick" in m for m in models)


def test_apply_preset_ollama_with_custom_base_url():
    """Ollama wizard lets the user override the base URL (Docker
    default is rarely right). Custom base URL should win."""
    out = apply_preset(
        provider_id="ollama",
        api_key="",
        current_settings={},
        llm_base_url_override="http://192.168.0.27:11434",
        llm_model_override="ollama_chat/qwen2.5:14b",
    )
    assert out["llm_base_url"] == "http://192.168.0.27:11434"
    assert out["llm_model"] == "ollama_chat/qwen2.5:14b"
    # Embedding base URL also points at the same Ollama server
    assert out["embedding_base_url"] == "http://192.168.0.27:11434"


async def test_fetch_ollama_models_returns_model_tags():
    """fetch_ollama_models should call /api/tags on the given base URL
    and return the model name strings."""
    import respx
    from httpx import Response

    from app.services.providers import fetch_ollama_models

    sample = {
        "models": [
            {"name": "llama3.1:latest", "size": 4661211808},
            {"name": "qwen2.5:14b", "size": 8988111360},
            {"name": "nomic-embed-text:latest", "size": 274302450},
        ]
    }
    with respx.mock(base_url="http://192.168.0.27:11434") as mock:
        mock.get("/api/tags").mock(return_value=Response(200, json=sample))
        models = await fetch_ollama_models("http://192.168.0.27:11434")
    assert "llama3.1:latest" in models
    assert "qwen2.5:14b" in models
    assert "nomic-embed-text:latest" in models


async def test_fetch_ollama_models_strips_trailing_slash():
    import respx
    from httpx import Response

    from app.services.providers import fetch_ollama_models

    with respx.mock(base_url="http://x:11434") as mock:
        route = mock.get("/api/tags").mock(
            return_value=Response(200, json={"models": []})
        )
        await fetch_ollama_models("http://x:11434/")
    assert route.called


async def test_fetch_ollama_models_raises_on_unreachable():
    """If the server doesn't answer, raise so caller can surface a
    clear UI error instead of pretending the server has zero models."""
    import httpx
    import pytest as _pytest
    import respx
    from httpx import Response

    from app.services.providers import fetch_ollama_models

    with (
        respx.mock(base_url="http://nope:11434") as mock,
        _pytest.raises(httpx.HTTPError),
    ):
        mock.get("/api/tags").mock(
            return_value=Response(503, json={"error": "down"})
        )
        await fetch_ollama_models("http://nope:11434")


def test_split_ollama_tags_separates_embeddings():
    """Tags whose name contains 'embed' are classified as embedders;
    everything else is treated as chat."""
    from app.services.providers import split_ollama_tags

    tags = [
        "llama3.1:latest",
        "nomic-embed-text:latest",
        "qwen2.5:14b",
        "mxbai-embed-large:latest",
        "snowflake-arctic-embed:33m",
        "gemma3:1b",
    ]
    chat, embed = split_ollama_tags(tags)
    assert "llama3.1:latest" in chat
    assert "qwen2.5:14b" in chat
    assert "gemma3:1b" in chat
    assert "nomic-embed-text:latest" in embed
    assert "mxbai-embed-large:latest" in embed
    assert "snowflake-arctic-embed:33m" in embed
    # No overlap
    assert set(chat) & set(embed) == set()


def test_split_ollama_tags_handles_empty():
    from app.services.providers import split_ollama_tags

    chat, embed = split_ollama_tags([])
    assert chat == []
    assert embed == []


def test_split_ollama_tags_no_embedders_returns_all_chat():
    """If no tag looks like an embedder, all go to chat (and the
    UI will hide the empty embedding dropdown)."""
    from app.services.providers import split_ollama_tags

    chat, embed = split_ollama_tags(["llama3.1", "qwen3:8b"])
    assert set(chat) == {"llama3.1", "qwen3:8b"}
    assert embed == []
