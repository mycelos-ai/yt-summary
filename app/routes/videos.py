import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_db
from app.repos import jobs as jobs_repo
from app.repos import videos as videos_repo
from app.services.youtube import download_thumbnail, fetch_metadata, parse_video_id

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.post("/videos", response_class=HTMLResponse)
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
    video = await videos_repo.get(db, meta.id)
    return templates.TemplateResponse(
        request, "video_card.html", {"video": video}
    )


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
        request, "video_status.html", {"video": video, "job": job}
    )
