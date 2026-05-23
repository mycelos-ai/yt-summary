import asyncio
import re

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_current_user, get_current_user_id, get_db
from app.repos import mail_senders as mail_senders_repo
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.services.mail_sync import _imap_config_from_settings
from app.services.mailbox import discover_senders
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
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    """Dedicated playlists page: every playlist with stats.

    Pulls per-playlist video counts in a single LEFT JOIN + GROUP BY
    so the page scales with N playlists, not N+1 queries.
    """
    rows = await playlists_repo.list_with_stats(db, current_user_id)
    return templates.TemplateResponse(
        request,
        "playlists.html",
        {"rows": rows, "current_user": current_user},
    )


@router.get("/playlists/new", response_class=HTMLResponse)
async def new_playlist_form(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # The "Add a source" page is tabbed: YouTube playlists + Email
    # newsletters. The Email tab needs to know whether THIS profile has a
    # mailbox connected (config lives on the profile page) and which
    # senders it already knows about.
    imap_settings = await settings_repo.get_all_for_user(db, current_user.id)
    # "Connected" = valid credentials saved (independent of the polling
    # toggle), so a saved-but-not-enabled mailbox isn't falsely reported
    # as missing. The polling flag is surfaced separately as a hint.
    imap_configured = (
        _imap_config_from_settings(imap_settings, require_enabled=False)
        is not None
    )
    imap_polling_enabled = bool(imap_settings.get("imap_enabled"))
    senders = await mail_senders_repo.list_for_user(db, current_user.id)
    return templates.TemplateResponse(
        request,
        "playlist_new.html",
        {
            "current_user": current_user,
            "imap_configured": imap_configured,
            "imap_polling_enabled": imap_polling_enabled,
            "senders": senders,
        },
    )


@router.post("/playlists/new/mail/scan", response_class=HTMLResponse)
async def scan_mail_senders(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """Scan recent mail for distinct senders and render the subscribe
    checklist. Read-only on the mailbox (headers only, never marks
    seen). Also initialises the sync cursor to "now" on first scan so
    subscribing crawls forward instead of backfilling the whole inbox."""
    imap_settings = await settings_repo.get_all_for_user(db, current_user_id)
    # Scanning is a manual, read-only action — allow it whenever creds
    # exist, even if scheduled polling is toggled off.
    cfg = _imap_config_from_settings(imap_settings, require_enabled=False)
    if cfg is None:
        return HTMLResponse(
            '<p class="status status-failed">⚠ No mailbox connected for '
            'this profile yet. Set it up on your profile page first.</p>'
        )
    try:
        discovery = await discover_senders(cfg)
    except ValueError as e:
        return HTMLResponse(f'<p class="status status-failed">⚠ {e}</p>')

    await mail_senders_repo.upsert_discovered(
        db,
        current_user_id,
        [
            (s.addr, s.name, s.last_date.isoformat() if s.last_date else None,
             s.last_subject)
            for s in discovery.senders
        ],
    )
    # Forward-only default: if the cursor is unset, start from the newest
    # message so subscribing pulls future issues, not the back-catalogue.
    if imap_settings.get("imap_last_uid") is None and discovery.max_uid:
        await settings_repo.set_for_user(
            db, current_user_id, "imap_last_uid", str(discovery.max_uid)
        )

    senders = await mail_senders_repo.list_for_user(db, current_user_id)
    return templates.TemplateResponse(
        request,
        "_mail_senders.html",
        {"senders": senders, "scanned": True},
    )


@router.post("/playlists/new/mail/subscribe")
async def subscribe_mail_senders(
    sender: list[str] = Form(default=[]),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """Persist the subscribed sender set for this profile. Only checked
    senders are crawled by the scheduler from now on."""
    await mail_senders_repo.set_subscriptions(db, current_user_id, sender)
    return RedirectResponse("/playlists/new", status_code=303)


@router.post("/playlists")
async def submit_playlist(
    url: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
    current_user_id: int = Depends(get_current_user_id),
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
        user_id=current_user_id,
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
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    playlist = await playlists_repo.get(db, playlist_id)
    if playlist is None:
        raise HTTPException(404)
    # Hide playlists owned by other profiles. The cookie-based current
    # user mirrors how `videos_repo.list_recent` already scopes results;
    # this keeps the playlist routes consistent.
    if playlist.user_id != current_user_id:
        raise HTTPException(404)
    videos = await playlists_repo.videos_for_playlist(db, playlist_id)
    return templates.TemplateResponse(
        request,
        "playlist_detail.html",
        {
            "playlist": playlist,
            "videos": videos,
            "current_user": current_user,
        },
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
