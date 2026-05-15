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
        default_llm="openai/gpt-5.5",
        api_key_url="https://platform.openai.com/api-keys",
        whisper_base_url="https://api.openai.com/v1",
        whisper_model="whisper-1",
    ),
    "anthropic": ProviderPreset(
        id="anthropic",
        name="Anthropic",
        litellm_provider="anthropic",
        default_llm="anthropic/claude-sonnet-4-6",
        api_key_url="https://console.anthropic.com/settings/keys",
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
        default_llm="gemini/gemini-3.1-flash-lite-preview",
        api_key_url="https://aistudio.google.com/apikey",
    ),
    "groq": ProviderPreset(
        id="groq",
        name="Groq",
        litellm_provider="groq",
        # Groq churns model availability fast. We default to the
        # most boring, longest-running option in their production
        # tier so the wizard's pre-flight test isn't fighting a
        # delist on every other release. Power users pick a fancier
        # model from the dropdown / Settings page.
        # (History: Kimi K2 was delisted ~2025, llama-4-maverick
        # disappeared in May 2026 — both shipped here as defaults
        # and broke for new users. Don't make the same mistake
        # with this slot again.)
        default_llm="groq/llama-3.3-70b-versatile",
        api_key_url="https://console.groq.com/keys",
        # Groq's Whisper-as-a-service is the fastest commercial option.
        whisper_base_url="https://api.groq.com/openai/v1",
        whisper_model="whisper-large-v3",
        notes=(
            "Groq has no embedding API — configure Ollama or another "
            "provider in the Embedding card if you need search. The "
            "summary LLM and Whisper are extremely fast on Groq."
        ),
    ),
    "ollama": ProviderPreset(
        id="ollama",
        name="Ollama (local)",
        litellm_provider="ollama",
        default_llm="ollama_chat/llama3.1",
        requires_api_key=False,
        default_llm_base_url="http://host.docker.internal:11434",
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
        notes=(
            "Routes to many backends through one API. Pick any model "
            "from openrouter.ai/models — they all start with the "
            "openrouter/ prefix."
        ),
    ),
}


# Curated chat-model lists per provider. The Quick Setup wizard shows
# these by default; "Show all" exposes the full LiteLLM cost-map list.
#
# Why curated: the cost map carries hundreds of legacy / preview /
# regional / fine-tune entries that bury the few models a user
# actually wants. We pre-pick a small set of current flagships +
# trustworthy mid-tier options per provider.
#
# Maintenance: bumping these is a one-line edit when a new model
# ships. Verify the id exists in `litellm.model_cost` before adding,
# or the dropdown will show a dead entry.
CURATED_CHAT_MODELS: dict[str, list[str]] = {
    "openai": [
        "openai/gpt-5.5",
        "openai/gpt-5.4",
        "openai/gpt-5.4-mini",
        "openai/gpt-5.4-nano",
    ],
    "anthropic": [
        "anthropic/claude-opus-4-7",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-haiku-4-5",
    ],
    "gemini": [
        "gemini/gemini-3.1-pro-preview",
        "gemini/gemini-3-flash-preview",
        "gemini/gemini-3.1-flash-lite-preview",
        "gemini/gemini-2.5-flash",
    ],
    "groq": [
        # Default (most stable, longest-running production model).
        "groq/llama-3.3-70b-versatile",
        # Newer / experimental — keep available for power users
        # but NOT default, because Groq delists these without
        # warning (Kimi K2 in 2025, Llama 4 Maverick in May 2026).
        "groq/openai/gpt-oss-120b",
        "groq/openai/gpt-oss-20b",
        "groq/qwen/qwen3-32b",
        "groq/meta-llama/llama-4-scout-17b-16e-instruct",
    ],
    "openrouter": [
        "openrouter/anthropic/claude-opus-4-7",
        "openrouter/anthropic/claude-sonnet-4-6",
        "openrouter/anthropic/claude-haiku-4-5",
        "openrouter/openai/gpt-5.2",
        "openrouter/google/gemini-3-pro-preview",
        "openrouter/deepseek/deepseek-v3.2",
    ],
    # Ollama is dynamic (hits the user's server) — no curation here.
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


def list_chat_models(
    provider_id: str, *, include_legacy: bool = False
) -> list[str]:
    """Return chat-capable model ids for the given provider.

    By default returns only the curated short-list defined in
    `CURATED_CHAT_MODELS` (current flagships + a few useful mid-tier
    options). Pass `include_legacy=True` to fall back to the full
    LiteLLM cost map — used by the wizard's "Show all" toggle for
    power users who want a specific older or specialized model.

    The preset's default model is always first. Models in the curated
    list float to the top when include_legacy=True, so the dropdown
    still leads with the recommended choices.
    """
    try:
        preset = get_preset(provider_id)
    except KeyError:
        return []

    curated = CURATED_CHAT_MODELS.get(provider_id, [])

    if not include_legacy:
        # Curated path: just the hand-picked list, default first.
        if not curated:
            # Fallback for providers without a curation (e.g. ollama,
            # though ollama hits a separate code path).
            return [preset.default_llm]
        return _sort_with_default_first(curated, preset.default_llm)

    # Legacy path: full LiteLLM list, with curated models surfaced
    # at the top in their preferred order, then everything else
    # reverse-alphabetically (newer suffixes tend to sort higher).
    import litellm

    target = preset.litellm_provider
    raw = {
        m for m, info in litellm.model_cost.items()
        if info.get("litellm_provider") == target
        and info.get("mode") == "chat"
        and not m.startswith("ft:")
    }
    # Curated entries that exist in LiteLLM, in the order we curated.
    curated_present = [m for m in curated if m in raw]
    # Default first if it's present anywhere; keep curated order otherwise.
    head = _sort_with_default_first(curated_present, preset.default_llm)
    # The rest, reverse-alphabetical, excluding anything already at the top.
    used = set(head)
    tail = sorted([m for m in raw if m not in used], reverse=True)
    return [*head, *tail]


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


def split_ollama_tags(tags: list[str]) -> tuple[list[str], list[str]]:
    """Split a flat tag list into (chat_models, embedding_models).

    /api/tags doesn't tell us which tag is an embedder, so we use a
    name heuristic: anything containing "embed" goes in the embedding
    bucket. Covers the three standard Ollama embedders (nomic-embed,
    mxbai-embed-large, snowflake-arctic-embed) and any future model
    that follows the same naming convention. Everything else is
    classified as chat.
    """
    chat: list[str] = []
    embed: list[str] = []
    for tag in tags:
        if "embed" in tag.lower():
            embed.append(tag)
        else:
            chat.append(tag)
    return chat, embed
