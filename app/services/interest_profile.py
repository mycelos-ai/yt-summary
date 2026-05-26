"""Maintain a per-Profile interest profile destilled from feedback.

The profile is a short Markdown document the LLM updates from
`feedback` rows. Read at summarize-time and digest-time as prompt
context.

Concurrency: writes use the optimistic-lock helper in `users_repo`,
which prevents two consolidate runs from clobbering each other. A
write conflict logs a warning and is swallowed — the loser's signal
gets folded in on the next consolidate.
"""
from __future__ import annotations

import logging

import aiosqlite
import litellm

from app.repos import feedback as feedback_repo
from app.repos import llm_models as llm_models_repo
from app.repos import users as users_repo

log = logging.getLogger(__name__)

_MAX_PROFILE_CHARS = 8000  # ~2000 tokens, soft limit enforced in prompt


_CONSOLIDATE_SYSTEM = """\
You maintain a short Markdown "interest profile" for one Profile of
yt-summary. Goal: capture what they care about so future video and
article summaries can be shaped to their interests.

Rules:
- Keep it under ~2000 tokens of markdown. Merge duplicates ruthlessly.
- Use bullet lists. Group related interests under short headings if
  natural ("LLM tooling", "Hardware", "Stories I followed", ...).
- Keep both "interested in" and "explicitly not interested in" — both
  are useful signals.
- Be concrete: "cares about LLM caching cost reductions" beats "cares
  about AI".
- Preserve the existing profile content unless new feedback contradicts
  it. New feedback ADDS or REFINES; it does not erase past structure.

Return ONLY the updated markdown profile. No commentary, no JSON.
"""


async def _call_consolidate_llm(
    *,
    current_profile: str,
    feedback_lines: str,
    model: str,
    api_key: str,
    base_url: str | None,
) -> str:
    user_msg = (
        f"CURRENT PROFILE:\n{current_profile or '(empty)'}\n\n"
        f"NEW FEEDBACK EVENTS:\n{feedback_lines}\n\n"
        "Produce the updated profile now."
    )
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CONSOLIDATE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "api_key": api_key,
    }
    if base_url:
        kwargs["api_base"] = base_url
    response = await litellm.acompletion(**kwargs)
    return response.choices[0].message.content or ""


async def consolidate(
    db: aiosqlite.Connection, *, user_id: int, limit: int = 50,
) -> None:
    """Fold the latest ``limit`` feedback rows into the Profile's
    interest profile via one LLM call. No-op if no feedback exists.
    Failure is logged and swallowed (profile stays as-is)."""
    fb_rows = await feedback_repo.list_recent_for_user(
        db, user_id=user_id, limit=limit,
    )
    if not fb_rows:
        return

    current_md, version = await users_repo.get_interest_profile(
        db, user_id=user_id,
    )
    feedback_lines = "\n".join(
        f"- [{fb.sentiment.value}] "
        f"\"{fb.selected_text}\""
        + (f" (comment: {fb.comment})" if fb.comment else "")
        for fb in fb_rows
    )

    model_row = await llm_models_repo.get_default(db)
    if model_row is None:
        log.warning("interest_profile: no default LLM configured; skip")
        return

    try:
        updated = await _call_consolidate_llm(
            current_profile=current_md or "",
            feedback_lines=feedback_lines,
            model=model_row.model,
            api_key=model_row.api_key,
            base_url=model_row.base_url or None,
        )
    except Exception:
        log.exception("interest_profile: consolidate LLM call failed")
        return

    updated = (updated or "").strip()[:_MAX_PROFILE_CHARS]

    ok = await users_repo.set_interest_profile(
        db, user_id=user_id, markdown=updated, expected_version=version,
    )
    if not ok:
        log.warning(
            "interest_profile: optimistic-lock conflict for user %s "
            "(another writer raced us); skipping this update", user_id,
        )


async def rebuild(db: aiosqlite.Connection, *, user_id: int) -> None:
    """Wipe the profile and redistill it from every feedback row for
    this Profile. Used by the "Rebuild from feedback" button."""
    _, version = await users_repo.get_interest_profile(db, user_id=user_id)
    # Set to empty before consolidating so the consolidate prompt starts
    # from a clean slate.
    await users_repo.set_interest_profile(
        db, user_id=user_id, markdown="", expected_version=version,
    )
    fb_rows = await feedback_repo.list_recent_for_user(
        db, user_id=user_id, limit=10_000,
    )
    if not fb_rows:
        return

    model_row = await llm_models_repo.get_default(db)
    if model_row is None:
        log.warning("interest_profile: no default LLM configured; rebuild skipped")
        return

    feedback_lines = "\n".join(
        f"- [{fb.sentiment.value}] \"{fb.selected_text}\""
        + (f" (comment: {fb.comment})" if fb.comment else "")
        for fb in fb_rows
    )

    try:
        updated = await _call_consolidate_llm(
            current_profile="",
            feedback_lines=feedback_lines,
            model=model_row.model,
            api_key=model_row.api_key,
            base_url=model_row.base_url or None,
        )
    except Exception:
        log.exception("interest_profile: rebuild LLM call failed")
        return

    updated = (updated or "").strip()[:_MAX_PROFILE_CHARS]
    _, new_version = await users_repo.get_interest_profile(db, user_id=user_id)
    await users_repo.set_interest_profile(
        db, user_id=user_id, markdown=updated, expected_version=new_version,
    )
