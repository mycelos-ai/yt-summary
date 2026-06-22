"""Attributed claim extraction + persona-prompt retrieval.

ONE entry point (extract_claims_for_source) serves both extraction
triggers (the pipeline piggyback now; the standalone backfill job in
PR 4). The LLM is given the expected speakers BY NAME and must attribute
each claim to exactly one of them with evidence + a timestamp/offset +
how confidently it tied the claim to that speaker. Statements it can't
confidently attribute become NO claim — attribution beats style
(spec rule #3). Best-effort: returns [] on garbage and NEVER raises, so
the pipeline piggyback can call it without a guard of its own.

Context-window-aware grounding (Finding 5):
- If the transcript fits in ≤60% of the model's context window → extract
  from the full transcript (precise evidence_start_s).
- If it does NOT fit → fall back to the already-computed summary (+ highlights
  if present). NEVER blind-truncate the transcript.
- No map-reduce/chunking in v1.5.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import litellm

from app.repos import speaker_claims as claims_repo
from app.repos import speakers as speakers_repo
from app.services import model_info
from app.services.highlight_parser import _extract_json_blob

log = logging.getLogger(__name__)

# Attribution methods the persona prompt understands. An LLM value
# outside this set is coerced to None (the prompt then hedges).
_ATTR_METHODS = {
    "explicit_name", "speaker_marker", "metadata_context", "llm_inferred", "manual",
}

# Fraction of the context window to budget for the grounding text.
# Leaves ~40% for the prompt scaffold + expected JSON output.
_TRANSCRIPT_WINDOW_FRACTION = 0.60


def _system_prompt(speaker_names: list[str]) -> str:
    names = ", ".join(speaker_names)
    return (
        "You extract ATTRIBUTED claims from a transcript or article for a "
        "track-record dossier. The following named people are expected to "
        f"appear in this source: {names}.\n\n"
        "For each substantive position, prediction, or factual assertion, "
        "attribute it to EXACTLY ONE of those named people and record the "
        "evidence. A claim you cannot confidently tie to one of those named "
        "people MUST be dropped — never guess, never attribute to someone not "
        "in the list. Attribution beats coverage: fewer, well-attributed "
        "claims are better than many shaky ones.\n\n"
        "Return ONE JSON object, no prose, with this exact shape:\n"
        "{\n"
        '  "claims": [\n'
        "    {\n"
        '      "speaker": "<one of the expected names, verbatim>",\n'
        '      "claim": "<the position in their words, paraphrased, <40 words>",\n'
        '      "topic": "<short topical tag for grouping, e.g. \\"markets\\">",\n'
        '      "evidence_text": "<the supporting excerpt>",\n'
        '      "evidence_start_s": <integer seconds into the video, or null>,\n'
        '      "text_start_offset": <integer char offset for article/text, or null>,\n'
        '      "confidence": <0..1 paraphrase fidelity>,\n'
        '      "attribution_method": "<explicit_name|speaker_marker|metadata_context|llm_inferred>",\n'
        '      "attribution_confidence": <0..1 confidence the claim is THIS speaker\'s>,\n'
        '      "attribution_reason": "<short why, e.g. \\"named in prior sentence\\">"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        'If nothing is confidently attributable, return {"claims": []}.'
    )


def _user_message(source, *, use_summary: bool) -> str:
    """Build the user-turn grounding message.

    When use_summary=True the source body is the pre-computed summary
    (+ highlights if present) rather than the raw transcript. This is
    the context-window fallback for long sources.
    """
    if use_summary:
        body = source.summary or ""
        if source.highlights_json:
            try:
                highlights = json.loads(source.highlights_json)
                if isinstance(highlights, list) and highlights:
                    bullet_lines = "\n".join(
                        f"- {h.get('text', '')}" if isinstance(h, dict) else f"- {h}"
                        for h in highlights[:20]
                    )
                    body = body + "\n\nKEY HIGHLIGHTS:\n" + bullet_lines
            except (json.JSONDecodeError, TypeError):
                pass
        grounding_label = "SUMMARY"
    else:
        body = source.transcript or ""
        grounding_label = "TRANSCRIPT"

    return f"SOURCE TITLE: {source.title}\n\n{grounding_label}:\n{body}"


def _coerce_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    return None


def _coerce_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


async def extract_claims_for_source(
    db,
    source,
    speaker_ids: list[int],
    *,
    model: str,
    api_key: str,
    base_url: str | None,
) -> list[dict]:
    """Extract + persist attributed claims for `source`.

    Returns the list of accepted claim dicts (also persisted). Returns []
    on garbage response / no model / any error. Never raises.

    Long-source handling: resolves the model's context window via
    model_info.get_context_window, estimates transcript token cost
    (chars/4 heuristic), and uses the summary as grounding text when
    the transcript would exceed 60% of the window. Never truncates.
    """
    if not speaker_ids or not model:
        return []

    # Resolve expected speakers by id → Speaker object.
    speakers = []
    for sid in speaker_ids:
        sp = await speakers_repo.get_speaker(db, sid)
        if sp is not None:
            speakers.append(sp)
    if not speakers:
        return []

    by_name: dict[str, Any] = {sp.name.strip().lower(): sp for sp in speakers}
    names = [sp.name for sp in speakers]

    # --- Context-window-aware grounding (Finding 5) ---
    # Default: use full transcript for precise evidence_start_s.
    # Fallback: use summary when transcript is too long.
    use_summary = False
    transcript = source.transcript or ""
    if transcript:
        try:
            context_window = await model_info.get_context_window(model, base_url)
            budget = int(context_window * _TRANSCRIPT_WINDOW_FRACTION)
            # chars/4 heuristic matches what summarizer.py uses via _safe_token_count
            transcript_tokens = len(transcript) // 4
            if transcript_tokens > budget:
                use_summary = True
                log.debug(
                    "claim extraction for %s: transcript ~%d tokens exceeds budget %d "
                    "(window=%d), falling back to summary",
                    getattr(source, "id", None), transcript_tokens, budget, context_window,
                )
        except Exception as e:  # noqa: BLE001
            log.debug("get_context_window failed, using transcript: %s", e)
            # On window-resolution failure, default to transcript (safest: no info lost)

    if use_summary and not (source.summary or "").strip():
        log.warning(
            "claim extraction for %s: transcript exceeds context window but "
            "source.summary is empty — extracting nothing",
            getattr(source, "id", None),
        )

    user_msg = _user_message(source, use_summary=use_summary)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt(names)},
            {"role": "user", "content": user_msg},
        ],
        "api_key": api_key,
    }
    if base_url:
        kwargs["api_base"] = base_url

    try:
        response = await litellm.acompletion(**kwargs)
        raw = response.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001 — extraction is best-effort
        log.warning(
            "claim extraction LLM call failed for %s: %s: %s",
            getattr(source, "id", None), type(e).__name__, e,
        )
        return []

    blob = _extract_json_blob(raw)
    if blob is None:
        return []
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    items = payload.get("claims")
    if not isinstance(items, list):
        return []

    accepted: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("speaker")
        claim = item.get("claim")
        # Drop unattributable: null speaker, non-string speaker, empty claim,
        # or speaker not in the expected list.
        if not isinstance(name, str) or not isinstance(claim, str) or not claim.strip():
            continue
        sp = by_name.get(name.strip().lower())
        if sp is None:
            continue  # not one of the expected speakers → drop (spec rule #3)
        method = item.get("attribution_method")
        if method not in _ATTR_METHODS:
            method = None
        accepted.append({
            "speaker_id": sp.id,
            "claim": claim.strip(),
            "topic": item.get("topic") if isinstance(item.get("topic"), str) else None,
            "evidence_text": (
                item.get("evidence_text")
                if isinstance(item.get("evidence_text"), str) else None
            ),
            "evidence_start_s": _coerce_int(item.get("evidence_start_s")),
            "evidence_end_s": _coerce_int(item.get("evidence_end_s")),
            "text_start_offset": _coerce_int(item.get("text_start_offset")),
            "text_end_offset": _coerce_int(item.get("text_end_offset")),
            "confidence": _coerce_float(item.get("confidence")),
            "attribution_method": method,
            "attribution_confidence": _coerce_float(item.get("attribution_confidence")),
            "attribution_reason": (
                item.get("attribution_reason")
                if isinstance(item.get("attribution_reason"), str) else None
            ),
        })

    # Replace-on-reprocess: clear THIS source's rows for these speakers,
    # then insert fresh. Prevents duplicates on re-extraction.
    # commit=False on both delete and every insert; single commit after the
    # loop makes the whole replace+reinsert atomic — a crash before the commit
    # rolls back the DELETE too, leaving prior claims intact.
    from app.repos import speaker_claim_embeddings as _cve

    # Drop old claim vectors for this (speaker, source) pair before re-deriving.
    # Must happen before replace_for_source_speakers so the sub-SELECT inside
    # delete_for_source can still find the claim rows (they are deleted next).
    for sid in speaker_ids:
        await _cve.delete_for_source(db, sid, source.id)

    await claims_repo.replace_for_source_speakers(db, source.id, speaker_ids, commit=False)
    inserted: list[tuple[int, str]] = []   # (claim_id, claim_text)
    for c in accepted:
        claim_id = await claims_repo.insert_claim(
            db,
            user_id=source.user_id,
            source_id=source.id,
            speaker_id=c["speaker_id"],
            claim=c["claim"],
            topic=c["topic"],
            evidence_text=c["evidence_text"],
            evidence_start_s=c["evidence_start_s"],
            evidence_end_s=c["evidence_end_s"],
            text_start_offset=c["text_start_offset"],
            text_end_offset=c["text_end_offset"],
            confidence=c["confidence"],
            extraction_method="llm",
            attribution_method=c["attribution_method"],
            attribution_confidence=c["attribution_confidence"],
            attribution_reason=c["attribution_reason"],
            commit=False,
        )
        inserted.append((claim_id, c["claim"]))
    await db.commit()   # claims durably committed first — atomicity preserved

    # Best-effort embeddings AFTER the atomic commit.
    # A failure here degrades semantic ranking only; the recency/topic
    # fallback in retrieve_for_prompt still works.
    for claim_id, claim_text in inserted:
        await _embed_claim_best_effort(db, claim_id, claim_text)

    return accepted


async def _embed_claim_best_effort(db, claim_id: int, claim_text: str) -> None:
    """Embed one claim into speaker_claim_embeddings. Never raises — a failure
    only degrades retrieval ranking (the recency/topic fallback still works).
    Same posture as pipeline._try_embed_summary."""
    from app.repos import speaker_claim_embeddings as cve
    from app.services.embeddings_local import embed_text
    try:
        vector = await embed_text(claim_text)
        await cve.upsert_claim_embedding(db, claim_id, vector)
    except Exception as e:  # noqa: BLE001 — best-effort, must not break extraction
        log.warning(
            "claim embedding failed for claim %s: %s: %s", claim_id, type(e).__name__, e,
        )


# ---------------------------------------------------------------------------
# Retrieval — PR 4: embedding-ranked KNN with recency/topic fallback
# ---------------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")}


async def _embed_query(text: str) -> list[float]:
    """Embed a retrieval query using the SAME local embedder as T3 claim extraction.

    Must be a module-level function so the fallback test can monkeypatch it
    via ``speaker_claims._embed_query``.
    """
    from app.services.embeddings_local import embed_text
    return await embed_text(text)


async def _retrieve_recency(db, speaker_id: int, *, query: str, limit: int):
    """PR 3 recency + topic-text overlap path, returning raw aiosqlite Row objects.

    Returns rows with ``source_title`` JOIN alias + all speaker_claims columns +
    ``id``. Rows are subscriptable (``r["id"]``, ``r["claim"]``, etc.).
    """
    cur = await db.execute(
        "SELECT c.*, v.title AS source_title "
        "FROM speaker_claims c JOIN videos v ON v.id = c.source_id "
        "WHERE c.speaker_id=? AND c.review_status != 'rejected' "
        "ORDER BY c.created_at DESC, c.id DESC",
        (speaker_id,),
    )
    rows = await cur.fetchall()
    q = _tokens(query)

    def score(r) -> int:
        if not q:
            return 0
        hay = _tokens(f"{r['topic'] or ''} {r['claim']}")
        return len(q & hay)

    # Stable sort: overlap score desc, then the recency order from SQL.
    ranked = sorted(enumerate(rows), key=lambda iz: (-score(iz[1]), iz[0]))
    return [r for _i, r in ranked[:limit]]


async def _load_claims_by_id(db, claim_ids: list[int]) -> dict:
    """Load speaker_claims rows (with source_title JOIN) for the given ids.

    Returns ``{row["id"]: row}`` preserving the same column shape as
    ``_retrieve_recency`` so ``_claim_to_prompt_dict`` works on both.
    """
    if not claim_ids:
        return {}
    marks = ",".join("?" for _ in claim_ids)
    cur = await db.execute(
        f"SELECT c.*, v.title AS source_title "
        f"FROM speaker_claims c JOIN videos v ON v.id = c.source_id "
        f"WHERE c.id IN ({marks}) AND c.review_status != 'rejected'",
        claim_ids,
    )
    rows = await cur.fetchall()
    return {r["id"]: r for r in rows}


async def retrieve_for_prompt(
    db, speaker_id: int, *, query: str, limit: int = 12,
) -> list[dict]:
    """Cross-source claim slice for the persona prompt.

    PR 4 ranking: claims are KNN-ranked by embedding similarity to ``query``
    (using the same 384-d local embedder as T3 claim extraction). Falls back
    to the PR 3 recency/topic path when the embedder is unavailable or no
    claim vectors exist yet. Partial KNN hits are topped up from recency
    (de-duped by id) when fewer than ``limit`` vectors were found.

    PUBLIC SIGNATURE AND RETURN CONTRACT UNCHANGED:
    Returns list[dict] with exactly the 9 keys from ``_claim_to_prompt_dict``.
    No SpeakerClaim / aiosqlite.Row objects leak to callers.
    """
    from app.repos import speaker_claim_embeddings as cve
    try:
        qvec = await _embed_query(query)
        hits = await cve.search_claim_vectors(db, speaker_id, qvec, limit=limit)
    except Exception as e:  # noqa: BLE001 — degrade to recency, never raise
        log.warning(
            "claim KNN unavailable (%s: %s); using recency fallback",
            type(e).__name__, e,
        )
        hits = []
    if not hits:
        recency_rows = await _retrieve_recency(db, speaker_id, query=query, limit=limit)
        return [_claim_to_prompt_dict(r) for r in recency_rows]
    claim_ids = [cid for cid, _ in hits]
    by_id = await _load_claims_by_id(db, claim_ids)
    # Preserve KNN order by iterating claim_ids (closest-first from search_claim_vectors).
    ranked = [by_id[cid] for cid in claim_ids if cid in by_id]
    if len(ranked) < limit:
        # Top up from recency for claims that have no vector yet, de-duped by id.
        seen = {r["id"] for r in ranked}
        for r in await _retrieve_recency(db, speaker_id, query=query, limit=limit):
            if r["id"] not in seen:
                ranked.append(r)
                if len(ranked) >= limit:
                    break
    # Public contract is list[dict] — map every row, never leak Row objects.
    return [_claim_to_prompt_dict(r) for r in ranked[:limit]]


def _claim_to_prompt_dict(r) -> dict:
    """THE fixed-key contract for a retrieved claim.

    speaker_chat's prompt builder, the routes, the persona-turn tests, and
    PR 4's track-record peek + embedding-ranked retrieval all consume exactly
    these 9 keys. PR 4 reuses this function so the shape never drifts.

    The row `r` must expose `source_title` (the JOIN alias on videos.title)
    alongside the speaker_claims columns.
    """
    return {
        "claim": r["claim"],
        "topic": r["topic"],
        "evidence_text": r["evidence_text"],
        "evidence_start_s": r["evidence_start_s"],
        "source_id": r["source_id"],
        "source_title": r["source_title"],
        "attribution_method": r["attribution_method"],
        "attribution_confidence": r["attribution_confidence"],
        "review_status": r["review_status"],
    }
