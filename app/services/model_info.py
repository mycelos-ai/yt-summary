"""Detect a model's effective context window.

LiteLLM's get_max_tokens only knows the models in its built-in catalogue,
which excludes arbitrary local Ollama tags. For ollama / ollama_chat models
we ask the Ollama server itself via /api/show and parse the architecture-
specific context_length field. Other providers fall back to LiteLLM's
catalogue, then to a conservative 8k default.
"""

from __future__ import annotations

import logging

import httpx
import litellm

log = logging.getLogger(__name__)

DEFAULT_CONTEXT = 8000

# Per-process cache: (model, base_url) -> tokens. Cleared on restart, which
# is fine — the Ollama server might pull a new model variant between runs.
_CACHE: dict[tuple[str, str | None], int] = {}


def _strip_provider(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _extract_context_length(payload: dict) -> int | None:
    """Walk model_info for any *.context_length key.

    Ollama returns architecture-prefixed keys, e.g.::

        {"model_info": {"gemma3.context_length": 131072, ...}}

    or sometimes a top-level "context_length". We accept any key ending in
    ".context_length" and the bare key.
    """
    info = payload.get("model_info") or {}
    if not isinstance(info, dict):
        return None
    if isinstance(info.get("context_length"), int):
        return info["context_length"]
    for key, value in info.items():
        if key.endswith(".context_length") and isinstance(value, int):
            return value
    # Older Ollama versions: "parameters" string with "num_ctx <n>".
    params = payload.get("parameters")
    if isinstance(params, str):
        for line in params.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "num_ctx" and parts[1].isdigit():
                return int(parts[1])
    return None


async def _query_ollama(base_url: str, tag: str) -> int | None:
    url = f"{base_url.rstrip('/')}/api/show"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(url, json={"name": tag})
            r.raise_for_status()
            return _extract_context_length(r.json())
    except Exception as e:
        log.warning("Ollama /api/show for %r failed: %s: %s", tag, type(e).__name__, e)
        return None


def _from_litellm(model: str) -> int | None:
    try:
        value = litellm.get_max_tokens(model)
        if isinstance(value, int) and value > 0:
            return value
    except Exception:
        pass
    return None


async def get_context_window(model: str, base_url: str | None) -> int:
    """Return the model's context window in tokens.

    Order of resolution:
    1. Cached value from a previous lookup in this process.
    2. For ollama / ollama_chat: Ollama's /api/show output.
    3. LiteLLM's built-in catalogue.
    4. DEFAULT_CONTEXT (8000).
    """
    cache_key = (model, base_url)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    if model.startswith(("ollama/", "ollama_chat/")) and base_url:
        tag = _strip_provider(model)
        ollama_value = await _query_ollama(base_url, tag)
        if ollama_value:
            log.info("Context for %s via Ollama: %d tokens", model, ollama_value)
            _CACHE[cache_key] = ollama_value
            return ollama_value

    catalogue_value = _from_litellm(model)
    if catalogue_value:
        log.info("Context for %s via LiteLLM catalogue: %d tokens", model, catalogue_value)
        _CACHE[cache_key] = catalogue_value
        return catalogue_value

    log.info("Context for %s unknown — defaulting to %d", model, DEFAULT_CONTEXT)
    _CACHE[cache_key] = DEFAULT_CONTEXT
    return DEFAULT_CONTEXT


def clear_cache() -> None:
    """Test helper."""
    _CACHE.clear()
