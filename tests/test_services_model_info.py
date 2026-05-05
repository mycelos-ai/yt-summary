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
