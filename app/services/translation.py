"""Chunked LLM translation for long transcripts.

Strategy: split on paragraph boundaries; if a single paragraph is
larger than the target chunk size, split it on sentence boundaries.
Every non-first/non-last chunk gets a 50-word overlap context
window from the surrounding chunks, marked as "do not translate"
in the prompt so terminology stays consistent across the seams.
"""
import re
from collections.abc import Awaitable, Callable

DEFAULT_TARGET_WORDS = 1500
DEFAULT_OVERLAP_WORDS = 50

# Language code → human-readable name used in the prompt.
LANGUAGE_NAMES = {
    "de": "German",
    "en": "English",
    "en_US": "English",
    "en_GB": "English",
    "fr": "French",
    "es": "Spanish",
}

# Sentence terminator regex: matches `.`, `!`, `?` followed by whitespace.
# Conservative — we'd rather have an over-long chunk than split mid-quote.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Prevent literal occurrences of our delimiter tags inside the
# chunk content from breaking the prompt structure. We don't strip
# the angle brackets entirely (in case the user's text genuinely
# contains "<TRANSLATE>" as a discussed term) — we substitute the
# brackets with a unicode lookalike. The LLM still understands
# the original text but our parsing fence stays unambiguous.
_FENCE_TAGS = ("TRANSLATE", "CONTEXT_BEFORE", "CONTEXT_AFTER")
_FENCE_PATTERN = re.compile(
    r"</?(?:" + "|".join(_FENCE_TAGS) + r")>"
)


def _escape_fence_tags(text: str) -> str:
    """Replace our delimiter tags inside user content so they can't
    confuse the LLM's parsing of the prompt structure."""
    return _FENCE_PATTERN.sub(
        lambda m: m.group(0).replace("<", "⟨").replace(">", "⟩"),
        text,
    )


def _split_on_sentences(paragraph: str, target_words: int) -> list[str]:
    sentences = _SENTENCE_END.split(paragraph)
    out: list[str] = []
    cur: list[str] = []
    cur_words = 0
    for sent in sentences:
        wc = len(sent.split())
        if cur and cur_words + wc > target_words:
            out.append(" ".join(cur).strip())
            cur, cur_words = [], 0
        # Last-resort hard split: a single "sentence" larger than the
        # target (e.g. an auto-caption paragraph with no punctuation)
        # gets chopped on word boundaries. Better an awkward break
        # than a chunk that breaks the LLM context window.
        if not cur and wc > target_words:
            words = sent.split()
            for i in range(0, len(words), target_words):
                out.append(" ".join(words[i:i + target_words]))
            continue
        cur.append(sent)
        cur_words += wc
    if cur:
        out.append(" ".join(cur).strip())
    return [c for c in out if c]


def chunk_text(text: str, *, target_words: int = DEFAULT_TARGET_WORDS) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_words = 0
    for p in paragraphs:
        pw = len(p.split())
        if pw > target_words:
            if buf:
                chunks.append("\n\n".join(buf))
                buf, buf_words = [], 0
            chunks.extend(_split_on_sentences(p, target_words))
            continue
        if buf and buf_words >= target_words:
            chunks.append("\n\n".join(buf))
            buf, buf_words = [], 0
        buf.append(p)
        buf_words += pw
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def overlap_tail(text: str, *, n: int = DEFAULT_OVERLAP_WORDS) -> str:
    return " ".join(text.split()[-n:])


def overlap_head(text: str, *, n: int = DEFAULT_OVERLAP_WORDS) -> str:
    return " ".join(text.split()[:n])


def build_prompt(
    *,
    source_lang: str,
    target_lang: str,
    chunk: str,
    previous_tail: str | None,
    next_head: str | None,
) -> str:
    parts = [
        f"You are a professional translator from {source_lang} to {target_lang}.",
        "Translate ONLY the text inside the <TRANSLATE> tags.",
        "Use any context sections for continuity —",
        "they show what comes before and after in the original text, but you",
        "must not include them in your output.",
        "",
        "Preserve paragraph breaks. Keep technical terms and proper names",
        "consistent across chunks. Do not add a preamble, do not summarize,",
        "do not add explanations. Output only the translation.",
        "",
    ]
    if previous_tail:
        parts += ["<CONTEXT_BEFORE>", _escape_fence_tags(previous_tail), "</CONTEXT_BEFORE>", ""]
    parts += ["<TRANSLATE>", _escape_fence_tags(chunk), "</TRANSLATE>", ""]
    if next_head:
        parts += ["<CONTEXT_AFTER>", _escape_fence_tags(next_head), "</CONTEXT_AFTER>"]
    return "\n".join(parts)


CompleteFn = Callable[[str], Awaitable[str]]


async def translate(
    text: str,
    *,
    source_language: str,
    target_language: str,
    complete: CompleteFn,
    target_words: int = DEFAULT_TARGET_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """Chunk + translate. `complete(prompt) -> output_text` is the
    LLM dependency, injected so tests can stub it without touching
    LiteLLM."""
    if source_language == target_language:
        return text
    src_name = LANGUAGE_NAMES.get(source_language, source_language)
    tgt_name = LANGUAGE_NAMES.get(target_language, target_language)
    chunks = chunk_text(text, target_words=target_words)
    results: list[str] = []
    for i, chunk_str in enumerate(chunks):
        prev_tail = overlap_tail(chunks[i - 1], n=overlap_words) if i > 0 else None
        next_head = overlap_head(chunks[i + 1], n=overlap_words) if i + 1 < len(chunks) else None
        prompt = build_prompt(
            source_lang=src_name, target_lang=tgt_name,
            chunk=chunk_str, previous_tail=prev_tail, next_head=next_head,
        )
        translated = await complete(prompt)
        results.append(translated.strip())
        if progress is not None:
            progress(i + 1, len(chunks))
    return "\n\n".join(results)
