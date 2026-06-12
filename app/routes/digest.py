"""Digest endpoints.

GET    /digest                  list view (latest + archive)
GET    /digest/new              candidate-selection page
GET    /digest/<id>             single digest view, HTMX-pollable
POST   /digest/generate         enqueue an on-demand digest job
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.main import get_current_user_id, get_db
from app.models import Digest
from app.repos import digests as digests_repo
from app.repos import videos as videos_repo
from app.services import digest as digest_service
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

log = logging.getLogger(__name__)

# Strong references to in-flight digest tasks. asyncio.create_task only
# weak-references the resulting Task; without a strong ref the loop can
# GC it before it runs. Tasks self-discard via add_done_callback below.
_PENDING_JOBS: set[asyncio.Task] = set()


async def _enqueue_digest_job(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    period_start: datetime,
    period_end: datetime,
    video_ids: list[str] | None,
) -> Digest:
    """Spawn the digest job. Pre-creates the pending row in the
    foreground so the redirect to `/digest/<id>` has a valid target
    before the background generation finishes. `video_ids` is the
    hand-picked selection (None = automatic). Tests monkeypatch this
    whole function.
    """
    d = await digests_repo.create_pending(
        db, user_id=user_id, period_start=period_start,
        period_end=period_end,
        selected_video_ids_json=(
            json.dumps(video_ids) if video_ids is not None else None
        ),
    )

    async def _run(digest_id: int) -> None:
        try:
            await digest_service.run_for_existing_digest(
                db, digest_id=digest_id, user_id=user_id,
            )
        except Exception as e:
            log.exception(
                "on-demand digest job crashed for user %s", user_id,
            )
            # Safety net: don't leave the row stuck pending/rendering if
            # the job raised before marking its own failure.
            try:
                await digests_repo.mark_failed(
                    db, digest_id=digest_id,
                    error=f"{type(e).__name__}: {e}",
                )
            except Exception:
                log.exception(
                    "digest job: could not mark digest %s failed", digest_id,
                )

    task = asyncio.create_task(_run(d.id))
    _PENDING_JOBS.add(task)
    task.add_done_callback(_PENDING_JOBS.discard)
    return d


@router.get("/digest", response_class=HTMLResponse)
async def digest_index(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    digests = await digests_repo.list_for_user(db, user_id=user_id, limit=30)
    return templates.TemplateResponse(
        request,
        "digest/list.html",
        {"digests": digests},
    )


async def _fetch_digest_for_user(
    db: aiosqlite.Connection, digest_id: int, user_id: int,
) -> tuple[Digest, dict[str, Any]]:
    """Load a digest scoped to the active Profile, plus the referenced
    video metadata. Raises 404 for missing or cross-Profile."""
    d = await digests_repo.get(db, digest_id)
    if d is None or d.user_id != user_id:
        raise HTTPException(status_code=404)
    referenced: dict[str, Any] = {}
    if d.top_items_json:
        try:
            entries = json.loads(d.top_items_json)
        except json.JSONDecodeError:
            entries = []
        for e in entries:
            vid = e.get("video_id")
            if vid:
                referenced[vid] = await videos_repo.get(db, vid)
    return d, referenced


async def _existing_feedback_for_digest(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    digest_id: int,
    video_ids: list[str],
) -> list[dict]:
    """Collect this Profile's digest feedback rows so the highlight.js
    restore-pass can re-mark them on page load. Returned as a plain list;
    the template serializes it with `| tojson` (which escapes `</script>`
    so prompt-injected selected_text can't break out of the script block).

    Two kinds of feedback live on a digest page:
    - source-item feedback (anchored to a video_id) — for hooks /
      reasons in the Sources list. We include only rows whose
      source='digest' and whose video_id appears in this digest.
    - TL;DR feedback (anchored to this digest_id) — for the LLM-
      synthesised TL;DR block. source='digest_tldr'.

    The JS distinguishes them by which of (video_id, digest_id) is set.
    """
    from app.repos import feedback as feedback_repo

    rows: list[dict] = []
    for vid in video_ids:
        fbs = await feedback_repo.list_for_video(
            db, video_id=vid, user_id=user_id,
        )
        for fb in fbs:
            if fb.source.value != "digest":
                continue
            rows.append({
                "id": fb.id,
                "video_id": fb.video_id,
                "digest_id": None,
                "selected_text": fb.selected_text,
                "text_offset_start": fb.text_offset_start,
                "text_offset_end": fb.text_offset_end,
                "sentiment": fb.sentiment.value,
                "comment": fb.comment,
            })
    for fb in await feedback_repo.list_for_digest(
        db, digest_id=digest_id, user_id=user_id,
    ):
        rows.append({
            "id": fb.id,
            "video_id": None,
            "digest_id": fb.digest_id,
            "selected_text": fb.selected_text,
            "text_offset_start": fb.text_offset_start,
            "text_offset_end": fb.text_offset_end,
            "sentiment": fb.sentiment.value,
            "comment": fb.comment,
        })
    return rows


@router.get("/digest/new", response_class=HTMLResponse)
async def digest_new(
    request: Request,
    error: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    """Candidate-selection page for an on-demand digest. Window =
    since the last non-failed digest, capped at 96 h."""
    period_start, period_end = await digest_service.compute_window(
        db, user_id=user_id,
    )
    candidates, missing = await digest_service.list_candidates(
        db, user_id=user_id, period_start=period_start,
    )
    since_last = period_start > period_end - timedelta(
        hours=digest_service.WINDOW_CAP_HOURS,
    )
    return templates.TemplateResponse(
        request,
        "digest/new.html",
        {
            "candidates": candidates,
            "missing_highlights_count": missing,
            "period_start": period_start,
            "period_end": period_end,
            "since_last_digest": since_last,
            "error": error,
        },
    )


@router.get("/digest/{digest_id}", response_class=HTMLResponse)
async def digest_show(
    request: Request,
    digest_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    d, referenced = await _fetch_digest_for_user(db, digest_id, user_id)
    feedbacks_data = await _existing_feedback_for_digest(
        db, user_id=user_id,
        digest_id=digest_id,
        video_ids=list(referenced.keys()),
    )
    return templates.TemplateResponse(
        request,
        "digest/show.html",
        {
            "digest": d,
            "videos": referenced,
            "feedbacks_data": feedbacks_data,
        },
    )


@router.get(
    "/digest/{digest_id}/body-fragment", response_class=HTMLResponse,
)
async def digest_body_fragment(
    request: Request,
    digest_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    """Fragment endpoint for the HTMX poll while a digest is rendering.

    Returns ONLY the inner body (pending spinner, failed retry, or the
    final TL;DR + sources) — never wrapped in the base layout. Once the
    digest reaches a terminal state, sends `HX-Refresh: true` so the
    browser does a full reload of `/digest/<id>` (so the surrounding
    chrome stays in sync), matching the pattern used by
    /v/<id>/summary-fragment.
    """
    d, referenced = await _fetch_digest_for_user(db, digest_id, user_id)
    is_htmx_poll = request.headers.get("HX-Request") == "true"
    if is_htmx_poll and d.status.value in ("ready", "failed"):
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(
        request,
        "digest/_body.html",
        {"digest": d, "videos": referenced},
    )


@router.post("/digest/generate")
async def digest_generate(
    video_ids: list[str] = Form(default=[]),
    period_end: str | None = Form(default=None),
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    period_start, window_end = await digest_service.compute_window(
        db, user_id=user_id,
    )
    # The selection page stamps its render-time window end into the
    # form. Honour it (clamped to "not in the future") so items that
    # arrived after the page was rendered stay out of this digest's
    # window and surface as candidates for the next one (spec §5).
    effective_end = window_end
    if period_end:
        try:
            posted = datetime.fromisoformat(period_end)
        except ValueError:
            posted = None
        if posted is not None:
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=UTC)
            effective_end = min(posted, window_end)
    candidates, _ = await digest_service.list_candidates(
        db, user_id=user_id, period_start=period_start,
        period_end=effective_end,
    )
    allowed = {c["id"] for c in candidates}
    chosen = [v for v in video_ids if v in allowed]
    if not chosen:
        return RedirectResponse(
            url="/digest/new?error=no-selection", status_code=303,
        )
    d = await _enqueue_digest_job(
        db, user_id=user_id,
        period_start=period_start, period_end=effective_end,
        video_ids=chosen,
    )
    return RedirectResponse(url=f"/digest/{d.id}", status_code=303)
