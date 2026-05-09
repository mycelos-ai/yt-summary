import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_current_user, get_current_user_id, get_db
from app.models import VideoKind
from app.repos import chat as chat_repo
from app.repos import jobs as jobs_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services.markdown import render_markdown
from app.services.reader import fetch_article
from app.services.url_classify import classify_url, web_id_from_url
from app.services.youtube import download_thumbnail, fetch_metadata
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

# Markdown rendering + timestamp-link decoration moved to
# app.services.markdown so the chat path can share the same logic.


def _import_error_response(
    request: Request, *, submitted_url: str, error_message: str, error_title: str
) -> HTMLResponse:
    """Render a friendly error page instead of returning JSON.

    For HTMX requests we still send 400 so HTMX surfaces it; for plain
    browser submits we send 200 with the rendered page so the user
    sees the form again with their URL preserved.
    """
    status = 400 if request.headers.get("HX-Request") else 200
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "submitted_url": submitted_url,
            "error_message": error_message,
            "error_title": error_title,
        },
        status_code=status,
    )


@router.post("/videos")
async def submit_video(
    request: Request,
    url: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
    current_user_id: int = Depends(get_current_user_id),
):
    submitted = url
    url = url.strip().strip("'\"")
    if not url.startswith(("http://", "https://")):
        return _import_error_response(
            request,
            submitted_url=submitted,
            error_title="That doesn't look like a URL",
            error_message=(
                "Paste a full URL starting with http:// or https://."
            ),
        )

    kind = classify_url(url)
    try:
        if kind == "youtube":
            item_id = await _import_youtube(url, db, config, current_user_id)
        else:
            item_id = await _import_web(url, db, config, current_user_id)
    except ValueError as e:
        return _import_error_response(
            request,
            submitted_url=submitted,
            error_title="Couldn't add that",
            error_message=str(e),
        )
    except Exception as e:
        return _import_error_response(
            request,
            submitted_url=submitted,
            error_title="Something went wrong",
            error_message=f"{type(e).__name__}: {e}",
        )

    # HTMX request -> return the card fragment so it can be slotted into the
    # list. Plain browser submit -> redirect to the detail page so the user
    # lands on a full styled view that auto-polls the job status.
    if request.headers.get("HX-Request"):
        video = await videos_repo.get(db, item_id)
        return templates.TemplateResponse(
            request, "video_card.html", {"video": video}
        )
    return RedirectResponse(f"/v/{item_id}", status_code=303)



def _composite_id(user_id: int, base_id: str) -> str:
    """Build the per-profile video id used for new imports.

    Existing rows on a single-user install keep their bare ids; new
    rows get the user_id prefix so the same YouTube video can live in
    multiple profiles' libraries without colliding on the videos PK.
    """
    return f"{user_id}:{base_id}"


async def _import_youtube(
    url: str,
    db: aiosqlite.Connection,
    config: Config,
    user_id: int,
) -> str:
    cookies = config.cookies_path if config.cookies_path.exists() else None
    meta = await fetch_metadata(url, cookies_path=cookies)

    item_id = _composite_id(user_id, meta.id)
    thumb_target = config.thumbnails_dir / f"{item_id}.jpg"
    await download_thumbnail(meta.thumbnail_url, thumb_target)
    thumb_db_path = str(thumb_target) if thumb_target.exists() else None

    await videos_repo.upsert_metadata(
        db,
        video_id=item_id,
        url=meta.url,
        title=meta.title,
        description=meta.description,
        thumbnail_path=thumb_db_path,
        duration_seconds=meta.duration_seconds,
        kind=VideoKind.YOUTUBE,
        user_id=user_id,
        youtube_id=meta.id,
    )
    if meta.tags:
        await tags_repo.set_tags_for_video(db, item_id, list(meta.tags))
    await jobs_repo.enqueue(db, item_id)
    return item_id


async def _import_web(
    url: str,
    db: aiosqlite.Connection,
    config: Config,
    user_id: int,
) -> str:
    """Fetch the article body up-front so the user gets a fast 400 if
    extraction fails, rather than a confusing 'queued' state followed
    by a failed job."""
    article = await fetch_article(url)
    base_id = web_id_from_url(article.url)
    item_id = _composite_id(user_id, base_id)

    thumb_db_path: str | None = None
    if article.thumbnail_url:
        thumb_target = config.thumbnails_dir / f"{item_id}.jpg"
        try:
            await download_thumbnail(article.thumbnail_url, thumb_target)
            if thumb_target.exists():
                thumb_db_path = str(thumb_target)
        except Exception:
            # Thumbnail is cosmetic; never block import on it.
            pass

    await videos_repo.upsert_metadata(
        db,
        video_id=item_id,
        url=article.url,
        title=article.title,
        description=article.description,
        thumbnail_path=thumb_db_path,
        duration_seconds=None,
        kind=VideoKind.WEB,
        user_id=user_id,
    )
    # Persist the body now so the pipeline doesn't refetch (article
    # text is stable; refetch is only useful when the user clicks
    # "Re-summarize" with no transcript yet — handled in pipeline).
    from app.models import TranscriptSource
    await videos_repo.set_transcript(
        db, item_id, article.body, TranscriptSource.WEB
    )
    await jobs_repo.enqueue(db, item_id)
    return item_id


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
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != current_user_id:
        raise HTTPException(404)
    job = await jobs_repo.latest_for_video(db, video_id)
    return templates.TemplateResponse(
        request,
        "video_status.html",
        {
            "video": video,
            "job": job,
            "elapsed_s": _elapsed_seconds(job),
            "current_user": current_user,
        },
    )


@router.get("/v/{video_id}/summary-fragment", response_class=HTMLResponse)
async def video_summary_fragment(
    video_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != current_user_id:
        raise HTTPException(404)
    job = await jobs_repo.latest_for_video(db, video_id)

    # If a poll arrives and the job has reached a terminal state, ask HTMX
    # to do a full page reload so the chat form, reindex button label, and
    # any other surrounding bits become consistent with the final state.
    is_htmx_poll = request.headers.get("HX-Request") == "true"
    job_terminal = job is not None and job.state.value in ("done", "failed")
    if is_htmx_poll and job_terminal:
        return HTMLResponse("", headers={"HX-Refresh": "true"})

    summary_html = render_markdown(video.summary or "")
    return templates.TemplateResponse(
        request,
        "video_summary_section.html",
        {
            "video": video,
            "job": job,
            "summary_html": summary_html,
            "elapsed_s": _elapsed_seconds(job),
            "current_user": current_user,
        },
    )


@router.get("/v/{video_id}.md")
async def video_markdown(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Combined export — summary + transcript + metadata. Kept for
    curl/scripting users who want the whole thing in one shot. The UI
    no longer surfaces it; per-section downloads (`/summary.md`,
    `/transcript.md`) are what the buttons point at."""
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    parts: list[str] = [f"# {video.title}", "", f"Source: {video.url}", ""]
    if video.summary:
        parts += ["## Summary", "", video.summary, ""]
    if video.transcript:
        parts += ["## Transcript", "", video.transcript, ""]
    return PlainTextResponse("\n".join(parts), media_type="text/markdown; charset=utf-8")


@router.get("/v/{video_id}/summary.md")
async def video_summary_markdown(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Just the summary, with a small header so the file is meaningful
    on its own (you usually want to know which video the summary came
    from)."""
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    if not video.summary:
        raise HTTPException(404, detail="No summary available yet")
    body = "\n".join([
        f"# {video.title}",
        "",
        f"Source: {video.url}",
        "",
        "## Summary",
        "",
        video.summary,
        "",
    ])
    return PlainTextResponse(body, media_type="text/markdown; charset=utf-8")


@router.get("/v/{video_id}/transcript.md")
async def video_transcript_markdown(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Just the transcript / article body. Same header pattern as the
    summary export."""
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    if not video.transcript:
        raise HTTPException(404, detail="No transcript available yet")
    section_label = (
        "Article body" if video.kind == VideoKind.WEB else "Transcript"
    )
    body = "\n".join([
        f"# {video.title}",
        "",
        f"Source: {video.url}",
        "",
        f"## {section_label}",
        "",
        video.transcript,
        "",
    ])
    return PlainTextResponse(body, media_type="text/markdown; charset=utf-8")


@router.post("/v/{video_id}/reindex")
async def reindex_video(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != current_user_id:
        raise HTTPException(404)
    await jobs_repo.enqueue(db, video_id)
    return RedirectResponse(f"/v/{video_id}", status_code=303)


@router.post("/v/{video_id}/retranscribe")
async def retranscribe_video(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """Throw away the stored transcript + segments so the worker
    fetches them fresh. Useful when transcript-format improvements
    ship and you want the new format applied to old videos."""
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != current_user_id:
        raise HTTPException(404)
    await videos_repo.clear_transcript(db, video_id)
    await jobs_repo.enqueue(db, video_id)
    return RedirectResponse(f"/v/{video_id}", status_code=303)


@router.get("/v/{video_id}", response_class=HTMLResponse)
async def video_detail(
    video_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != current_user_id:
        raise HTTPException(404)
    summary_html = render_markdown(video.summary or "")
    history = await chat_repo.history(db, video_id)
    job = await jobs_repo.latest_for_video(db, video_id)
    video_tags = await tags_repo.tags_for_video(db, video_id)
    # Parse the JSON-stored transcript segments into render-ready blocks.
    # The template falls back to the plain transcript if blocks is empty.
    transcript_blocks = _parse_transcript_blocks(video)
    return templates.TemplateResponse(
        request,
        "video_detail.html",
        {
            "video": video,
            "summary_html": summary_html,
            "chat_history": history,
            "job": job,
            "video_tags": video_tags,
            "elapsed_s": _elapsed_seconds(job),
            "transcript_blocks": transcript_blocks,
            "current_user": current_user,
        },
    )


def _parse_transcript_blocks(video) -> list[dict]:
    """Decode video.transcript_segments (JSON) into a list of dicts
    with {start_s, timestamp, text} suitable for the template.

    Returns [] when the video has no segments stored — the template
    then renders the plain transcript fallback.
    """
    raw = getattr(video, "transcript_segments", None)
    if not raw:
        return []
    import json as _json

    from app.services.transcript_format import format_timestamp
    try:
        items = _json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    duration = video.duration_seconds or 0
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        start = item.get("start")
        text = (item.get("text") or "").strip()
        if start is None or not text:
            continue
        out.append({
            "start_s": float(start),
            "timestamp": format_timestamp(float(start), total_duration_s=duration),
            "text": text,
        })
    return out
