"""Personal podcast feed routes (Part B).

Podcast clients fetch plain URLs — no Bearer headers, no cookies — so the
feed and its episodes are gated by a per-profile capability token in the
URL path (users.podcast_token). 404 on any unknown token, so the feed's
existence isn't leaked.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app.config import Config
from app.main import get_config, get_db
from app.repos import tts_jobs as tts_jobs_repo
from app.repos import users as users_repo
from app.repos import videos as videos_repo
from app.services import podcast as podcast_svc

router = APIRouter()


def _mp3_path(config: Config, job_audio_path: str) -> Path:
    """Resolve a tts_jobs.audio_path to a real path, verifying it stays
    inside tts_audio_dir (path-traversal guard, mirrors routes/audio.py)."""
    candidate = (config.data_dir / job_audio_path).resolve()
    audio_root = config.tts_audio_dir.resolve()
    try:
        candidate.relative_to(audio_root)
    except ValueError as e:
        raise HTTPException(404) from e
    return candidate


@router.get("/podcast/{token}/feed.xml")
async def podcast_feed(
    token: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    user = await users_repo.get_by_podcast_token(db, token)
    if user is None:
        raise HTTPException(404)
    jobs = await tts_jobs_repo.list_done_for_user(
        db, user_id=user.id, limit=100,
    )
    # base_url honours proxy headers (uvicorn runs with --proxy-headers),
    # so the feed works behind HTTPS proxies.
    base_url = str(request.base_url).rstrip("/")

    episodes = []
    for job in jobs:
        if not job.audio_path:
            continue
        video = await videos_repo.get(db, job.video_id)
        if video is None:
            continue
        mp3 = _mp3_path(config, job.audio_path)
        byte_length = mp3.stat().st_size if mp3.exists() else 0
        summary = (video.summary or "")[:500]
        episodes.append({
            "job_id": job.id,
            "title": video.title,
            "description": summary,
            "source": job.source,
            "target_language": job.target_language,
            "translated": bool(job.translated_text),
            "duration_seconds": job.duration_seconds,
            "byte_length": byte_length,
            "thumbnail_url": (
                f"{base_url}/thumbnails/{video.id}.jpg"
                if video.thumbnail_path else None
            ),
        })

    xml = podcast_svc.build_feed_xml(
        profile_name=user.name, token=token,
        episodes=episodes, base_url=base_url,
    )
    return Response(xml, media_type="application/rss+xml")


@router.get("/podcast/{token}/episode/{job_id}.mp3")
async def podcast_episode(
    token: str,
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    user = await users_repo.get_by_podcast_token(db, token)
    if user is None:
        raise HTTPException(404)
    job = await tts_jobs_repo.get(db, job_id)
    if job is None or not job.audio_path or job.status != "done":
        raise HTTPException(404)
    # The job must belong to one of this profile's videos.
    video = await videos_repo.get(db, job.video_id)
    if video is None or video.user_id != user.id:
        raise HTTPException(404)
    mp3 = _mp3_path(config, job.audio_path)
    if not mp3.exists():
        raise HTTPException(404)
    # FileResponse handles HTTP Range (podcast apps seek) automatically.
    return FileResponse(mp3, media_type="audio/mpeg")
