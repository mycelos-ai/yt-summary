"""Detect a model's effective context window.

LiteLLM's get_max_tokens only knows the models in its built-in catalogue,
which lags weeks-to-months behind upstream releases and excludes arbitrary
local Ollama tags. We use a layered strategy:

  - Ollama tags → ask the Ollama server via /api/show.
  - OpenRouter slugs → ask OpenRouter's /api/v1/models (free, no key needed,
    always current). One snapshot is fetched per process and cached.
  - Everything else → LiteLLM's static catalogue.
  - Unknown → conservative 8k default.

Without the OpenRouter lookup, brand-new slugs like deepseek-v4-pro
(real context 1M) fall through to the 8k default and the summarizer
chunks transcripts unnecessarily.
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

# OpenRouter catalogue snapshot: slug-without-prefix -> context_length.
# None = not yet fetched; {} = fetched but empty (treated as "no data").
# Populated lazily on first lookup, kept for the lifetime of the process.
_OPENROUTER_CATALOGUE: dict[str, int] | None = None
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


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


async def _fetch_openrouter_catalogue() -> dict[str, int]:
    """One-shot fetch of OpenRouter's full model catalogue.

    Returns a slug -> context_length map. On any failure returns {} so we
    don't keep retrying for the rest of the process. ~355 models, ~250KB
    JSON as of mid-2026 — small enough to keep in memory.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(_OPENROUTER_MODELS_URL)
            r.raise_for_status()
            body = r.json()
    except Exception as e:
        log.warning(
            "OpenRouter /api/v1/models fetch failed: %s: %s",
            type(e).__name__, e,
        )
        return {}

    out: dict[str, int] = {}
    for m in body.get("data") or []:
        slug = m.get("id")
        ctx = m.get("context_length")
        if isinstance(slug, str) and isinstance(ctx, int) and ctx > 0:
            out[slug] = ctx
    log.info("Fetched OpenRouter catalogue: %d models", len(out))
    return out


async def _from_openrouter(model: str) -> int | None:
    """Resolve an `openrouter/<provider>/<slug>` model via OpenRouter's API.

    The catalogue is fetched lazily on first call and cached for the
    lifetime of the process. Returns None for non-openrouter models or
    when the slug isn't in OpenRouter's catalogue.
    """
    if not model.startswith("openrouter/"):
        return None

    global _OPENROUTER_CATALOGUE
    if _OPENROUTER_CATALOGUE is None:
        _OPENROUTER_CATALOGUE = await _fetch_openrouter_catalogue()
    if not _OPENROUTER_CATALOGUE:
        return None

    # `openrouter/deepseek/deepseek-v4-pro` → `deepseek/deepseek-v4-pro`,
    # which is the form OpenRouter uses as its `id`.
    slug = _strip_provider(model)
    ctx = _OPENROUTER_CATALOGUE.get(slug)
    return ctx if isinstance(ctx, int) and ctx > 0 else None


async def get_context_window(model: str, base_url: str | None) -> int:
    """Return the model's context window in tokens.

    Order of resolution:
    1. Cached value from a previous lookup in this process.
    2. For ollama / ollama_chat: Ollama's /api/show output.
    3. For openrouter/*: OpenRouter's /api/v1/models catalogue.
    4. LiteLLM's built-in catalogue.
    5. DEFAULT_CONTEXT (8000).
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

    openrouter_value = await _from_openrouter(model)
    if openrouter_value:
        log.info(
            "Context for %s via OpenRouter catalogue: %d tokens",
            model, openrouter_value,
        )
        _CACHE[cache_key] = openrouter_value
        return openrouter_value

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
    global _OPENROUTER_CATALOGUE
    _CACHE.clear()
    _OPENROUTER_CATALOGUE = None
