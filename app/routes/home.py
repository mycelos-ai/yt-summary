import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.main import get_db
from app.repos import playlists as playlists_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    q: str | None = None,
    tag: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
):
    tag = tag.strip() if tag else None
    if q:
        videos = await videos_repo.search(db, q, tag=tag)
    else:
        videos = await videos_repo.list_recent(db, tag=tag)
    playlists = await playlists_repo.list_for_user(db, 1)
    video_ids = [v.id for v in videos]
    playlist_links = await playlists_repo.playlists_for_videos(db, video_ids)
    video_tags = await tags_repo.tags_for_videos(db, video_ids)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "videos": videos,
            "q": q,
            "active_tag": tag,
            "playlists": playlists,
            "playlist_links": playlist_links,
            "video_tags": video_tags,
        },
    )
