import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.main import get_db
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
    return templates.TemplateResponse(
        request, "home.html", {"videos": videos, "q": q}
    )
