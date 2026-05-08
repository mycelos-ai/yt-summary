"""Curated provider presets for the Quick Setup wizard.

The wizard lets a user pick a single LLM/Whisper/Embedding provider and
have all the relevant settings keys filled in with sensible defaults.

The defaults below are hand-picked — they're a one-line edit when a new
flagship model ships. The full per-provider model dropdown comes from
LiteLLM's static cost map, which we filter down to chat / embedding
modes and sort with the curated default first.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ProviderPreset:
    """A single quick-setup target.

    The litellm_provider field maps to LiteLLM's `litellm_provider`
    metadata so we can filter the cost-map dropdown for this provider.

    `default_llm` already includes the litellm prefix (e.g.
    `openai/gpt-4o`) — that's what gets written to settings, so the
    rest of the app passes it to litellm.acompletion as-is.
    """

    id: str
    name: str
    litellm_provider: str
    default_llm: str
    requires_api_key: bool = True
    api_key_url: str = ""
    default_llm_base_url: str = ""
    default_embedding: str | None = None  # litellm-prefixed; None if none
    whisper_base_url: str = ""
    whisper_model: str = ""
    notes: str = ""  # Free-form note shown in the wizard UI


# Curated default flagships. One-line edit when a new model ships —
# the wizard will still expose the full LiteLLM list so users can
# pick something else without waiting for us to bump these.
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        id="openai",
        name="OpenAI",
        litellm_provider="openai",
        default_llm="openai/gpt-4o",
        api_key_url="https://platform.openai.com/api-keys",
        default_embedding="openai/text-embedding-3-small",
        whisper_base_url="https://api.openai.com/v1",
        whisper_model="whisper-1",
    ),
    "anthropic": ProviderPreset(
        id="anthropic",
        name="Anthropic",
        litellm_provider="anthropic",
        default_llm="anthropic/claude-sonnet-4-6",
        api_key_url="https://console.anthropic.com/settings/keys",
        # Anthropic has no embedding API.
        default_embedding=None,
        notes=(
            "Anthropic has no embedding or speech-to-text API. "
            "Configure those in the Embedding and Whisper cards "
            "separately if you need them."
        ),
    ),
    "gemini": ProviderPreset(
        id="gemini",
        name="Google Gemini",
        litellm_provider="gemini",
        default_llm="gemini/gemini-2.5-flash",
        api_key_url="https://aistudio.google.com/apikey",
        default_embedding="gemini/text-embedding-004",
    ),
    "groq": ProviderPreset(
        id="groq",
        name="Groq",
        litellm_provider="groq",
        default_llm="groq/llama-3.3-70b-versatile",
        api_key_url="https://console.groq.com/keys",
        # Groq has no first-party embedding model.
        default_embedding=None,
        # Groq's Whisper-as-a-service is the fastest commercial option.
        whisper_base_url="https://api.groq.com/openai/v1",
        whisper_model="whisper-large-v3",
        notes=(
            "Groq has no embedding API. The summary LLM and Whisper "
            "are both extremely fast through Groq."
        ),
    ),
    "ollama": ProviderPreset(
        id="ollama",
        name="Ollama (local)",
        litellm_provider="ollama",
        default_llm="ollama_chat/llama3.1",
        requires_api_key=False,
        default_llm_base_url="http://host.docker.internal:11434",
        default_embedding="ollama/nomic-embed-text",
        notes=(
            "Local install — no API key required. The default base URL "
            "talks to an Ollama server on the Docker host. Change it "
            "to a LAN IP if Ollama runs on a different machine."
        ),
    ),
    "openrouter": ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        litellm_provider="openrouter",
        default_llm="openrouter/anthropic/claude-sonnet-4",
        default_llm_base_url="https://openrouter.ai/api/v1",
        api_key_url="https://openrouter.ai/keys",
        # OpenRouter doesn't expose embeddings.
        default_embedding=None,
        notes=(
            "Routes to many backends through one API. Pick any model "
            "from openrouter.ai/models — they all start with the "
            "openrouter/ prefix."
        ),
    ),
}


def get_preset(provider_id: str) -> ProviderPreset:
    """Return the preset for `provider_id`. Raises KeyError on unknown."""
    return PROVIDER_PRESETS[provider_id]


def apply_preset(
    *,
    provider_id: str,
    api_key: str,
    current_settings: dict[str, str],
    llm_model_override: str | None = None,
    llm_base_url_override: str | None = None,
    embedding_model_override: str | None = None,
) -> dict[str, str]:
    """Compute the new settings dict after applying a preset.

    Returns ONLY the keys the caller should write — preserves any
    setting the preset doesn't touch (e.g. embedding for Anthropic).

    `api_key` blank → don't surface llm_api_key/whisper_api_key in the
    output, so the caller knows to leave the existing key alone.
    """
    preset = get_preset(provider_id)
    out: dict[str, str] = {}

    # ── LLM ──
    out["llm_model"] = (llm_model_override or preset.default_llm).strip()
    # Base URL: explicit user input wins, then preset default, then clear.
    chosen_base_url = (
        llm_base_url_override.strip() if llm_base_url_override else None
    ) or preset.default_llm_base_url
    if chosen_base_url:
        out["llm_base_url"] = chosen_base_url.rstrip("/")
    elif "llm_base_url" in current_settings:
        # Switching to a hosted provider — clear any old self-hosted
        # base URL that would point at the wrong server. Cloud providers
        # default to "" which means "use litellm's known endpoint".
        out["llm_base_url"] = ""

    # ── Embedding (only if this provider has one) ──
    if preset.default_embedding:
        out["embedding_model"] = (
            embedding_model_override or preset.default_embedding
        ).strip()
        # Embedding endpoint reuses LLM base URL by default; clear any
        # stale embedding_base_url so litellm uses the cloud endpoint.
        if preset.id != "ollama":
            out["embedding_base_url"] = ""
        else:
            # Ollama: point embedding at the same server.
            out["embedding_base_url"] = chosen_base_url or ""

    # ── Whisper (only if provider hosts a Whisper-compatible endpoint) ──
    if preset.whisper_base_url:
        out["whisper_base_url"] = preset.whisper_base_url
        out["whisper_model"] = preset.whisper_model

    # ── API keys ──
    if api_key:
        # Same key reused for whisper if this provider hosts it.
        out["llm_api_key"] = api_key
        if preset.whisper_base_url:
            out["whisper_api_key"] = api_key
    # If api_key is blank we deliberately omit the *_api_key keys so
    # the caller can keep whatever's already in the database.

    return out


def list_chat_models(provider_id: str) -> list[str]:
    """Return chat-capable model ids from the LiteLLM cost map for the
    given provider, with the preset's default model first.

    Excludes fine-tune entries and non-chat modes. Used to populate the
    wizard's model dropdown.
    """
    try:
        preset = get_preset(provider_id)
    except KeyError:
        return []

    # litellm imports are slow; do it lazily.
    import litellm

    target = preset.litellm_provider
    raw = [
        m for m, info in litellm.model_cost.items()
        if info.get("litellm_provider") == target
        and info.get("mode") == "chat"
        and not m.startswith("ft:")
    ]
    return _sort_with_default_first(raw, preset.default_llm)


def list_embedding_models(provider_id: str) -> list[str]:
    """Return embedding-capable model ids for the given provider, with
    the preset's default first."""
    try:
        preset = get_preset(provider_id)
    except KeyError:
        return []
    if not preset.default_embedding:
        return []

    import litellm

    target = preset.litellm_provider
    raw = [
        m for m, info in litellm.model_cost.items()
        if info.get("litellm_provider") == target
        and info.get("mode") == "embedding"
        and not m.startswith("ft:")
    ]
    return _sort_with_default_first(raw, preset.default_embedding)


def _sort_with_default_first(models: Iterable[str], default: str) -> list[str]:
    """Sort the model list reverse-alphabetically (newer versions
    usually have higher numbers) and float the default to the top
    if it's present."""
    sorted_models = sorted(models, reverse=True)

    # Match either with or without provider prefix.
    candidates = {default, default.split("/", 1)[-1]}
    for cand in candidates:
        if cand in sorted_models:
            sorted_models.remove(cand)
            return [cand, *sorted_models]
    # Default missing from LiteLLM — keep it as first option anyway,
    # since the user might be on a fresher model than the cost map.
    return [default, *sorted_models]


async def fetch_ollama_models(base_url: str) -> list[str]:
    """Hit /api/tags on an Ollama server and return its model name list.

    Used by the Quick Setup wizard so users can pick from real models
    on their server instead of typing tags from memory.

    Raises httpx.HTTPError on non-2xx or unreachable.
    """
    url = f"{base_url.rstrip('/')}/api/tags"
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        body = resp.json()
    return [m.get("name", "") for m in body.get("models", []) if m.get("name")]
