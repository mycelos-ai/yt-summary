import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.main import get_current_user_id, get_db
from app.repos import source_speakers as ss_repo
from app.repos import speakers as sp_repo
from app.repos import videos as videos_repo
from app.services import speaker_pipeline
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)


async def _owned_video(db, video_id: str, current_user_id: int):
    """Load a video or 404 — including foreign-profile rows (404, not 403,
    matching routes/chat.py + routes/videos.py)."""
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != current_user_id:
        raise HTTPException(404)
    return video


def _chips_response(request: Request, video, speakers) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_speaker_chips.html", {"video": video, "speakers": speakers}
    )


@router.post("/v/{video_id}/speakers/detect", response_class=HTMLResponse)
async def detect_speakers(
    request: Request,
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await _owned_video(db, video_id, current_user_id)
    await speaker_pipeline.detect_and_link(db, video)
    speakers = await ss_repo.list_for_source(db, video_id)
    return _chips_response(request, video, speakers)


@router.post("/v/{video_id}/speakers", response_class=HTMLResponse)
async def add_speaker(
    request: Request,
    video_id: str,
    name: str = Form(...),
    role: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await _owned_video(db, video_id, current_user_id)
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "Speaker name required")
    speaker_id = await sp_repo.resolve_speaker(
        db, user_id=current_user_id, name=clean_name, role=role.strip() or None,
    )
    await ss_repo.link_speaker(
        db, video_id, speaker_id,
        role=role.strip() or None, detection_source="manual",
    )
    speakers = await ss_repo.list_for_source(db, video_id)
    return _chips_response(request, video, speakers)


@router.post("/v/{video_id}/speakers/{speaker_id}/unlink", response_class=HTMLResponse)
async def unlink_speaker(
    request: Request,
    video_id: str,
    speaker_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await _owned_video(db, video_id, current_user_id)
    await ss_repo.unlink(db, video_id, speaker_id)
    speakers = await ss_repo.list_for_source(db, video_id)
    return _chips_response(request, video, speakers)
