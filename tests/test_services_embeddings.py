from unittest.mock import AsyncMock, patch

import pytest


def _embedding_response(vector: list[float]) -> dict:
    return {"data": [{"embedding": vector}], "model": "x", "usage": {}}


async def test_embed_text_returns_list_of_floats():
    from app.services.embeddings import embed_text
    with patch(
        "app.services.embeddings.litellm.aembedding",
        AsyncMock(return_value=_embedding_response([0.1, 0.2, 0.3])),
    ):
        v = await embed_text("hello")
    assert v == [0.1, 0.2, 0.3]


async def test_embed_text_passes_base_url_when_set():
    from app.services.embeddings import embed_text
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _embedding_response([0.0])

    with patch("app.services.embeddings.litellm.aembedding", side_effect=fake):
        await embed_text(
            "hi",
            model="ollama/nomic-embed-text",
            api_key="",
            base_url="http://192.168.0.27:11434",
        )
    assert captured["api_base"] == "http://192.168.0.27:11434"
    assert captured["model"] == "ollama/nomic-embed-text"


async def test_embed_text_prepends_ollama_provider_for_bare_model():
    from app.services.embeddings import embed_text
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _embedding_response([0.0])

    with patch("app.services.embeddings.litellm.aembedding", side_effect=fake):
        await embed_text("hi", model="nomic-embed-text")
    assert captured["model"] == "ollama/nomic-embed-text"


async def test_embed_text_rejects_empty():
    from app.services.embeddings import embed_text
    with pytest.raises(ValueError):
        await embed_text("   ")


async def test_embed_text_propagates_provider_errors():
    from app.services.embeddings import embed_text
    with patch(
        "app.services.embeddings.litellm.aembedding",
        AsyncMock(side_effect=RuntimeError("offline")),
    ), pytest.raises(RuntimeError):
        await embed_text("hi")


async def test_embed_text_raises_when_response_has_no_data():
    from app.services.embeddings import embed_text
    with (
        patch(
            "app.services.embeddings.litellm.aembedding",
            AsyncMock(return_value={"data": [], "model": "x"}),
        ),
        pytest.raises(ValueError, match="no vectors"),
    ):
        await embed_text("hi")
