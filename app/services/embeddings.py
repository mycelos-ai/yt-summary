"""Compatibility shim — delegates to embeddings_local.

The legacy ``model`` / ``api_key`` / ``base_url`` parameters are
accepted but ignored. They will be removed in a follow-up cleanup
once all callers (pipeline.py, home.py, routes/settings.py) stop
passing them.
"""
from __future__ import annotations


async def embed_text(
    text: str,
    *,
    model: str | None = None,    # noqa: ARG001 — kept for back-compat
    api_key: str = "",            # noqa: ARG001
    base_url: str | None = None,  # noqa: ARG001
) -> list[float]:
    """Return the embedding vector for `text`.

    All positional/keyword args except `text` are ignored — the local
    `paraphrase-multilingual-MiniLM-L12-v2` model is the only embedder.
    """
    from app.services import embeddings_local
    return await embeddings_local.embed_text(text)
