import re
from collections.abc import Awaitable, Callable
from typing import Any

import litellm

from app.services.model_info import get_context_window
from app.services.transcript_format import format_timestamp

ProgressCb = Callable[[str], Awaitable[None]]

# Matches inline summary timestamp links produced by the LLM.
# Format: [MM:SS](#t=SECONDS) or [HH:MM:SS](#t=SECONDS)
_TIMESTAMP_LINK_RE = re.compile(r"\[\d{1,2}(?::\d{2}){1,2}\]\(#t=(\d+)\)")

# Tolerance (seconds) when matching an LLM-emitted timestamp against a
# real segment start. The LLM may pick the closest segment to the
# actual moment rather than the cue's start, so we allow a few seconds
# of slack in either direction before counting it as an anomaly.
_TIMESTAMP_TOLERANCE_S = 5

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


_TIMESTAMP_INSTRUCTION = (
    "INLINE TIMESTAMP LINKS:\n"
    "The user message includes a list of transcript paragraphs each "
    "prefixed with [MM:SS]. When you reference a specific moment from "
    "the transcript, include a clickable timestamp in this exact "
    "format: [MM:SS](#t=SECONDS). Use timestamps for: (1) the speaker's "
    "main thesis or pivot point, (2) any concrete example, "
    "demonstration, or surprising claim, (3) anything a viewer would "
    "want to verify or re-watch. Aim for 3-7 high-value timestamps "
    "total — don't sprinkle them. Always pick from the timestamps "
    "shown in the transcript paragraphs above; never invent your own.\n\n"
)


def build_system_prompt(
    *,
    language: str | None,
    extra_instructions: str | None,
    with_timestamps: bool = False,
) -> str:
    extra = (extra_instructions or "").strip()
    extra_block = f"\n\nADDITIONAL USER INSTRUCTIONS:\n{extra}" if extra else ""
    timestamp_block = _TIMESTAMP_INSTRUCTION if with_timestamps else ""
    return (
        "You analyze YouTube videos and extract their substance for someone "
        "who doesn't have time to watch.\n\n"
        f"{_language_directive(language)}\n"
        "OUTPUT FORMAT: Markdown.\n\n"
        "THINK LIKE THE VIEWER:\n"
        "The reader is deciding whether this video is worth their 30-90 "
        "minutes of attention. Your job is to give them enough that they "
        "can decide without watching — the central insight, the concrete "
        "specifics, the verdict if there is one. If the video is worth "
        "watching, your summary should make that obvious. If it isn't, "
        "your summary should make THAT obvious too. Don't sell it; "
        "report it.\n\n"
        "CORE RULE — SPECIFICITY OVER SYNTHESIS:\n"
        "Prefer concrete details over abstract synthesis. Name the things. "
        "If the speaker announces \"we're launching X, Y, and Z,\" name X, "
        "Y, and Z — do not paraphrase a list of three concrete things into "
        "\"various new capabilities.\" Keep specific product names, "
        "feature names, company names, people, dates, percentages, dollar "
        "amounts, version numbers, technical terms, and quoted phrasing. "
        "A summary that could apply to any video on the same broad topic "
        "has failed.\n\n"
        "ANSWER THE TITLE:\n"
        "The video's title is the user's reason for clicking. Read it "
        "carefully. If it poses a question (\"Which is best?\", \"Why "
        "does X work?\", \"Should you use Y?\") or makes a promise "
        "(\"5 ways to ...\", \"How to fix ...\", \"The truth about "
        "...\"), the TL;DR or the first Key Point MUST surface the "
        "answer in plain words — verdict, mechanism, list, whatever the "
        "title set up. If the speaker doesn't actually answer the "
        "title's question or sidesteps it, say so explicitly: \"The "
        "speaker doesn't pick a winner — they argue ...\" That itself "
        "is high-value information. Don't hide behind generic synthesis "
        "when the title set up an expectation.\n\n"
        "STRUCTURE:\n"
        "ALWAYS render these two sections:\n"
        "1. **TL;DR** — one paragraph, 2-4 sentences. The single most "
        "important takeaway, including the most concrete fact (a name, a "
        "number, a launch, a key concept). Not a description of the "
        "video.\n"
        "2. **Key points** — the main arguments, findings, mechanisms, "
        "or concepts the speaker presents. Each as a bullet of prose "
        "density (a sentence or two), anchored in specifics from the "
        "transcript, not generic restatements. Order by importance, "
        "not by appearance.\n\n"
        "OPTIONAL — include a section ONLY if the video has substantive "
        "content for it. Otherwise omit the heading entirely. Do NOT "
        "write acknowledgment sentences like \"no announcements were "
        "made\" or \"no quotes given\" — readers don't need to know what "
        "isn't there. Skip silently.\n"
        "3. **Specifics** — every named product, feature, partnership, "
        "deal, hire, integration, paper, person, technique, framework, "
        "concept, dataset, or quantitative claim that's worth recalling. "
        "One bullet each, with the actual name and the specific detail. "
        "(For news/launch videos, this is product launches. For "
        "educational videos, this is named techniques, papers, or "
        "concepts. For interviews, this is the people, claims, and "
        "facts that came up.)\n"
        "4. **Notable quotes** — verbatim quotes that carry weight.\n"
        "5. **Mentioned resources** — links, tools, products, papers, or "
        "projects referenced. Use the description below as the primary "
        "source for exact URLs; transcript references can fill in names "
        "when the description doesn't have them.\n\n"
        "AVOID generic restatements like:\n"
        "- ❌ \"The presentation discussed advanced AI capabilities.\"\n"
        "- ❌ \"AI is becoming a reliable agent capable of complex goals.\"\n"
        "- ❌ \"Speakers covered modern development workflows.\"\n"
        "Prefer specifics like:\n"
        "- ✅ \"Anthropic announced Claude Code Routines (webhook-triggered "
        "agent runs that prompt Claude on a schedule).\"\n"
        "- ✅ \"Stripe migrated 50,000 lines of Scala to Java in 4 days "
        "with Claude (originally estimated at 10 engineering weeks).\"\n"
        "- ✅ \"Self-attention computes a weighted sum where each token's "
        "query vector dot-products against every other token's key "
        "vector — introduced in 'Attention is All You Need' (Google, "
        "2017).\"\n\n"
        f"{timestamp_block}"
        "WHAT TO IGNORE:\n"
        "- Sponsor reads and ad segments — skip them entirely.\n"
        "- Self-promotion (\"subscribe\", \"like the video\", merch, "
        "Patreon plugs).\n"
        "- Filler words and repeated transitions.\n"
    ) + extra_block


def build_reduce_prompt(
    *,
    language: str | None,
    extra_instructions: str | None,
    with_timestamps: bool = False,
) -> str:
    extra = (extra_instructions or "").strip()
    extra_block = f"\n\nADDITIONAL USER INSTRUCTIONS:\n{extra}" if extra else ""
    timestamp_block = (
        "PRESERVE INLINE TIMESTAMP LINKS:\n"
        "Partial summaries may contain [MM:SS](#t=SECONDS) markdown "
        "links pointing into the transcript. Keep them intact in the "
        "merged result — do not rewrite, drop, or invent new ones.\n\n"
        if with_timestamps
        else ""
    )
    return (
        "You merge several partial summaries of a single YouTube video into "
        "one cohesive Markdown summary.\n\n"
        f"{_language_directive(language)}\n"
        "OUTPUT FORMAT: Markdown.\n\n"
        "THINK LIKE THE VIEWER:\n"
        "The reader is deciding whether this video is worth their 30-90 "
        "minutes of attention. The merged summary should give them enough "
        "to decide without watching — central insight, concrete specifics, "
        "verdict if there is one. Don't sell it; report it.\n\n"
        "CORE RULE — KEEP THE SPECIFICS:\n"
        "When merging, preserve every concrete name, number, product, "
        "named technique, and announcement from the partials. Deduplicate, "
        "do not abstract. If two partials each mention three different "
        "launches, the merged result lists all six — not \"various "
        "launches.\" The single fastest way to ruin this step is to "
        "paraphrase a specific list into a generic category.\n\n"
        "ANSWER THE TITLE:\n"
        "The video's title (in the user message) is the reader's reason "
        "for opening this summary. If it poses a question or makes a "
        "promise, the merged TL;DR must answer it in plain words. If "
        "none of the partials surfaced an answer, say so explicitly — "
        "don't paper over the gap with generic synthesis.\n\n"
        "Preserve this structure in the merged result:\n"
        "ALWAYS render:\n"
        "1. **TL;DR** — one paragraph, anchored in the most concrete "
        "fact across all partials.\n"
        "2. **Key points** — deduplicated, ordered by importance, "
        "specific not generic.\n\n"
        "OPTIONAL — include ONLY if the partials contain substantive "
        "content for these. Otherwise omit the heading entirely. Do NOT "
        "write acknowledgment sentences like \"no announcements were "
        "made\" — skip silently.\n"
        "3. **Specifics** — every named product, feature, partnership, "
        "deal, integration, paper, person, technique, framework, "
        "concept, or quantitative claim that appeared in any partial. "
        "Bullet each, with name and detail.\n"
        "4. **Notable quotes** — verbatim quotes that carry weight.\n"
        "5. **Mentioned resources** — links, tools, papers, or projects. "
        "Use the description (given below) as the primary source for "
        "exact URLs.\n\n"
        "Drop sponsor reads, self-promotion, and filler. If a partial "
        "summary mentions sponsors, do not surface them in the final "
        "result.\n\n"
        f"{timestamp_block}"
    ).rstrip() + extra_block


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


def _format_segments_for_prompt(
    segments: list[dict],
    *,
    total_duration_s: float | None = None,
) -> str:
    """Render JSON-ish segments as `[MM:SS] text` lines so the LLM has
    a fixed inventory of timestamps to choose from.

    The picked-from-here list also tells the model exactly which
    `t=SECONDS` values are legal for inline summary links.
    """
    lines: list[str] = []
    for seg in segments:
        start = seg.get("start")
        text = (seg.get("text") or "").strip()
        if start is None or not text:
            continue
        ts = format_timestamp(float(start), total_duration_s=total_duration_s)
        lines.append(f"[{ts}] {text}")
    return "\n".join(lines)


def _verify_summary_timestamps(
    summary: str,
    segments: list[dict] | None,
) -> tuple[int, int]:
    """Verify each [MM:SS](#t=SECONDS) link in `summary` matches a
    real segment start within ±5 s. Pure function — does not mutate
    the summary.

    Returns (verified, anomalies).
    """
    if not summary:
        return (0, 0)
    matches = _TIMESTAMP_LINK_RE.findall(summary)
    if not matches:
        return (0, 0)
    starts = [float(s["start"]) for s in (segments or []) if s.get("start") is not None]
    verified = 0
    anomalies = 0
    for raw in matches:
        try:
            t = int(raw)
        except ValueError:
            anomalies += 1
            continue
        if any(abs(t - s) <= _TIMESTAMP_TOLERANCE_S for s in starts):
            verified += 1
        else:
            anomalies += 1
    return (verified, anomalies)


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
    transcript_segments: list[dict] | None = None,
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
    transcript_segments: list of {"start": float, "text": str} dicts.
        When provided, the user prompt body is rendered as `[MM:SS] …`
        lines and the system prompt instructs the LLM to embed
        clickable [MM:SS](#t=SECONDS) links for key moments. None /
        empty list → behave exactly as before (no timestamp
        instruction). Currently only honoured in the single-shot path —
        map-reduce chunks raw text by word count, which discards
        segment alignment, so we fall back to plain transcript there.
    on_partial: optional async callback invoked after each completed chunk
        in the map-reduce path. Receives a Markdown-formatted "live"
        summary that combines the partial summaries produced so far.
        Not called in the single-shot path (no intermediate state).
    """
    progress = progress or _noop
    has_segments = bool(transcript_segments)
    system_prompt = build_system_prompt(
        language=language,
        extra_instructions=extra_instructions,
        with_timestamps=has_segments,
    )
    reduce_prompt = build_reduce_prompt(
        language=language,
        extra_instructions=extra_instructions,
        with_timestamps=has_segments,
    )

    max_tokens = await get_context_window(model, base_url)
    budget = int(max_tokens * 0.7)
    transcript_tokens = _safe_token_count(model, transcript)

    if transcript_tokens <= budget:
        await progress(
            f"summarizing (single-shot, ~{transcript_tokens} tokens, "
            f"model context {max_tokens})"
        )
        if has_segments:
            assert transcript_segments is not None
            body = (
                "TRANSCRIPT (each paragraph prefixed with [MM:SS] from the "
                "video — pick from these timestamps when emitting inline "
                "[MM:SS](#t=SECONDS) summary links):\n\n"
                + _format_segments_for_prompt(transcript_segments)
            )
        else:
            body = f"TRANSCRIPT:\n{transcript}"
        return await _completion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": _build_user_message(
                        title=title,
                        description=description,
                        body=body,
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
