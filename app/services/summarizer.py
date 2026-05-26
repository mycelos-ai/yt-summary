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
    "These timestamps are the moments worth watching even after reading "
    "the summary — the parts where seeing/hearing the original beats "
    "reading about it. The user message includes a list of transcript "
    "paragraphs each prefixed with [MM:SS]. When you reference one of "
    "those moments, include a clickable timestamp in this exact format: "
    "[MM:SS](#t=SECONDS). Pick 3-7 high-value moments: (1) the speaker's "
    "main thesis or pivot point, (2) any concrete example, "
    "demonstration, or surprising claim, (3) anything a viewer would "
    "want to verify or re-watch. Don't sprinkle them. Always pick from "
    "the timestamps shown in the transcript paragraphs above; never "
    "invent your own.\n\n"
)


def _additional_prompt_block(additional_prompt: str | None) -> str:
    """Render the optional one-shot 'USER OVERRIDE FOR THIS RUN' block.

    Returns the empty string when ``additional_prompt`` is None or
    whitespace-only — so callers can unconditionally concatenate the
    return value without having to special-case the omitted case.
    """
    text = (additional_prompt or "").strip()
    if not text:
        return ""
    return "USER OVERRIDE FOR THIS RUN:\n" + text


def _newsletter_system_prompt(language: str | None) -> str:
    """Default system prompt for email-kind items (newsletters).

    Used when the profile has no custom prompt and the content is a
    newsletter issue. Its job is triage: pull the substance out, drop
    the advertising / tracking / footer cruft that survived the
    structural cleaning, and — for digest-style issues — break the
    edition into individual items the reader can decide on.
    """
    return (
        "You are triaging a newsletter issue for a reader who subscribed "
        "but doesn't have time to read every edition. Extract the "
        "substance and help them decide what, if anything, is worth their "
        "full attention.\n\n"
        f"{_language_directive(language)}\n"
        "OUTPUT FORMAT: Markdown. Use **bold** for item headlines and key "
        "terms, bullets for enumerations. Tables with `| col | col |` "
        "(plus a `|---|---|` row) render as HTML if a two-column mapping "
        "fits.\n\n"
        "IGNORE — never surface these, even if the cleaning missed them:\n"
        "- Advertising, sponsorships, \"a word from our sponsor\".\n"
        "- Tracking notices, \"view in browser\", \"add us to your address "
        "book\".\n"
        "- Unsubscribe / manage-preferences / footer boilerplate and legal "
        "disclaimers.\n"
        "- Social-follow prompts and app-download badges.\n\n"
        "STRUCTURE:\n"
        "1. **TL;DR** — 1-3 sentences naming the single most noteworthy "
        "item in this issue. Lead with the most concrete fact (a name, a "
        "number, a launch).\n"
        "2. **In this issue** — IF the newsletter bundles multiple "
        "distinct stories, links, or items: list each as a bullet — a "
        "one-line headline in **bold**, then one sentence on why it "
        "matters or whether it's worth opening in full. Order by "
        "importance, not by where it appeared. Preserve concrete names, "
        "numbers, products, people, and claims.\n\n"
        "If the issue is a single essay rather than a digest, omit \"In "
        "this issue\" and instead give **Key points** as dense bullets "
        "anchored in specifics.\n\n"
        "CORE RULE — SPECIFICITY OVER SYNTHESIS:\n"
        "Keep specific names, numbers, companies, people, dates, and "
        "quoted phrasing. A summary that could describe any edition of "
        "any newsletter has failed."
    )


def build_system_prompt(
    *,
    language: str | None,
    custom_system_prompt: str | None = None,
    with_timestamps: bool = False,
    additional_prompt: str | None = None,
    content_kind: str = "youtube",
    interest_profile_md: str | None = None,
    with_highlights: bool = False,
) -> str:
    """Build the system prompt for single-shot summarization.

    `custom_system_prompt`, when set (the per-profile prompt loaded
    from `users.custom_summary_prompt`), REPLACES the standard
    instructions block entirely. We still wrap it with the language
    directive (so model selection respects user preference) and the
    timestamp-format instruction (which the player JS depends on for
    inline seek-to-moment links). Everything else — the SPECIFICITY,
    THINK LIKE THE VIEWER, ANSWER THE TITLE, STRUCTURE blocks — is
    the user's responsibility once they've taken control.

    additional_prompt: optional one-shot override appended at the very
        end of the system prompt, marked with a ``USER OVERRIDE FOR
        THIS RUN:`` header. Used by the Re-summarize panel to bias the
        next single run without persisting anything. None/blank → no
        block rendered.
    interest_profile_md: optional Markdown blob describing the active
        Profile's stated interests. When provided, it's appended as an
        ``Interest profile`` block so the LLM can bias which points it
        emphasises and which moments it highlights. None/blank → no
        block rendered.
    with_highlights: when True, append the structured-highlights schema
        hint (from ``app.services.highlight_parser``) so the LLM
        returns a ``{"summary": ..., "highlights": [...]}`` JSON
        envelope instead of plain Markdown. Used by the Daily Digest
        path.
    """
    custom = (custom_system_prompt or "").strip()
    timestamp_block = _TIMESTAMP_INSTRUCTION if with_timestamps else ""
    override_block = _additional_prompt_block(additional_prompt)
    override_suffix = f"\n\n{override_block}" if override_block else ""

    profile_block = ""
    if interest_profile_md and interest_profile_md.strip():
        profile_block = (
            "\n\nInterest profile (the active Profile's stated interests — "
            "use this to shape which points you emphasize in the summary "
            "and which highlights you surface):\n"
            f"{interest_profile_md.strip()}\n"
        )

    highlights_block = ""
    if with_highlights:
        from app.services.highlight_parser import HIGHLIGHTS_SCHEMA_HINT
        highlights_block = "\n\n" + HIGHLIGHTS_SCHEMA_HINT

    if custom:
        return (
            f"{_language_directive(language)}\n"
            "OUTPUT FORMAT: Markdown. Tables with `| col | col |` "
            "syntax (plus a `|---|---|` separator row) render as "
            "proper HTML tables.\n\n"
            f"{custom}\n\n"
            f"{timestamp_block}"
        ).rstrip() + "\n" + override_suffix + profile_block + highlights_block
    if content_kind == "email":
        # Newsletters have no timestamps; the newsletter prompt is
        # self-contained, so we only append the one-shot override
        # plus the optional interest-profile + highlights blocks so
        # the daily-digest path works for newsletter items too.
        return (
            _newsletter_system_prompt(language)
            + override_suffix
            + profile_block
            + highlights_block
        )
    return (
        "You analyze YouTube videos and extract their substance for someone "
        "who doesn't have time to watch.\n\n"
        f"{_language_directive(language)}\n"
        "OUTPUT FORMAT: Markdown. Tables with `| col | col |` syntax "
        "(plus a `|---|---|` separator row) render as proper HTML "
        "tables — use them for two-column mappings (concept → "
        "explanation, name → role, option → tradeoff). Use bullets "
        "for enumerations and **bold** for key terms. Don't force a "
        "table where bullets read better.\n\n"
        "THINK LIKE THE VIEWER:\n"
        "The reader has already decided this video is interesting — they "
        "just want to avoid watching the whole 30-90 minutes. Your job: "
        "extract the core substance, distill it into a short scannable "
        "form, and flag the specific moments worth watching anyway (via "
        "inline timestamp links). You're saving them time. They committed "
        "to the topic; you commit attention to the video on their "
        "behalf.\n\n"
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
    ) + override_suffix + profile_block + highlights_block


def _newsletter_reduce_prompt(language: str | None) -> str:
    """Merge prompt for long, chunked newsletter issues."""
    return (
        "You merge several partial summaries of a single newsletter issue "
        "into one cohesive Markdown summary.\n\n"
        f"{_language_directive(language)}\n"
        "OUTPUT FORMAT: Markdown. **bold** headlines, bullets for items.\n\n"
        "Preserve this structure in the merged result:\n"
        "1. **TL;DR** — 1-3 sentences naming the single most noteworthy "
        "item across all partials.\n"
        "2. **In this issue** — one bullet per distinct story/item: a "
        "**bold** headline plus one sentence on why it matters. "
        "Deduplicate across partials, do NOT abstract — if two partials "
        "list different items, the merged result lists all of them. Order "
        "by importance.\n\n"
        "If the issue is a single essay, use **Key points** instead. "
        "Drop any advertising, sponsorships, tracking, or unsubscribe "
        "boilerplate that leaked into the partials."
    )


def build_reduce_prompt(
    *,
    language: str | None,
    with_timestamps: bool = False,
    additional_prompt: str | None = None,
    content_kind: str = "youtube",
) -> str:
    """Reduce prompt is intentionally NOT user-customizable — it's an
    internal map-reduce mechanic, not a user-facing summary style. The
    final output respects whatever schema/tone the per-profile system
    prompt established because the LLM applies it to the merged
    result.

    additional_prompt: see build_system_prompt — the same one-shot
        override block is appended at the end of the reduce prompt so
        chunked videos honour the user's per-run tweak too.
    """
    timestamp_block = (
        "PRESERVE INLINE TIMESTAMP LINKS:\n"
        "Partial summaries may contain [MM:SS](#t=SECONDS) markdown "
        "links pointing into the transcript. Keep them intact in the "
        "merged result — do not rewrite, drop, or invent new ones.\n\n"
        if with_timestamps
        else ""
    )
    override_block = _additional_prompt_block(additional_prompt)
    override_suffix = f"\n\n{override_block}" if override_block else ""
    if content_kind == "email":
        return _newsletter_reduce_prompt(language) + override_suffix
    return (
        "You merge several partial summaries of a single YouTube video into "
        "one cohesive Markdown summary.\n\n"
        f"{_language_directive(language)}\n"
        "OUTPUT FORMAT: Markdown. Tables with `| col | col |` syntax "
        "(plus a `|---|---|` separator row) render as HTML — use them "
        "for two-column mappings if the partials' content fits that "
        "shape. Otherwise bullets.\n\n"
        "THINK LIKE THE VIEWER:\n"
        "The reader has already decided this video is interesting — they "
        "just want to avoid watching the whole 30-90 minutes. The merged "
        "summary extracts the core substance and flags the specific "
        "moments worth watching anyway (via inline timestamp links). "
        "You're saving them time.\n\n"
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
    ).rstrip() + override_suffix


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
    custom_system_prompt: str | None = None,
    playlist_context: list[str] | None = None,
    transcript_segments: list[dict] | None = None,
    additional_prompt: str | None = None,
    content_kind: str = "youtube",
    progress: ProgressCb | None = None,
    on_partial: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Summarize a transcript.

    title, description: video metadata; surfaced to the model for context
        (especially valuable for extracting "Mentioned resources" from the
        description).
    language: BCP-47-ish code ("auto", "de", "en", ...). "auto" means
        match the transcript's language.
    custom_system_prompt: per-profile system prompt loaded from
        `users.custom_summary_prompt`. When set, it replaces the
        standard instructions block — only the language directive
        and the timestamp-format instruction stay wrapped around it.
        Each profile owns its prompt; there's no global default left
        in the runtime.
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
        custom_system_prompt=custom_system_prompt,
        with_timestamps=has_segments,
        additional_prompt=additional_prompt,
        content_kind=content_kind,
    )
    reduce_prompt = build_reduce_prompt(
        language=language,
        with_timestamps=has_segments,
        additional_prompt=additional_prompt,
        content_kind=content_kind,
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


async def summarize_with_highlights(
    *,
    transcript: str,
    model: str,
    api_key: str,
    base_url: str | None,
    title: str = "",
    description: str = "",
    language: str | None = None,
    custom_system_prompt: str | None = None,
    interest_profile_md: str | None = None,
    playlist_context: list[str] | None = None,
    transcript_segments: list[dict] | None = None,
    additional_prompt: str | None = None,
    progress: ProgressCb | None = None,
    on_partial: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[dict] | None]:
    """Like `summarize()`, but asks the LLM for a JSON envelope with
    structured highlights alongside the summary.

    Returns (summary_markdown, highlights_or_none). `highlights` is a
    list of `{text, rank, reason}` dicts, an empty list (LLM said
    "nothing noteworthy"), or None (LLM didn't follow the JSON shape —
    pipeline falls back to legacy behaviour).

    Implementation: re-uses `summarize()` with a schema-hint addendum
    threaded through `additional_prompt`. Map-reduce path discards
    highlights from intermediate chunks and only honours the final
    reduce LLM's JSON envelope.
    """
    from app.services.highlight_parser import (
        HIGHLIGHTS_SCHEMA_HINT,
        parse_summary_payload,
    )

    schema_addendum = (
        "\n\n[OUTPUT-FORMAT OVERRIDE FOR THIS RUN]\n"
        + HIGHLIGHTS_SCHEMA_HINT
    )
    composed_additional = (
        (additional_prompt or "") + schema_addendum
    )
    raw = await summarize(
        transcript=transcript,
        model=model,
        api_key=api_key,
        base_url=base_url,
        title=title,
        description=description,
        language=language,
        custom_system_prompt=_inject_profile_into_custom(
            custom_system_prompt, interest_profile_md,
        ),
        playlist_context=playlist_context,
        transcript_segments=transcript_segments,
        additional_prompt=composed_additional,
        progress=progress,
        on_partial=on_partial,
    )
    return parse_summary_payload(raw)


def _inject_profile_into_custom(
    custom: str | None, profile_md: str | None,
) -> str | None:
    """Splice the interest profile into the per-Profile custom prompt.

    When a Profile has a custom_system_prompt set, `build_system_prompt`
    uses it in place of the standard prompt. To still surface the
    interest profile as context, we prepend an "Interest profile:" block
    to the custom prompt itself when there is one to surface.
    """
    if not profile_md or not profile_md.strip():
        return custom
    block = (
        "Interest profile (the active Profile's stated interests):\n"
        f"{profile_md.strip()}\n\n"
    )
    if custom is None:
        return block
    return block + custom
