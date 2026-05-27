from unittest.mock import AsyncMock, patch

import pytest

from app.services import model_info


@pytest.fixture(autouse=True)
def _clear_cache():
    model_info.clear_cache()
    yield
    model_info.clear_cache()


def _ollama_show_response(*, context_length: int, family: str = "gemma3") -> dict:
    return {
        "model_info": {
            f"{family}.context_length": context_length,
            f"{family}.attention.head_count": 32,
            "general.architecture": family,
        },
        "parameters": "stop \"<|im_end|>\"\nnum_ctx 4096",
    }


async def test_ollama_path_extracts_architecture_prefixed_context_length():
    payload = _ollama_show_response(context_length=131072, family="gemma3")
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: payload

    async def fake_post(self, url, json):
        return response

    with patch("httpx.AsyncClient.post", new=fake_post):
        ctx = await model_info.get_context_window(
            "ollama_chat/gemma3:latest", "http://192.168.0.27:11434"
        )
    assert ctx == 131072


async def test_ollama_path_falls_through_to_litellm_when_show_fails():
    async def boom(self, url, json):
        raise RuntimeError("offline")

    with (
        patch("httpx.AsyncClient.post", new=boom),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=128_000),
    ):
        ctx = await model_info.get_context_window(
            "ollama_chat/gemma3:latest", "http://x"
        )
    assert ctx == 128_000


async def test_default_when_neither_ollama_nor_litellm_knows():
    with (
        patch("app.services.model_info.litellm.get_max_tokens", return_value=None),
    ):
        ctx = await model_info.get_context_window("openai/something-rare", None)
    assert ctx == model_info.DEFAULT_CONTEXT


async def test_non_ollama_skips_query_and_uses_catalogue():
    with (
        patch("httpx.AsyncClient.post") as post_mock,
        patch("app.services.model_info.litellm.get_max_tokens", return_value=200_000),
    ):
        ctx = await model_info.get_context_window("openai/gpt-4o", None)
    assert ctx == 200_000
    post_mock.assert_not_called()


async def test_caching_prevents_repeat_lookup():
    payload = _ollama_show_response(context_length=8192)
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: payload

    call_count = 0

    async def counting_post(self, url, json):
        nonlocal call_count
        call_count += 1
        return response

    with patch("httpx.AsyncClient.post", new=counting_post):
        await model_info.get_context_window("ollama_chat/x:1", "http://y")
        await model_info.get_context_window("ollama_chat/x:1", "http://y")
    assert call_count == 1


async def test_openrouter_path_uses_live_catalogue():
    """OpenRouter models resolve via /api/v1/models, bypassing LiteLLM."""
    catalogue_response = AsyncMock()
    catalogue_response.raise_for_status = lambda: None
    catalogue_response.json = lambda: {
        "data": [
            {"id": "deepseek/deepseek-v4-pro", "context_length": 1048576},
            {"id": "anthropic/claude-opus-4.7", "context_length": 1000000},
        ]
    }

    async def fake_get(self, url, *args, **kwargs):
        assert "openrouter.ai/api/v1/models" in url
        return catalogue_response

    # LiteLLM mocked to None so we *prove* the OpenRouter path is what
    # produced the answer — not a stale LiteLLM cache.
    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=None),
    ):
        ctx = await model_info.get_context_window(
            "openrouter/deepseek/deepseek-v4-pro", None
        )
    assert ctx == 1048576


async def test_openrouter_catalogue_cached_across_lookups():
    """One HTTP call per process — second lookup hits the in-memory cache."""
    payload = {
        "data": [
            {"id": "deepseek/deepseek-v4-pro", "context_length": 1048576},
            {"id": "anthropic/claude-opus-4.7", "context_length": 1000000},
        ]
    }
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: payload

    call_count = 0

    async def counting_get(self, url, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return response

    with (
        patch("httpx.AsyncClient.get", new=counting_get),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=None),
    ):
        a = await model_info.get_context_window(
            "openrouter/deepseek/deepseek-v4-pro", None
        )
        b = await model_info.get_context_window(
            "openrouter/anthropic/claude-opus-4.7", None
        )
    assert a == 1048576
    assert b == 1000000
    # Two different slugs but only one catalogue fetch.
    assert call_count == 1


async def test_openrouter_unknown_slug_falls_through_to_litellm():
    """A slug not in OpenRouter's response shouldn't shortcut LiteLLM."""
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: {"data": []}

    async def fake_get(self, url, *args, **kwargs):
        return response

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=200_000),
    ):
        ctx = await model_info.get_context_window(
            "openrouter/some/unknown-model", None
        )
    assert ctx == 200_000


async def test_openrouter_fetch_failure_falls_through():
    """A failed catalogue fetch must not crash — caller gets LiteLLM/default."""
    async def boom(self, url, *args, **kwargs):
        raise RuntimeError("no network")

    with (
        patch("httpx.AsyncClient.get", new=boom),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=128_000),
    ):
        ctx = await model_info.get_context_window(
            "openrouter/deepseek/deepseek-v4-pro", None
        )
    assert ctx == 128_000


async def test_non_openrouter_model_does_not_trigger_catalogue_fetch():
    """Direct anthropic/openai models must not hit OpenRouter's API."""
    with (
        patch("httpx.AsyncClient.get") as get_mock,
        patch("app.services.model_info.litellm.get_max_tokens", return_value=200_000),
    ):
        ctx = await model_info.get_context_window("anthropic/claude-sonnet-4-6", None)
    assert ctx == 200_000
    get_mock.assert_not_called()


async def test_old_ollama_parses_num_ctx_from_parameters_string():
    payload = {
        "model_info": {},  # nothing useful
        "parameters": "stop \"<|stop|>\"\nnum_ctx 16384\ntemperature 0.8",
    }
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: payload

    async def fake_post(self, url, json):
        return response

    with patch("httpx.AsyncClient.post", new=fake_post):
        ctx = await model_info.get_context_window(
            "ollama_chat/old-model", "http://x"
        )
    assert ctx == 16384
