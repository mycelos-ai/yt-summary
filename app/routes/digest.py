"""Digest endpoints.

GET    /digest                  list view (latest + archive)
GET    /digest/<id>             single digest view, HTMX-pollable
POST   /digest/generate         enqueue an on-demand digest job
"""
from __future__ import annotations

import asyncio
import json
import logging
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
    db: aiosqlite.Connection, *, user_id: int, period_hours: int,
) -> Digest:
    """Spawn the digest job. Pre-creates the pending row in the
    foreground so the redirect to `/digest/<id>` has a valid target
    before the background generation finishes. Tests monkeypatch this
    whole function.
    """
    from datetime import UTC, datetime, timedelta
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(hours=period_hours)
    d = await digests_repo.create_pending(
        db, user_id=user_id, period_start=start, period_end=end,
    )

    async def _run(digest_id: int) -> None:
        try:
            await digest_service.run_for_existing_digest(
                db, digest_id=digest_id, user_id=user_id,
                period_hours=period_hours,
            )
        except Exception:
            log.exception(
                "on-demand digest job crashed for user %s", user_id,
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


@router.get("/digest/{digest_id}", response_class=HTMLResponse)
async def digest_show(
    request: Request,
    digest_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    d, referenced = await _fetch_digest_for_user(db, digest_id, user_id)
    return templates.TemplateResponse(
        request,
        "digest/show.html",
        {"digest": d, "videos": referenced},
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
    period_hours: int = Form(default=24),
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    if period_hours < 1 or period_hours > 24 * 30:
        raise HTTPException(status_code=422, detail="invalid period_hours")
    d = await _enqueue_digest_job(
        db, user_id=user_id, period_hours=period_hours,
    )
    return RedirectResponse(url=f"/digest/{d.id}", status_code=303)
