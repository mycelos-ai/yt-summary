"""REST API. Mounted at /api/v1.

Each handler authenticates via the api dependency, then delegates to
services/api.py.
"""

from typing import Any

import aiosqlite
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.config import Config
from app.main import get_config, get_db
from app.repos import playlists as playlists_repo
from app.services import api as api_svc
from app.services.auth import authenticate
from app.services.playlist import fetch_playlist
from app.services.playlist_sync import (
    load_older_videos as _load_older,
)
from app.services.playlist_sync import (
    sync_playlist as _sync_playlist,
)
from app.services.youtube import download_thumbnail

router = APIRouter(prefix="/api/v1")

API_VERSION = "0.4.0"


async def current_user(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> int:
    return await authenticate(db, request)


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": API_VERSION}


@router.post("/videos")
async def api_submit_video(
    payload: dict = Body(...),
    wait: bool = Query(False),
    wait_timeout: int = Query(60, ge=0, le=300, alias="timeout"),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    url = payload.get("url", "")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "url is required", "code": "INVALID_INPUT"},
        )
    try:
        resource = await api_svc.submit_video(
            db, config, url=url, user_id=user_id,
            wait=wait, wait_timeout=wait_timeout,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": str(e), "code": "IMPORT_FAILED"},
        ) from e
    # Wrap with `video_id` alias on top of the resource so clients can
    # discover the new ID without parsing every nested field.
    payload_out = {"video_id": resource["id"], **resource}
    status_code = 200 if resource["summary_ready"] else 202
    return JSONResponse(payload_out, status_code=status_code)


@router.get("/videos")
async def api_list_videos(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tag: str | None = Query(None),
    playlist_id: str | None = Query(None),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    videos = await api_svc.list_videos(
        db, limit=limit, offset=offset, tag=tag, playlist_id=playlist_id,
    )
    return {"videos": videos}


@router.get("/videos/{video_id}")
async def api_get_video(
    video_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    resource = await api_svc.get_video_resource(db, video_id)
    if resource is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        )
    return resource


@router.get("/videos/{video_id}/summary")
async def api_get_summary(
    video_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    from app.repos import videos as videos_repo
    video = await videos_repo.get(db, video_id)
    if video is None or not video.summary:
        raise HTTPException(
            status_code=404,
            detail={"error": "Summary not available", "code": "NOT_FOUND"},
        )
    return {"summary": video.summary, "model": video.summary_model}


@router.get("/videos/{video_id}/transcript")
async def api_get_transcript(
    video_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    from app.repos import videos as videos_repo
    video = await videos_repo.get(db, video_id)
    if video is None or not video.transcript:
        raise HTTPException(
            status_code=404,
            detail={"error": "Transcript not available", "code": "NOT_FOUND"},
        )
    return {
        "transcript": video.transcript,
        "source": (
            video.transcript_source.value
            if video.transcript_source else None
        ),
    }


@router.post("/videos/{video_id}/reindex")
async def api_reindex_video(
    video_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    try:
        await api_svc.reindex_video(db, video_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        ) from None
    return JSONResponse({"queued": True}, status_code=202)


@router.post("/videos/{video_id}/chat")
async def api_chat(
    video_id: str,
    payload: dict = Body(...),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    content = payload.get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "content is required", "code": "INVALID_INPUT"},
        )
    try:
        result = await api_svc.chat_about_video(
            db, video_id, content, user_id=user_id,
        )
    except ValueError as e:
        msg = str(e)
        if "LLM" in msg:
            raise HTTPException(
                status_code=400,
                detail={"error": msg, "code": "LLM_NOT_CONFIGURED"},
            ) from e
        raise HTTPException(
            status_code=404,
            detail={"error": msg, "code": "NOT_FOUND"},
        ) from e
    return result


@router.get("/search")
async def api_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    tag: str | None = Query(None),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    hits = await api_svc.search_videos(db, q, limit=limit, tag=tag)
    return {"hits": hits}


@router.get("/playlists")
async def api_list_playlists(
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    return {"playlists": await api_svc.list_playlists(db, user_id=user_id)}


@router.post("/playlists")
async def api_create_playlist(
    payload: dict = Body(...),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    url = payload.get("url", "")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "url is required", "code": "INVALID_INPUT"},
        )
    import re
    match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    if not match:
        raise HTTPException(
            status_code=400,
            detail={"error": "Not a playlist URL", "code": "INVALID_INPUT"},
        )

    cookies = config.cookies_path if config.cookies_path.exists() else None
    meta = await fetch_playlist(url, cookies_path=cookies)
    thumb_target = config.thumbnails_dir / f"playlist_{meta.id}.jpg"
    await download_thumbnail(meta.thumbnail_url, thumb_target)
    thumb_db_path = str(thumb_target) if thumb_target.exists() else None

    await playlists_repo.create(
        db,
        playlist_id=meta.id,
        user_id=user_id,
        url=meta.url,
        title=meta.title,
        description=meta.description,
        thumbnail_path=thumb_db_path,
    )
    await _sync_playlist(db, config, meta.id)
    rows = await api_svc.list_playlists(db, user_id=user_id)
    out = next((r for r in rows if r["id"] == meta.id), None)
    return JSONResponse(out, status_code=201)


@router.post("/playlists/{playlist_id}/refresh")
async def api_refresh_playlist(
    playlist_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        )
    await _sync_playlist(db, config, playlist_id)
    return {"refreshed": True}


@router.post("/playlists/{playlist_id}/load-older")
async def api_load_older(
    playlist_id: str,
    count: int = Query(20, ge=1, le=100),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        )
    result = await _load_older(db, config, playlist_id, count=count)
    return {
        "newly_linked": result.newly_linked,
        "newly_enqueued": result.newly_enqueued,
    }


@router.delete("/playlists/{playlist_id}")
async def api_remove_playlist(
    playlist_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        )
    await playlists_repo.delete(db, playlist_id)
    return {"removed": True}


@router.get("/tags")
async def api_list_tags(
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    return {"tags": await api_svc.list_tags(db)}
