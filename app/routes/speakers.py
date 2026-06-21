from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_current_user_id, get_db
from app.repos import source_speakers as ss_repo
from app.repos import speakers as sp_repo
from app.repos import videos as videos_repo
from app.services import avatars, speaker_pipeline
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


# ---------------------------------------------------------------------------
# Speaker detail page (PR 2: header + confirmed sources)
# ---------------------------------------------------------------------------

async def _owned_speaker(
    db: aiosqlite.Connection, speaker_id: int, current_user_id: int,
):
    """Load a speaker or 404 — foreign-profile rows get a 404, not 403."""
    sp = await sp_repo.get_speaker(db, speaker_id)
    if sp is None or sp.user_id != current_user_id:
        raise HTTPException(404)
    return sp


@router.get("/speaker/{speaker_id}", response_class=HTMLResponse)
async def speaker_page(
    request: Request,
    speaker_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await _owned_speaker(db, speaker_id, current_user_id)
    sources = await ss_repo.list_sources_for_speaker(db, speaker_id)
    return templates.TemplateResponse(
        request, "speaker.html",
        {"speaker": speaker, "sources": sources, "avatars": avatars.AVATARS},
    )


@router.post("/speaker/{speaker_id}/edit", response_class=HTMLResponse)
async def edit_speaker(
    request: Request,
    speaker_id: int,
    name: str = Form(...),
    role: str = Form(""),
    avatar_id: str = Form(""),
    style_note: str = Form(""),
    photo: UploadFile | None = File(None),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
    current_user_id: int = Depends(get_current_user_id),
):
    await _owned_speaker(db, speaker_id, current_user_id)
    clean_avatar = avatar_id if avatars.is_valid_id(avatar_id) else None
    await sp_repo.update_fields(
        db, speaker_id,
        name=name.strip(),
        role=role.strip() or None,
        avatar_id=clean_avatar,
        style_note=style_note.strip() or None,
    )
    if photo is not None and photo.filename:
        if not (photo.content_type or "").startswith("image/"):
            raise HTTPException(400, "Photo must be an image file")
        dest_dir = Path(config.data_dir) / "speaker_photos"
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(photo.filename).suffix or ".jpg"
        dest = dest_dir / f"{speaker_id}{suffix}"
        dest.write_bytes(await photo.read())
        await sp_repo.set_photo_path(db, speaker_id, str(dest))
    return RedirectResponse(f"/speaker/{speaker_id}", status_code=303)


@router.get("/speaker/{speaker_id}/photo")
async def speaker_photo(
    speaker_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await _owned_speaker(db, speaker_id, current_user_id)
    if not speaker.avatar_photo_path or not Path(speaker.avatar_photo_path).exists():
        raise HTTPException(404)
    return FileResponse(speaker.avatar_photo_path)
