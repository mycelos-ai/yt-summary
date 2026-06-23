"""Persona reply — a clearly simulated, in-character perspective of a
speaker, grounded in their ATTRIBUTED CLAIMS.

Mirrors services/chat.stream_reply mechanics exactly (same litellm
streaming kwargs, reuses chat_core.build_messages); only the system
prompt differs. The prompt is the safety boundary — it forbids putting
other speakers' / unattributed words in the persona's mouth and frames
claims as "extracted from your sources" paraphrases, not verbatim
quotes. The interface owns the AI-disclaimer banner, so the prompt tells
the model NOT to self-disclaim in the reply.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import litellm

from app.models import ChatMessage
from app.services.chat_core import build_messages


_EVIDENCE_TRUNCATE = 200  # chars; keeps rendered lines readable


def _render_claims(claims: list[dict]) -> str:
    if not claims:
        return "(no attributed claims retrieved for this question yet)"
    lines: list[str] = []
    for c in claims:
        conf = c.get("attribution_confidence")
        method = c.get("attribution_method")
        # None/unspecified attribution → emit "unattributed" so the hedge rule fires
        if not method:
            method = "unattributed"
        tag = f"[attribution: {method}"
        if isinstance(conf, (int, float)):
            tag += f", confidence {conf:.2f}"
        tag += "]"
        src = c.get("source_title") or "a source"
        ts = c.get("evidence_start_s")
        where = f" ({src}" + (f" @ {int(ts)}s" if isinstance(ts, (int, float)) else "") + ")"
        # Include evidence excerpt when available so the model can cite it
        ev = (c.get("evidence_text") or "").strip()
        if ev:
            snippet = ev[:_EVIDENCE_TRUNCATE] + ("…" if len(ev) > _EVIDENCE_TRUNCATE else "")
            evidence_clause = f' [evidence: "{snippet}"]'
        else:
            evidence_clause = ""
        lines.append(f"- {c.get('claim', '')} {tag}{where}{evidence_clause}")
    return "\n".join(lines)


def build_speaker_system_prompt(
    *, speaker, claims: list[dict], source_context: str,
    seed_ts: str | None = None, seed_quote: str | None = None,
    speaker_present: bool = True,
) -> str:
    name = speaker.name
    role_clause = f", {speaker.role}" if getattr(speaker, "role", None) else ""
    style_note = getattr(speaker, "style_note", None) or "(no style note on file)"
    seed_block = ""
    if seed_ts or seed_quote:
        ts = f"[{seed_ts}] " if seed_ts else ""
        quote = f"'{seed_quote}'" if seed_quote else ""
        seed_block = (
            f"\nThe viewer is jumping in at this moment: {ts}{quote}\n"
        )

    speculative_clause = ""
    if not speaker_present:
        speculative_clause = (
            f"\nSPECULATIVE MODE: {name} did NOT actually appear in the CURRENT SOURCE. "
            "Treat the transcript as a TOPIC to react to, NOT as something you participated in. "
            "Make clear your take is speculative/extrapolated from your known positions "
            "(e.g. 'I wasn't part of this, but based on what I've argued, I'd probably say…'). "
            "Do not claim to have been there or to have said anything in it.\n"
        )

    return (
        f"You are a clearly simulated, in-character perspective of {name}"
        f"{role_clause}, talking with a viewer. Speak in the first person, in "
        f"their voice — match their tone, rhetorical habits, bluntness or "
        f"warmth. You are NOT the real {name} and must not claim to be.\n\n"
        "LANGUAGE: reply in the SAME language as the viewer's latest message, "
        "regardless of the language of the source or the dossier.\n\n"
        "GROUNDING:\n"
        "- Anchor everything assertible in the ATTRIBUTED CLAIMS and the "
        "attributed excerpts below. These are attributed claims extracted "
        f"from the viewer's sources — paraphrases of {name}'s positions, "
        "not verbatim quotes. Where available, each claim includes an "
        "evidence excerpt you may cite. Do NOT present claims or excerpts "
        "as exact verbatim utterances.\n"
        f"- Each claim is tagged with how confidently it was attributed to "
        f"{name}. For claims marked 'llm_inferred' OR 'unattributed' OR with "
        "confidence below 0.7, speak more tentatively (\"I think I've argued…\", "
        "\"as I recall…\") rather than asserting them flatly.\n"
        "- The CURRENT SOURCE TRANSCRIPT is context for flow and style ONLY. "
        f"Do NOT present things from it as {name}'s statements unless they are "
        "attributed.\n"
        f"- NEVER put other speakers' words, or unattributed words, in {name}'s "
        "mouth. If attribution is unclear, say the source is ambiguous.\n"
        "- If the viewer points out a contradiction across sources, engage "
        "honestly and cite the sources.\n"
        "- NEVER invent specific facts, numbers, quotes, or beliefs.\n"
        "- Don't break character to disclaim you're an AI — the interface "
        "already says so.\n\n"
        f"STYLE NOTE: {style_note}\n"
        f"{speculative_clause}"
        f"{seed_block}\n"
        f"ATTRIBUTED CLAIMS (extracted from {name}'s sources, each with its "
        "source and an attribution-confidence tag):\n"
        f"{_render_claims(claims)}\n\n"
        "CURRENT SOURCE CONTEXT (style/flow only — not a source of "
        f"{name}'s claims):\n"
        f"{source_context or '(none)'}"
    )


async def stream_speaker_reply(
    *,
    speaker,
    source_context: str,
    claims: list[dict],
    history: list[ChatMessage],
    user_message: str,
    seed_ts: str | None = None,
    seed_quote: str | None = None,
    speaker_present: bool = True,
    model: str,
    api_key: str,
    base_url: str | None,
) -> AsyncIterator[str]:
    messages = build_messages(
        system_prompt=build_speaker_system_prompt(
            speaker=speaker, claims=claims, source_context=source_context,
            seed_ts=seed_ts, seed_quote=seed_quote, speaker_present=speaker_present,
        ),
        history=[(m.role, m.content) for m in history],
        user_message=user_message,
    )

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
        "stream": True,
    }
    if base_url:
        kwargs["api_base"] = base_url

    response = await litellm.acompletion(**kwargs)
    async for chunk in response:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
