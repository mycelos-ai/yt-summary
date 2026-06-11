"""Routes for "ask my library" (Part C.2).

Mirrors the digest flow: GET /ask shows the question box + archive, POST
/ask creates a thread and spawns the background job, then redirects to
the /ask/{id} permalink which HTMX-polls until ready.  Follow-up
questions are posted to /ask/{id}/followup.
"""

import asyncio
import json
import logging

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.main import get_current_user_id, get_db
from app.models import Synthesis
from app.repos import syntheses as syntheses_repo
from app.repos import synthesis_messages as sm_repo
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


async def _spawn_answer(db: aiosqlite.Connection, *, message_id: int, user_id: int):
    async def _run(mid):
        try:
            await ask_service.run_message(db, message_id=mid)
        except Exception as e:
            log.exception("ask job crashed for user %s", user_id)
            try:
                await sm_repo.mark_failed(db, message_id=mid,
                                          error=f"{type(e).__name__}: {e}")
            except Exception:
                log.exception(
                    "ask job: could not mark message %s failed", mid,
                )
    task = asyncio.create_task(_run(message_id))
    _PENDING_JOBS.add(task)
    task.add_done_callback(_PENDING_JOBS.discard)


async def _enqueue_first(db, *, user_id: int, query: str) -> int:
    """Create the thread (synthesis + first user+assistant turns) in the
    foreground, then run the assistant turn in the background.
    Returns the synthesis_id. Monkeypatched in tests."""
    s_id, assistant_id = await ask_service.start_thread(
        db, user_id=user_id, query=query,
    )
    await _spawn_answer(db, message_id=assistant_id, user_id=user_id)
    return s_id


async def _enqueue_followup(db, *, synthesis_id: int, query: str, user_id: int) -> int:
    """Append a user + pending-assistant turn, fire the background job.
    Returns the new assistant message id. Monkeypatched in tests."""
    assistant_id = await ask_service.add_followup(
        db, synthesis_id=synthesis_id, query=query,
    )
    await _spawn_answer(db, message_id=assistant_id, user_id=user_id)
    return assistant_id


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
    s_id = await _enqueue_first(db, user_id=user_id, query=q)
    return RedirectResponse(url=f"/ask/{s_id}", status_code=303)


@router.post("/ask/{synthesis_id}/followup")
async def ask_followup(
    synthesis_id: int,
    query: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    s = await _fetch_for_user(db, synthesis_id, user_id)  # 404s foreign profile
    q = query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Question is required")
    await _enqueue_followup(db, synthesis_id=s.id, query=q, user_id=user_id)
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
    all rendered turns plus the ordered source Video rows."""
    turns = await sm_repo.history(db, synthesis_id=s.id)
    rendered = []
    for t in turns:
        rendered.append({
            "role": t.role,
            "status": t.status.value,
            "error": t.error,
            "html": render_markdown(t.content) if (t.content and t.role == "assistant") else "",
            "text": t.content or "",
        })
    any_pending = any(t.status.value == "pending" for t in turns)
    ids = []
    if s.source_ids_json:
        try:
            ids = json.loads(s.source_ids_json)
        except (ValueError, TypeError):
            ids = []
    by_id = await videos_repo.get_many(db, ids)
    sources = [by_id[i] for i in ids if i in by_id]
    return {
        "synthesis": s,
        "turns": rendered,
        "any_pending": any_pending,
        "sources": sources,
    }


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

    Returns ONLY the inner body — never the base layout, so
    hx-swap=outerHTML doesn't nest the whole page into itself on every
    tick. On reaching a terminal state, sends HX-Refresh so the browser
    reloads /ask/<id> once and the surrounding chrome stays in sync.
    Mirrors digest_body_fragment."""
    s = await _fetch_for_user(db, synthesis_id, user_id)
    ctx = await _body_context(db, s)
    is_htmx_poll = request.headers.get("HX-Request") == "true"
    if is_htmx_poll and not ctx["any_pending"]:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(request, "ask/_body.html", ctx)
