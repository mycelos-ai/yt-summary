"""Per-profile processing view.

``GET /processing`` shows what the summary worker is doing with the
active profile's videos: the running job, the waiting ones with their
place in the shared FIFO, and recent failures with Retry / Dismiss.
``GET /processing/fragment`` serves the HTMX-polled body of that page
(``view=page``) and the compact home-page strip (``view=strip``).

The operator-level view (all profiles, TTS queue, heartbeats, log
tail) stays at ``/settings/diagnostics``. This page only ever reads
or mutates jobs whose video belongs to the active profile — the repo
helpers carry that predicate in every statement.
"""
from datetime import UTC, datetime

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.main import get_current_user, get_current_user_id, get_db
from app.models import JobState
from app.repos import jobs as jobs_repo
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

_FRAGMENTS = {
    "page": "_processing_body.html",
    "strip": "_processing_strip.html",
}


async def load_processing_context(
    db: aiosqlite.Connection, user_id: int,
) -> dict[str, object]:
    """Everything the strip and the page need, in one dict under the
    ``processing`` template key. Shared with the home route so the
    strip renders inline on first paint instead of after a poll."""
    overview = await jobs_repo.overview_for_user(db, user_id)
    active = await jobs_repo.list_active_for_user(db, user_id)
    failed = await jobs_repo.list_failed_for_user(db, user_id)
    running = [e for e in active if e.job.state is JobState.RUNNING]
    waiting = [e for e in active if e.job.state is JobState.PENDING]
    return {
        "overview": overview,
        "running": running,
        "waiting": waiting,
        "failed": failed,
        "is_active": bool(running or waiting),
        # Naive UTC to match SQLite's datetime('now') on job rows.
        "now": datetime.now(UTC).replace(tzinfo=None),
    }


@router.get("/processing", response_class=HTMLResponse)
async def processing_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    processing = await load_processing_context(db, current_user_id)
    return templates.TemplateResponse(
        request,
        "processing.html",
        {"processing": processing, "current_user": current_user},
    )


@router.get("/processing/fragment", response_class=HTMLResponse)
async def processing_fragment(
    request: Request,
    view: str = "page",
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    template = _FRAGMENTS.get(view)
    if template is None:
        raise HTTPException(400, detail="view must be 'page' or 'strip'")
    processing = await load_processing_context(db, current_user_id)
    return templates.TemplateResponse(
        request,
        template,
        {"processing": processing, "current_user": current_user},
    )


async def _after_action(
    request: Request,
    db: aiosqlite.Connection,
    current_user_id: int,
    current_user,
):
    """HTMX callers get the refreshed page body swapped in place;
    plain form posts bounce back to the page."""
    if request.headers.get("HX-Request"):
        processing = await load_processing_context(db, current_user_id)
        return templates.TemplateResponse(
            request,
            "_processing_body.html",
            {"processing": processing, "current_user": current_user},
        )
    return RedirectResponse("/processing", status_code=303)


async def _explain_zero_rows(
    db: aiosqlite.Connection, job_id: int, user_id: int, *, wanted: str,
) -> HTTPException:
    """An owner-scoped mutation touched nothing. Foreign or missing
    ids are indistinguishable on purpose (404); an own job in the
    wrong state is a 409 the UI can explain."""
    job = await jobs_repo.get_for_user(db, job_id, user_id)
    if job is None:
        return HTTPException(404, detail=f"No job {job_id}")
    return HTTPException(
        409,
        detail=f"Job {job_id} is {job.state.value}; only {wanted} jobs "
               f"can be changed here",
    )


@router.post("/processing/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    """Drop one of the active profile's pending jobs. Running jobs
    cannot be cancelled (409); the pipeline has no abort point."""
    n = await jobs_repo.cancel_pending_for_user(
        db, job_id, user_id=current_user_id,
    )
    if n == 0:
        raise await _explain_zero_rows(
            db, job_id, current_user_id, wanted="pending",
        )
    return await _after_action(request, db, current_user_id, current_user)


@router.post("/processing/jobs/{job_id}/retry")
async def retry_job(
    job_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    n = await jobs_repo.retry_for_user(db, job_id, user_id=current_user_id)
    if n == 0:
        raise await _explain_zero_rows(
            db, job_id, current_user_id, wanted="failed",
        )
    return await _after_action(request, db, current_user_id, current_user)


@router.post("/processing/jobs/{job_id}/dismiss")
async def dismiss_job(
    job_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    n = await jobs_repo.dismiss_failed_for_user(
        db, job_id, user_id=current_user_id,
    )
    if n == 0:
        raise await _explain_zero_rows(
            db, job_id, current_user_id, wanted="failed",
        )
    return await _after_action(request, db, current_user_id, current_user)
