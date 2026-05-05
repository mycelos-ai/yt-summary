import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from app.config import Config
from app.main import get_config, get_db
from app.repos import chat as chat_repo
from app.repos import jobs as jobs_repo
from app.repos import videos as videos_repo
from app.services.youtube import download_thumbnail, fetch_metadata, parse_video_id

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_md = MarkdownIt()


@router.post("/videos")
async def submit_video(
    request: Request,
    url: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    try:
        parse_video_id(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    cookies = config.cookies_path if config.cookies_path.exists() else None
    meta = await fetch_metadata(url, cookies_path=cookies)

    thumb_target = config.thumbnails_dir / f"{meta.id}.jpg"
    await download_thumbnail(meta.thumbnail_url, thumb_target)
    thumb_db_path = str(thumb_target) if thumb_target.exists() else None

    await videos_repo.upsert_metadata(
        db,
        video_id=meta.id,
        url=meta.url,
        title=meta.title,
        description=meta.description,
        thumbnail_path=thumb_db_path,
        duration_seconds=meta.duration_seconds,
    )
    await jobs_repo.enqueue(db, meta.id)

    # HTMX request -> return the card fragment so it can be slotted into the
    # list. Plain browser submit -> redirect to the detail page so the user
    # lands on a full styled view that auto-polls the job status.
    if request.headers.get("HX-Request"):
        video = await videos_repo.get(db, meta.id)
        return templates.TemplateResponse(
            request, "video_card.html", {"video": video}
        )
    return RedirectResponse(f"/v/{meta.id}", status_code=303)


def _elapsed_seconds(job) -> int | None:
    if job is None or job.state.value != "running":
        return None
    from datetime import UTC, datetime

    # job.updated_at is naive UTC (SQLite datetime('now'))
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    delta = now_utc - job.updated_at
    return max(0, int(delta.total_seconds()))


@router.get("/v/{video_id}/status", response_class=HTMLResponse)
async def video_status(
    video_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    job = await jobs_repo.latest_for_video(db, video_id)
    return templates.TemplateResponse(
        request,
        "video_status.html",
        {"video": video, "job": job, "elapsed_s": _elapsed_seconds(job)},
    )


@router.get("/v/{video_id}/summary-fragment", response_class=HTMLResponse)
async def video_summary_fragment(
    video_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    job = await jobs_repo.latest_for_video(db, video_id)

    # If a poll arrives and the job has reached a terminal state, ask HTMX
    # to do a full page reload so the chat form, reindex button label, and
    # any other surrounding bits become consistent with the final state.
    is_htmx_poll = request.headers.get("HX-Request") == "true"
    job_terminal = job is not None and job.state.value in ("done", "failed")
    if is_htmx_poll and job_terminal:
        return HTMLResponse("", headers={"HX-Refresh": "true"})

    summary_html = _md.render(video.summary) if video.summary else ""
    return templates.TemplateResponse(
        request,
        "video_summary_section.html",
        {
            "video": video,
            "job": job,
            "summary_html": summary_html,
            "elapsed_s": _elapsed_seconds(job),
        },
    )


@router.get("/v/{video_id}.md")
async def video_markdown(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    parts: list[str] = [f"# {video.title}", "", f"Source: {video.url}", ""]
    if video.summary:
        parts += ["## Summary", "", video.summary, ""]
    if video.transcript:
        parts += ["## Transcript", "", video.transcript, ""]
    return PlainTextResponse("\n".join(parts), media_type="text/markdown; charset=utf-8")


@router.post("/v/{video_id}/reindex")
async def reindex_video(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    await jobs_repo.enqueue(db, video_id)
    return RedirectResponse(f"/v/{video_id}", status_code=303)


@router.get("/v/{video_id}", response_class=HTMLResponse)
async def video_detail(
    video_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    summary_html = _md.render(video.summary) if video.summary else ""
    history = await chat_repo.history(db, video_id)
    job = await jobs_repo.latest_for_video(db, video_id)
    return templates.TemplateResponse(
        request,
        "video_detail.html",
        {
            "video": video,
            "summary_html": summary_html,
            "chat_history": history,
            "job": job,
            "elapsed_s": _elapsed_seconds(job),
        },
    )
