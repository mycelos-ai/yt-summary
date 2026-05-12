"""One-shot LLM language detection.

Used as a fallback when we have no other signal — Whisper gives us
language info on the audio path, YouTube VTT has a `Language:`
header on the subs path, but a summary written by the user-
configured LLM in `auto` mode has neither attached. A 50-token
LLM call labels it cheaply.
"""
from collections.abc import Awaitable, Callable
import re

CompleteFn = Callable[[str], Awaitable[str]]

_VALID = {"de", "en", "fr", "es", "it", "pt", "nl", "pl", "ru", "ja", "zh"}


def _normalise(raw: str) -> str | None:
    m = re.search(r"[A-Za-z]{2}", raw or "")
    if not m:
        return None
    code = m.group(0).lower()
    return code if code in _VALID else None


async def detect_language(text: str, *, complete: CompleteFn) -> str | None:
    excerpt = text.strip()[:500]
    prompt = (
        "Identify the language of the following text. Respond with "
        "only the ISO 639-1 two-letter code (e.g. 'en', 'de', 'fr'). "
        "No explanation, no punctuation.\n\n---\n" + excerpt
    )
    out = await complete(prompt)
    return _normalise(out)
