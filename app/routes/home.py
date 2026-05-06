import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.main import get_db
from app.repos import playlists as playlists_repo
from app.repos import videos as videos_repo

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    q: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
):
    if q:
        videos = await videos_repo.search(db, q)
    else:
        videos = await videos_repo.list_recent(db)
    playlists = await playlists_repo.list_for_user(db, 1)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"videos": videos, "q": q, "playlists": playlists},
    )
