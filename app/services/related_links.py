"""Curated related-summaries computation (block-at-end feature).

Two-stage hybrid: the existing 384-d KNN (`related_video_ids`) pre-filters
candidates, then ONE LLM call picks the genuinely relevant ones and gives a
one-line reason. Validated against the candidate set (anti-hallucination).

Called once from the pipeline after the summary is embedded. The pipeline
wraps the call in try/except — failures here leave related_links_json NULL,
and the detail page falls back to the live-KNN strip.
"""
from __future__ import annotations

import json
import logging

import aiosqlite
import litellm

from app.models import Video
from app.repos import videos as videos_repo
from app.services import related as related_svc
from app.services.highlight_parser import _extract_json_blob

log = logging.getLogger(__name__)

_MAX_CONTEXT_CHARS = 500

_SELECT_PROMPT = """\
You are linking one summary to other summaries in the same personal library.

THIS summary's title: {subject_title}

Candidate summaries (each has an id, title, and a short context):
{candidates}

Pick ONLY the candidates that a reader of THIS summary would genuinely
benefit from following — same topic, direct follow-up, opposing view, or
shared key entity. Linking nothing is fine.

Return a single JSON object, no prose, with this exact shape:

{{"links": [{{"video_id": "<id from the list above>",
             "reason": "<one short sentence on why it's related>"}}]}}
"""


def _candidate_context(cand: Video) -> str:
    """Compact context for one candidate: highlights if present, else a
    truncated summary."""
    if cand.highlights_json:
        try:
            hl = json.loads(cand.highlights_json)
            texts = [h.get("text", "") for h in hl if isinstance(h, dict)]
            joined = "; ".join(t for t in texts if t)
            if joined:
                return joined[:_MAX_CONTEXT_CHARS]
        except (json.JSONDecodeError, TypeError):
            pass
    return (cand.summary or "")[:_MAX_CONTEXT_CHARS]


async def _llm_select(
    *, prompt: str, model_row,
) -> str:
    """One self-contained completion (mirrors stock_images)."""
    kwargs: dict = {
        "model": model_row.model,
        "messages": [{"role": "user", "content": prompt}],
        "api_key": model_row.api_key,
    }
    if model_row.base_url:
        kwargs["api_base"] = model_row.base_url
    resp = await litellm.acompletion(**kwargs)
    return resp.choices[0].message.content or ""


async def compute_related_links(
    db: aiosqlite.Connection,
    *,
    video: Video,
    user_id: int,
    model_row,
    candidate_limit: int = 10,
) -> list[dict]:
    """Curated related links for `video`. Returns a list of
    {video_id, title, reason}; possibly empty.

    Short-circuits to [] (no LLM call) when there are no KNN candidates or
    no usable model. Otherwise raises on LLM / JSON failure — the pipeline
    boundary is responsible for swallowing those.
    """
    if model_row is None:
        return []
    candidate_ids = await related_svc.related_video_ids(
        db, video, user_id=user_id, limit=candidate_limit,
    )
    if not candidate_ids:
        return []
    cands = await videos_repo.get_many(db, candidate_ids)
    # preserve KNN order, keep only ids we actually loaded
    ordered = [cands[i] for i in candidate_ids if i in cands]
    if not ordered:
        return []

    by_id = {c.id: c for c in ordered}
    block = "\n".join(
        f"- id={c.id} | title={c.title} | context={_candidate_context(c)}"
        for c in ordered
    )
    prompt = _SELECT_PROMPT.format(
        subject_title=video.title, candidates=block,
    )
    raw = await _llm_select(prompt=prompt, model_row=model_row)

    blob = _extract_json_blob(raw)
    if blob is None:
        raise ValueError("related-links LLM returned no JSON object")
    payload = json.loads(blob)  # raises JSONDecodeError on bad JSON
    links_raw = payload.get("links") if isinstance(payload, dict) else None
    if not isinstance(links_raw, list):
        raise ValueError("related-links JSON missing 'links' list")

    out: list[dict] = []
    seen: set[str] = set()
    for entry in links_raw:
        if not isinstance(entry, dict):
            continue
        vid = entry.get("video_id")
        reason = entry.get("reason", "")
        # anti-hallucination: id must be a real candidate
        if vid not in by_id or vid in seen:
            continue
        if not isinstance(reason, str):
            reason = ""
        seen.add(vid)
        out.append({
            "video_id": vid,
            "title": by_id[vid].title,  # trusted title, never the LLM's
            "reason": reason.strip(),
        })
    return out
