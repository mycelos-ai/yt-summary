"""Tests for the embeddings.py compatibility shim.

The shim accepts the legacy `model` / `api_key` / `base_url` kwargs
but ignores them — the real work happens in embeddings_local.
"""
from unittest.mock import AsyncMock, patch

import pytest


async def test_shim_delegates_to_local():
    """The shim must call embeddings_local.embed_text with the text only."""
    from app.services import embeddings as shim
    with patch(
        "app.services.embeddings_local.embed_text",
        AsyncMock(return_value=[0.1] * 384),
    ) as m:
        v = await shim.embed_text("hello")
    assert v == [0.1] * 384
    m.assert_awaited_once_with("hello")


async def test_shim_ignores_legacy_kwargs():
    """Old callers that pass model/api_key/base_url must not break."""
    from app.services import embeddings as shim
    with patch(
        "app.services.embeddings_local.embed_text",
        AsyncMock(return_value=[0.0] * 384),
    ) as m:
        await shim.embed_text(
            "hi",
            model="ollama/nomic-embed-text",
            api_key="secret",
            base_url="http://example",
        )
    # The shim drops every kwarg before delegating.
    m.assert_awaited_once_with("hi")


async def test_shim_propagates_value_error_for_empty():
    from app.services import embeddings as shim
    with pytest.raises(ValueError):
        await shim.embed_text("")
