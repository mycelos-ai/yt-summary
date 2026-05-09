import asyncio
import re

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_db
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.services.playlist import fetch_playlist
from app.services.playlist_sync import load_older_videos, sync_playlist
from app.services.youtube import download_thumbnail
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

_PLAYLIST_ID_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


def _parse_playlist_id(url: str) -> str:
    match = _PLAYLIST_ID_RE.search(url)
    if not match:
        raise ValueError(f"Could not extract playlist id from {url!r}")
    return match.group(1)


@router.get("/playlists", response_class=HTMLResponse)
async def list_playlists(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Dedicated playlists page: every playlist with stats.

    Pulls per-playlist video counts in a single LEFT JOIN + GROUP BY
    so the page scales with N playlists, not N+1 queries.
    """
    rows = await playlists_repo.list_with_stats(db, 1)
    return templates.TemplateResponse(
        request,
        "playlists.html",
        {"rows": rows},
    )


@router.get("/playlists/new", response_class=HTMLResponse)
async def new_playlist_form(request: Request):
    return templates.TemplateResponse(request, "playlist_new.html", {})


@router.post("/playlists")
async def submit_playlist(
    url: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    try:
        _parse_playlist_id(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    cookies_exists = await asyncio.to_thread(config.cookies_path.exists)
    cookies = config.cookies_path if cookies_exists else None
    meta = await fetch_playlist(url, cookies_path=cookies)

    thumb_target = config.thumbnails_dir / f"playlist_{meta.id}.jpg"
    await download_thumbnail(meta.thumbnail_url, thumb_target)
    thumb_exists = await asyncio.to_thread(thumb_target.exists)
    thumb_db_path = str(thumb_target) if thumb_exists else None

    await playlists_repo.create(
        db,
        playlist_id=meta.id,
        user_id=1,
        url=meta.url,
        title=meta.title,
        description=meta.description,
        thumbnail_path=thumb_db_path,
    )

    raw_limit = await settings_repo.get(db, "playlist_initial_import_limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else 20
    except ValueError:
        limit = 20
    initial_limit: int | None = limit if limit > 0 else None

    await sync_playlist(db, config, meta.id, initial_limit=initial_limit)
    return RedirectResponse(f"/p/{meta.id}", status_code=303)


@router.get("/p/{playlist_id}", response_class=HTMLResponse)
async def playlist_detail(
    playlist_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    playlist = await playlists_repo.get(db, playlist_id)
    if playlist is None:
        raise HTTPException(404)
    videos = await playlists_repo.videos_for_playlist(db, playlist_id)
    return templates.TemplateResponse(
        request,
        "playlist_detail.html",
        {"playlist": playlist, "videos": videos},
    )


@router.post("/p/{playlist_id}/refresh")
async def playlist_refresh(
    playlist_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(404)
    await sync_playlist(db, config, playlist_id)
    return RedirectResponse(f"/p/{playlist_id}", status_code=303)


@router.post("/p/{playlist_id}/load-older")
async def playlist_load_older(
    playlist_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(404)
    raw_limit = await settings_repo.get(db, "playlist_initial_import_limit")
    try:
        count = int(raw_limit) if raw_limit is not None else 20
    except ValueError:
        count = 20
    if count <= 0:
        count = 20
    await load_older_videos(db, config, playlist_id, count=count)
    return RedirectResponse(f"/p/{playlist_id}", status_code=303)


@router.post("/p/{playlist_id}/remove")
async def playlist_remove(
    playlist_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(404)
    await playlists_repo.delete(db, playlist_id)
    return RedirectResponse("/", status_code=303)
