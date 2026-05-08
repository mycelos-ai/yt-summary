from collections.abc import Awaitable, Callable
from typing import Any

import litellm

from app.services.model_info import get_context_window

ProgressCb = Callable[[str], Awaitable[None]]

# Language code → human label used in the prompt. "auto" means: don't
# fix a language; let the model match the transcript's language.
LANGUAGE_LABELS: dict[str, str] = {
    "auto": "match the transcript's language",
    "de": "German",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pt": "Portuguese",
}


def _language_directive(code: str | None) -> str:
    code = (code or "auto").strip().lower()
    label = LANGUAGE_LABELS.get(code, LANGUAGE_LABELS["auto"])
    if code == "auto":
        return "OUTPUT LANGUAGE: match the transcript's language."
    return f"OUTPUT LANGUAGE: {label}."


def build_system_prompt(
    *, language: str | None, extra_instructions: str | None
) -> str:
    extra = (extra_instructions or "").strip()
    extra_block = f"\n\nADDITIONAL USER INSTRUCTIONS:\n{extra}" if extra else ""
    return (
        "You analyze YouTube videos and extract their substance for someone "
        "who doesn't have time to watch.\n\n"
        f"{_language_directive(language)}\n"
        "OUTPUT FORMAT: Markdown.\n\n"
        "CORE RULE — SPECIFICITY OVER SYNTHESIS:\n"
        "Prefer concrete details over abstract synthesis. Name the things. "
        "If the speaker announces \"we're launching X, Y, and Z,\" name X, "
        "Y, and Z — do not paraphrase a list of three concrete things into "
        "\"various new capabilities.\" Keep specific product names, "
        "feature names, company names, people, dates, percentages, dollar "
        "amounts, version numbers, and quoted phrasing. A summary that "
        "could apply to any video on the same broad topic has failed.\n\n"
        "STRUCTURE:\n"
        "1. **TL;DR** — one paragraph, 2-4 sentences. The single most "
        "important takeaway, including the most concrete fact (a name, a "
        "number, a launch). Not a description of the video.\n"
        "2. **Announcements / concrete claims** (if any) — every named "
        "product, feature, partnership, deal, hire, integration, or "
        "quantitative claim made. One bullet each, with the actual name "
        "and the specific detail. Skip this section only if the video "
        "contains no announcements (e.g. a pure opinion piece).\n"
        "3. **Key points** — the main arguments or findings the speaker "
        "makes. Each as a bullet of prose density (a sentence or two), "
        "anchored in specifics from the transcript, not generic "
        "restatements. Order by importance, not by appearance.\n"
        "4. **Notable quotes** (if any) — verbatim quotes that carry "
        "weight. Omit the section if none.\n"
        "5. **Mentioned resources** (if any) — links, tools, products, "
        "papers, or projects referenced. Use the description below as the "
        "primary source for exact URLs; transcript references can fill in "
        "names when the description doesn't have them. Omit the section "
        "if nothing was mentioned.\n\n"
        "AVOID generic restatements like:\n"
        "- ❌ \"The presentation discussed advanced AI capabilities.\"\n"
        "- ❌ \"AI is becoming a reliable agent capable of complex goals.\"\n"
        "- ❌ \"Speakers covered modern development workflows.\"\n"
        "Prefer specifics like:\n"
        "- ✅ \"Anthropic announced Claude Code Routines (webhook-triggered "
        "agent runs that prompt Claude on a schedule).\"\n"
        "- ✅ \"Stripe migrated 50,000 lines of Scala to Java in 4 days "
        "with Claude (originally estimated at 10 engineering weeks).\"\n\n"
        "WHAT TO IGNORE:\n"
        "- Sponsor reads and ad segments — skip them entirely.\n"
        "- Self-promotion (\"subscribe\", \"like the video\", merch, "
        "Patreon plugs).\n"
        "- Filler words and repeated transitions.\n"
    ) + extra_block


def build_reduce_prompt(
    *, language: str | None, extra_instructions: str | None
) -> str:
    extra = (extra_instructions or "").strip()
    extra_block = f"\n\nADDITIONAL USER INSTRUCTIONS:\n{extra}" if extra else ""
    return (
        "You merge several partial summaries of a single YouTube video into "
        "one cohesive Markdown summary.\n\n"
        f"{_language_directive(language)}\n"
        "OUTPUT FORMAT: Markdown.\n\n"
        "CORE RULE — KEEP THE SPECIFICS:\n"
        "When merging, preserve every concrete name, number, product, and "
        "announcement from the partials. Deduplicate, do not abstract. If "
        "two partials each mention three different launches, the merged "
        "result lists all six — not \"various launches.\" The single "
        "fastest way to ruin this step is to paraphrase a specific list "
        "into a generic category.\n\n"
        "Preserve this structure in the merged result:\n"
        "1. **TL;DR** — one paragraph, anchored in the most concrete "
        "fact across all partials.\n"
        "2. **Announcements / concrete claims** (if any) — every named "
        "product, feature, partnership, deal, integration, or "
        "quantitative claim that appeared in any partial. Bullet each.\n"
        "3. **Key points** — deduplicated, ordered by importance, "
        "specific not generic.\n"
        "4. **Notable quotes** (if any).\n"
        "5. **Mentioned resources** (if any) — use the description (given "
        "below) as the primary source for exact URLs.\n\n"
        "Drop sponsor reads, self-promotion, and filler. If a partial "
        "summary mentions sponsors, do not surface them in the final "
        "result."
    ) + extra_block


def _build_user_message(
    *,
    title: str,
    description: str,
    body: str,
    playlist_context: list[str] | None = None,
) -> str:
    """Body is either the full transcript (single-shot) or one chunk
    (map step) or the joined partials (reduce step).

    playlist_context: names of the user's own playlists this video
    sits in. The user organises their queue thematically (e.g.
    "AI", "Long-form interviews"), so naming the bucket lets the
    LLM bias the summary toward that topic.
    """
    parts = [
        f"VIDEO TITLE: {title}",
        f"VIDEO DESCRIPTION:\n{description or '(empty)'}",
    ]
    if playlist_context:
        joined = ", ".join(playlist_context)
        parts.append(
            f"ADDED TO PLAYLIST(S): {joined}\n"
            f"(The user files videos thematically; treat these names "
            f"as topic hints and emphasise the matching angle in the "
            f"summary.)"
        )
    parts.append("---")
    parts.append(body)
    return "\n\n".join(parts)


async def _noop(_: str) -> None:
    return None


def _safe_token_count(model: str, text: str) -> int:
    try:
        return litellm.token_counter(model=model, text=text)
    except Exception:
        return int(len(text.split()) * 1.3)


def _split_into_chunks(transcript: str, model: str, target_tokens: int) -> list[str]:
    words = transcript.split()
    if not words:
        return []
    approx_words_per_chunk = max(int(target_tokens * 0.6), 100)
    chunks: list[str] = []
    i = 0
    while i < len(words):
        end = min(i + approx_words_per_chunk, len(words))
        chunk = " ".join(words[i:end])
        while _safe_token_count(model, chunk) > target_tokens and end - i > 1:
            end = i + max((end - i) // 2, 1)
            chunk = " ".join(words[i:end])
        chunks.append(chunk)
        i = end
    return chunks


async def _completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    api_key: str,
    base_url: str | None,
) -> str:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
    }
    if base_url:
        kwargs["api_base"] = base_url
    response = await litellm.acompletion(**kwargs)
    return response.choices[0].message.content or ""


async def summarize(
    *,
    transcript: str,
    model: str,
    api_key: str,
    base_url: str | None,
    title: str = "",
    description: str = "",
    language: str | None = None,
    extra_instructions: str | None = None,
    playlist_context: list[str] | None = None,
    progress: ProgressCb | None = None,
    on_partial: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Summarize a transcript.

    title, description: video metadata; surfaced to the model for context
        (especially valuable for extracting "Mentioned resources" from the
        description).
    language: BCP-47-ish code ("auto", "de", "en", ...). "auto" means
        match the transcript's language.
    extra_instructions: free-form addendum appended to the system prompt
        (user preference for tone, length, style, etc.).
    playlist_context: names of the user's own playlists this video is
        in. Surfaced to the model as topic hints — the user organises
        their queue thematically, so naming the bucket helps the
        summary lean into that angle. Empty list / None omits the
        section entirely.
    on_partial: optional async callback invoked after each completed chunk
        in the map-reduce path. Receives a Markdown-formatted "live"
        summary that combines the partial summaries produced so far.
        Not called in the single-shot path (no intermediate state).
    """
    progress = progress or _noop
    system_prompt = build_system_prompt(
        language=language, extra_instructions=extra_instructions
    )
    reduce_prompt = build_reduce_prompt(
        language=language, extra_instructions=extra_instructions
    )

    max_tokens = await get_context_window(model, base_url)
    budget = int(max_tokens * 0.7)
    transcript_tokens = _safe_token_count(model, transcript)

    if transcript_tokens <= budget:
        await progress(
            f"summarizing (single-shot, ~{transcript_tokens} tokens, "
            f"model context {max_tokens})"
        )
        return await _completion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": _build_user_message(
                        title=title,
                        description=description,
                        body=f"TRANSCRIPT:\n{transcript}",
                        playlist_context=playlist_context,
                    ),
                },
            ],
            api_key=api_key,
            base_url=base_url,
        )

    chunks = _split_into_chunks(transcript, model, target_tokens=budget // 2)
    partials: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        await progress(f"summarizing chunk {idx}/{len(chunks)}")
        part = await _completion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": _build_user_message(
                        title=title,
                        description=description,
                        body=(
                            f"TRANSCRIPT (part {idx} of {len(chunks)}):\n\n"
                            f"{chunk}"
                        ),
                        playlist_context=playlist_context,
                    ),
                },
            ],
            api_key=api_key,
            base_url=base_url,
        )
        partials.append(part)
        if on_partial is not None:
            await on_partial(_render_live_summary(partials, total=len(chunks)))

    await progress(f"merging {len(chunks)} partial summaries")
    return await _completion(
        model=model,
        messages=[
            {"role": "system", "content": reduce_prompt},
            {
                "role": "user",
                "content": _build_user_message(
                    title=title,
                    description=description,
                    body=(
                        "PARTIAL SUMMARIES:\n\n"
                        + "\n\n---\n\n".join(partials)
                    ),
                    playlist_context=playlist_context,
                ),
            },
        ],
        api_key=api_key,
        base_url=base_url,
    )


def _render_live_summary(partials: list[str], *, total: int) -> str:
    """Format partial summaries for in-flight display.

    The user sees this in the UI while map-reduce is still running, so
    keep it plain and obvious. The final reduce-merged summary will
    overwrite this when the job finishes."""
    done = len(partials)
    pct = int(round(done / total * 100)) if total > 0 else 0
    header = (
        f"> ⏳ Working summary · {done} of {total} sections analyzed "
        f"({pct}%). The final, merged summary will replace this when the "
        "job finishes.\n\n"
    )
    body_blocks = []
    for idx, part in enumerate(partials, start=1):
        body_blocks.append(f"### Section {idx} / {total}\n\n{part}")
    return header + "\n\n".join(body_blocks)
