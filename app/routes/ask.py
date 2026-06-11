"""Routes for "ask my library" (Part C.2).

Mirrors the digest flow: GET /ask shows the question box + archive, POST
/ask creates a pending synthesis and spawns the background job, then
redirects to the /ask/{id} permalink which HTMX-polls until ready.
"""

import asyncio
import logging

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.main import get_current_user_id, get_db
from app.models import Synthesis
from app.repos import syntheses as syntheses_repo
from app.repos import videos as videos_repo
from app.services import ask as ask_service
from app.services.markdown import render_markdown
from app.template_filters import register_filters

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

# Strong refs to in-flight jobs so the loop doesn't GC them mid-run
# (same pattern as routes/digest.py).
_PENDING_JOBS: set[asyncio.Task] = set()


async def _enqueue_ask_job(
    db: aiosqlite.Connection, *, user_id: int, query: str,
) -> Synthesis:
    """Create the pending row in the foreground (so the redirect target
    exists), then run the synthesis in the background. Monkeypatched in
    tests."""
    s = await syntheses_repo.create_pending(
        db, user_id=user_id, query=query, source_ids=[],
    )

    async def _run(synthesis_id: int) -> None:
        try:
            await ask_service.run(db, synthesis_id=synthesis_id, user_id=user_id)
        except Exception as e:
            log.exception("ask job crashed for user %s", user_id)
            # Safety net: run() marks its own failures, but if it raised
            # before reaching that point the row would be stuck on
            # 'pending' forever (the UI would poll "Synthesising…"
            # endlessly). Force it to 'failed' so the user sees an error.
            try:
                await syntheses_repo.mark_failed(
                    db, synthesis_id=synthesis_id,
                    error=f"{type(e).__name__}: {e}",
                )
            except Exception:
                log.exception(
                    "ask job: could not mark synthesis %s failed",
                    synthesis_id,
                )

    task = asyncio.create_task(_run(s.id))
    _PENDING_JOBS.add(task)
    task.add_done_callback(_PENDING_JOBS.discard)
    return s


@router.get("/ask", response_class=HTMLResponse)
async def ask_index(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    archive = await syntheses_repo.list_for_user(db, user_id=user_id, limit=30)
    return templates.TemplateResponse(
        request, "ask/index.html", {"archive": archive},
    )


@router.post("/ask")
async def ask_submit(
    query: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    q = query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Question is required")
    s = await _enqueue_ask_job(db, user_id=user_id, query=q)
    return RedirectResponse(url=f"/ask/{s.id}", status_code=303)


async def _fetch_for_user(
    db: aiosqlite.Connection, synthesis_id: int, user_id: int,
) -> Synthesis:
    s = await syntheses_repo.get(db, synthesis_id)
    if s is None or s.user_id != user_id:
        raise HTTPException(status_code=404)
    return s


async def _body_context(
    db: aiosqlite.Connection, s: Synthesis,
) -> dict:
    """Shared template context for the full page and the poll fragment:
    the rendered answer HTML and the ordered source Video rows."""
    result_html = render_markdown(s.result_md) if s.result_md else ""
    sources = []
    if s.status.value == "ready":
        import json
        try:
            ids = json.loads(s.source_ids_json)
        except (ValueError, TypeError):
            ids = []
        by_id = await videos_repo.get_many(db, ids)
        sources = [by_id[i] for i in ids if i in by_id]
    return {"synthesis": s, "result_html": result_html, "sources": sources}


@router.get("/ask/{synthesis_id}", response_class=HTMLResponse)
async def ask_show(
    request: Request,
    synthesis_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    s = await _fetch_for_user(db, synthesis_id, user_id)
    ctx = await _body_context(db, s)
    return templates.TemplateResponse(request, "ask/show.html", ctx)


@router.get("/ask/{synthesis_id}/fragment", response_class=HTMLResponse)
async def ask_fragment(
    request: Request,
    synthesis_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    """Fragment endpoint for the HTMX poll while a synthesis runs.

    Returns ONLY the inner body (pending spinner / failed / answer +
    sources) — never the base layout, so hx-swap=outerHTML doesn't nest
    the whole page into itself on every tick. On reaching a terminal
    state, sends HX-Refresh so the browser reloads /ask/<id> once and the
    surrounding chrome stays in sync. Mirrors digest_body_fragment."""
    s = await _fetch_for_user(db, synthesis_id, user_id)
    is_htmx_poll = request.headers.get("HX-Request") == "true"
    if is_htmx_poll and s.status.value in ("ready", "failed"):
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    ctx = await _body_context(db, s)
    return templates.TemplateResponse(request, "ask/_body.html", ctx)
