"""Embedding generation via LiteLLM.

LiteLLM exposes `aembedding` for any provider it supports. For Ollama
we go through `ollama/<model>` (the OpenAI-compatible flavor accepts
embeddings too via /v1/embeddings) and pass the configured base_url.
"""

from typing import Any

import litellm

DEFAULT_EMBEDDING_MODEL = "ollama/nomic-embed-text"


async def embed_text(
    text: str,
    *,
    model: str | None = None,
    api_key: str = "",
    base_url: str | None = None,
) -> list[float]:
    """Return the embedding vector for `text`.

    `model` defaults to `nomic-embed-text` via Ollama. Empty text raises
    ValueError. Network/provider errors propagate.
    """
    text = text.strip()
    if not text:
        raise ValueError("Cannot embed empty text")

    chosen_model = (model or DEFAULT_EMBEDDING_MODEL).strip()
    # If the user typed the bare ollama model tag, prepend the provider.
    if "/" not in chosen_model:
        chosen_model = f"ollama/{chosen_model}"

    kwargs: dict[str, Any] = {
        "model": chosen_model,
        "input": text,
        "api_key": api_key or "",
    }
    if base_url:
        kwargs["api_base"] = base_url

    response = await litellm.aembedding(**kwargs)
    # LiteLLM normalises to OpenAI's response shape:
    # {"data": [{"embedding": [...]}], ...}
    data = response["data"]
    if not data:
        raise ValueError("Embedding provider returned no vectors")
    vector = data[0]["embedding"]
    if not vector:
        raise ValueError("Embedding provider returned an empty vector")
    return list(vector)
