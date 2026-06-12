"""Build a daily digest for one Profile.

Pool = all videos owned by the Profile whose `highlights_json` is set
and non-empty within the requested window. The LLM picks the Top-N
and writes a TL;DR. Result stored as a `digests` row.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite
import litellm

from app.models import Digest
from app.repos import digests as digests_repo
from app.repos import llm_models as llm_models_repo
from app.repos import users as users_repo

log = logging.getLogger(__name__)

_TOP_N = 10
_EMPTY_POOL_TLDR = (
    "Nothing noteworthy in the last "
    "{hours} hours — your queue is quiet."
)

# Hard cap on how far back a digest window may reach when the last
# digest is old or missing (spec: fixed 4 days, no setting).
WINDOW_CAP_HOURS = 96


async def compute_window(
    db: aiosqlite.Connection, *, user_id: int, now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Candidate window for the next digest of this Profile.

    Starts where the last non-failed digest ended, but never more than
    WINDOW_CAP_HOURS back. `now` is injectable for tests.
    """
    period_end = (now or datetime.now(UTC)).replace(microsecond=0)
    floor = period_end - timedelta(hours=WINDOW_CAP_HOURS)
    last_end = await digests_repo.latest_period_end(db, user_id=user_id)
    period_start = floor if last_end is None else max(last_end, floor)
    return period_start, period_end


_DIGEST_SYSTEM = """\
You curate a daily digest for one Profile of yt-summary.

You receive:
- the Profile's interest profile (their stated interests, may be empty)
- a list of items the Profile saved in the window, each with a small
  set of pre-extracted noteworthy highlights and metadata.

Your job:
1. Write a 3-5 sentence TL;DR that names the thematic clusters of the
   day. Concrete details over abstractions.
2. Pick the Top-N items (N up to 10; fewer if the pool is smaller).
3. For each picked item, write a 1-2 sentence hook and a one-sentence
   "why this matters for this Profile" reason.

Use the interest profile to bias what counts as "top". An item that
matches the Profile's interests outranks a generally-popular topic the
Profile doesn't care about.

Output ONLY a JSON object of this exact shape:

{
  "tldr": "<3-5 sentences>",
  "top_items": [
    {"video_id": "<exact id from the input>",
     "rank": <int>,
     "hook": "<1-2 sentences>",
     "reason": "<1 sentence>"},
    ...
  ]
}

If the pool is empty or you cannot pick any worthwhile items, return
an empty "top_items" list and a TL;DR that says so honestly. Never
invent video_ids.
"""


async def _gather_pool(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    period_start: datetime,
    video_ids: list[str] | None = None,
) -> list[dict]:
    """Return JSON-ready item dicts for the digest prompt.

    `video_ids` restricts the pool to a hand-picked selection (manual
    digests); None means everything in the window (automatic digests).
    The highlights gate applies either way.

    Uses SQLite's datetime() on both sides of the timestamp comparison
    to normalize the space-vs-T separator mismatch between SQLite's
    column-default datetime('now') and Python's datetime.isoformat().
    """
    if video_ids is not None and not video_ids:
        return []
    params: list = [user_id, period_start.isoformat()]
    id_clause = ""
    if video_ids is not None:
        placeholders = ",".join("?" for _ in video_ids)
        id_clause = f" AND id IN ({placeholders})"
        params.extend(video_ids)
    cur = await db.execute(
        f"""
        SELECT id, title, kind, url, highlights_json
        FROM videos
        WHERE user_id = ?
          AND datetime(created_at) >= datetime(?)
          AND highlights_json IS NOT NULL
          AND highlights_json != '[]'
          {id_clause}
        """,
        params,
    )
    rows = await cur.fetchall()
    items: list[dict] = []
    for r in rows:
        try:
            highlights = json.loads(r["highlights_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(highlights, list) or not highlights:
            continue
        items.append({
            "video_id": r["id"],
            "title": r["title"],
            "source_type": r["kind"],
            "url": r["url"],
            "highlights": highlights,
        })
    return items


async def list_candidates(
    db: aiosqlite.Connection, *, user_id: int, period_start: datetime,
) -> tuple[list[dict], int]:
    """Candidates for the /digest/new selection page.

    Returns (eligible, missing_highlights_count): eligible items have
    highlights and can be picked; the count covers in-window videos
    without highlights, surfaced as a footnote so the user understands
    why they are absent.
    """
    cur = await db.execute(
        """
        SELECT id, title, kind, created_at,
               (highlights_json IS NOT NULL AND highlights_json != '[]')
                   AS has_highlights
        FROM videos
        WHERE user_id = ?
          AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) DESC
        """,
        (user_id, period_start.isoformat()),
    )
    rows = await cur.fetchall()
    eligible: list[dict] = []
    missing = 0
    for r in rows:
        if not r["has_highlights"]:
            missing += 1
            continue
        eligible.append({
            "id": r["id"],
            "title": r["title"],
            "kind": r["kind"],
            "created_at": r["created_at"],
        })
    return eligible, missing


async def _call_digest_llm(
    *,
    payload: str,
    interest_profile_md: str,
    model: str,
    api_key: str,
    base_url: str | None,
) -> str:
    user_msg = (
        f"INTEREST PROFILE:\n{interest_profile_md or '(none yet)'}\n\n"
        f"ITEMS (JSON):\n{payload}\n\n"
        "Produce the digest JSON now."
    )
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _DIGEST_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "api_key": api_key,
    }
    if base_url:
        kwargs["api_base"] = base_url
    response = await litellm.acompletion(**kwargs)
    return response.choices[0].message.content or ""


def _parse_digest_response(
    raw: str, *, allowed_ids: set[str],
) -> tuple[str, list[dict]]:
    """Strict parse of the digest LLM output.

    Raises ValueError when not parseable. Filters hallucinated video_ids.
    """
    blob_start = raw.find("{")
    blob_end = raw.rfind("}")
    if blob_start == -1 or blob_end == -1:
        raise ValueError("no JSON object in response")
    obj = json.loads(raw[blob_start:blob_end + 1])
    if not isinstance(obj, dict):
        raise ValueError("response not a JSON object")
    tldr = obj.get("tldr")
    items = obj.get("top_items")
    if not isinstance(tldr, str) or not isinstance(items, list):
        raise ValueError("response missing tldr or top_items")
    kept: list[dict] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        vid = entry.get("video_id")
        if vid not in allowed_ids:
            continue
        kept.append({
            "video_id": vid,
            "rank": int(entry.get("rank", len(kept) + 1)),
            "hook": str(entry.get("hook", "")),
            "reason": str(entry.get("reason", "")),
        })
    return (tldr, kept)


async def generate(
    db: aiosqlite.Connection, *, user_id: int,
) -> Digest:
    """Build an automatic digest for the Profile.

    Window = since the last non-failed digest, capped at
    WINDOW_CAP_HOURS. Takes every candidate (no selection). Used by
    the cron sweep. The on-demand HTTP path creates the row in the
    handler so it can redirect to `/digest/<id>` immediately, then
    calls `run_for_existing_digest` to finish in the background.
    """
    period_start, period_end = await compute_window(db, user_id=user_id)

    d = await digests_repo.create_pending(
        db, user_id=user_id, period_start=period_start, period_end=period_end,
    )
    return await _run(
        db, digest_id=d.id, user_id=user_id,
        period_start=period_start, period_end=period_end,
        selected_video_ids=None,
    )


async def run_for_existing_digest(
    db: aiosqlite.Connection,
    *,
    digest_id: int,
    user_id: int,
) -> Digest:
    """Run the job for a digest row that's already been inserted as
    pending. Window and (optional) hand-picked selection come from the
    row itself — the route handler persisted both."""
    d = await digests_repo.get(db, digest_id)
    assert d is not None
    selected: list[str] | None = None
    if d.selected_video_ids_json:
        selected = json.loads(d.selected_video_ids_json)
    return await _run(
        db, digest_id=digest_id, user_id=user_id,
        period_start=d.period_start, period_end=d.period_end,
        selected_video_ids=selected,
    )


async def _run(
    db: aiosqlite.Connection,
    *,
    digest_id: int,
    user_id: int,
    period_start: datetime,
    period_end: datetime,
    selected_video_ids: list[str] | None,
) -> Digest:
    """Shared work loop. The row at `digest_id` must already exist."""
    await digests_repo.mark_rendering(db, digest_id=digest_id)

    period_hours = max(1, int((period_end - period_start).total_seconds() // 3600))
    pool = await _gather_pool(
        db, user_id=user_id, period_start=period_start,
        video_ids=selected_video_ids,
    )
    if not pool:
        await digests_repo.mark_ready(
            db, digest_id=digest_id,
            tldr=_EMPTY_POOL_TLDR.format(hours=period_hours),
            top_items_json="[]", item_count=0,
        )
        refreshed = await digests_repo.get(db, digest_id)
        assert refreshed is not None
        return refreshed

    profile_md, _ = await users_repo.get_interest_profile(db, user_id=user_id)
    model_row = await llm_models_repo.get_default(db)
    if model_row is None:
        await digests_repo.mark_failed(
            db, digest_id=digest_id, error="No default LLM configured",
        )
        refreshed = await digests_repo.get(db, digest_id)
        assert refreshed is not None
        return refreshed

    payload = json.dumps(pool, ensure_ascii=False)
    allowed_ids = {item["video_id"] for item in pool}

    try:
        raw = await _call_digest_llm(
            payload=payload,
            interest_profile_md=profile_md or "",
            model=model_row.model,
            api_key=model_row.api_key,
            base_url=model_row.base_url or None,
        )
        tldr, kept = _parse_digest_response(raw, allowed_ids=allowed_ids)
    except Exception as exc:
        log.exception("digest: generation failed for user %s", user_id)
        await digests_repo.mark_failed(
            db, digest_id=digest_id, error=str(exc) or "LLM call failed",
        )
        refreshed = await digests_repo.get(db, digest_id)
        assert refreshed is not None
        return refreshed

    kept = kept[:_TOP_N]
    await digests_repo.mark_ready(
        db, digest_id=digest_id,
        tldr=tldr,
        top_items_json=json.dumps(kept, ensure_ascii=False),
        item_count=len(pool),
    )
    refreshed = await digests_repo.get(db, digest_id)
    assert refreshed is not None
    return refreshed
