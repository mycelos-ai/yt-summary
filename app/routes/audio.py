"""HTMX modal + render/status/delete/file routes for TTS audio.

These endpoints sit under ``/v/{video_id}/audio*`` and drive the
"Audio" feature on the video detail page:

* ``GET  /v/{video_id}/audio``                  — render the form
* ``POST /v/{video_id}/audio/render``           — enqueue (or return cached)
* ``GET  /v/{video_id}/audio/status/{job_id}``  — HTMX-polled progress
* ``POST /v/{video_id}/audio/{job_id}/delete``  — drop row + MP3
* ``GET  /v/{video_id}/audio/file/{job_id}``    — stream the MP3

All routes verify the video belongs to the active profile and 404
otherwise (we don't leak 403 — keeps profile boundaries opaque).

The single ``audio_modal.html`` template renders four shapes
(``view in {'form', 'progress', 'done', 'failed'}``) — the route
picks the shape based on job status. The persistent rendering list
on the detail page is a separate template ``audio_renderings_block.html``
that re-renders in place on delete.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_current_user_id, get_db
from app.repos import settings as settings_repo
from app.repos import tts_jobs as tts_jobs_repo
from app.repos import videos as videos_repo
from app.services import tts_voices
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)


# All supported languages in display order. Drives the target-language
# dropdown. The voice-dropdown options for each language come from the
# catalogue (``voices_for_language``) and the client filters them via
# a small inline script that swaps the option list when the language
# changes.
#
# The (code, label) tuple lives in app/services/tts_voices.py so the
# audio modal, the settings card, and the "Detected: …" hint all share
# the same flag-prefixed labels and can't drift out of sync.
_LANGUAGES: tuple[tuple[str, str], ...] = tts_voices.LANGUAGES


def _default_target_language(
    settings: dict[str, str], video_source_language: str | None
) -> str:
    """Fallback chain for the pre-selected target language:

    1. ``default_tts_target_language`` setting (Task 13);
    2. the video's ``source_language`` if it matches a catalogue lang;
    3. the first language in the catalogue.
    """
    # The settings card (Task 13) writes ``default_tts_language``;
    # honour the older ``default_tts_target_language`` key too for
    # back-compat with any pre-card rows. "auto" means "fall through
    # to the video's source language", so we don't short-circuit.
    explicit = (
        settings.get("default_tts_language")
        or settings.get("default_tts_target_language")
    )
    if explicit and explicit != "auto" and explicit in {
        lang for lang, _ in _LANGUAGES
    }:
        return explicit
    if video_source_language and video_source_language in {
        lang for lang, _ in _LANGUAGES
    }:
        return video_source_language
    return _LANGUAGES[0][0]


def _default_voice(settings: dict[str, str], language: str) -> str:
    """Settings default if compatible with `language`, else the first
    voice the catalogue knows for that language.

    The settings card (Task 13) writes a per-language voice key
    (``default_tts_voice_de``, ``default_tts_voice_en_US`` …); we
    also honour an older single-voice key (``default_tts_voice``) for
    backward compatibility with any pre-card settings rows.
    """
    candidates = tts_voices.voices_for_language(language)
    explicit = (
        settings.get(f"default_tts_voice_{language}")
        or settings.get("default_tts_voice")
    )
    if explicit and any(v.id == explicit for v in candidates):
        return explicit
    return candidates[0].id if candidates else ""


def _default_quality(
    settings: dict[str, str], language: str, voice_id: str
) -> str:
    """Settings default if compatible with the chosen voice, else
    'medium' if available, else the voice's first quality tier."""
    allowed = tts_voices.qualities_for_voice(language, voice_id)
    explicit = settings.get("default_tts_quality")
    if explicit and explicit in allowed:
        return explicit
    if "medium" in allowed:
        return "medium"
    return allowed[0] if allowed else "medium"


async def _get_owned_video(
    db: aiosqlite.Connection, video_id: str, current_user_id: int,
):
    """Fetch the video and 404 if missing or not owned by the active
    profile. Keeps the profile boundary opaque (no 403)."""
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != current_user_id:
        raise HTTPException(404)
    return video


def _mp3_path(config: Config, job_audio_path: str) -> Path:
    """Resolve `tts_jobs.audio_path` (relative to data_dir) to a real
    filesystem path and verify it stays INSIDE ``tts_audio_dir``
    after resolution — defends against any future code path that
    might let user input bleed into ``audio_path``."""
    candidate = (config.data_dir / job_audio_path).resolve()
    audio_root = config.tts_audio_dir.resolve()
    try:
        candidate.relative_to(audio_root)
    except ValueError as e:
        raise HTTPException(404) from e
    return candidate


def _view_for_status(status: str) -> str:
    if status == "done":
        return "done"
    if status == "failed":
        return "failed"
    # queued / translating / rendering — all show the polling shape.
    return "progress"


# ----------------------------------------------------------------- routes


@router.get("/v/{video_id}/audio", response_class=HTMLResponse)
async def audio_modal(
    video_id: str,
    request: Request,
    source: str | None = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """Render the form fragment. The voice catalogue is passed in full
    so the template can group <option>s by language; the client
    filters voices in JS when the language changes.

    The optional ``?source=`` query param lets the inline "Audio"
    buttons next to each section's ``↓ .md`` download pre-select
    which source to render — when set, the form hides the Source
    select and renders a hidden input instead. Unknown values are
    treated as no-preselection (fall back to the full select)."""
    video = await _get_owned_video(db, video_id, current_user_id)
    settings = await settings_repo.get_all(db)
    default_target = _default_target_language(settings, video.source_language)
    default_voice = _default_voice(settings, default_target)
    default_quality = _default_quality(settings, default_target, default_voice)
    preselected_source = source if source in ("summary", "transcript") else None
    return templates.TemplateResponse(
        request,
        "audio_modal.html",
        {
            "view": "form",
            "video": video,
            "languages": _LANGUAGES,
            # Same (code, label) data as `languages`, but indexed by code
            # so the template can resolve `video.source_language` to a
            # human-readable label for the "Detected: …" hint when the
            # column is already populated.
            "source_language_labels": dict(_LANGUAGES),
            "voices": tts_voices.VOICES,
            "default_target": default_target,
            "default_voice": default_voice,
            "default_quality": default_quality,
            "has_summary": video.summary is not None,
            "has_transcript": video.transcript is not None,
            "preselected_source": preselected_source,
        },
    )


@router.post("/v/{video_id}/audio/render", response_class=HTMLResponse)
async def audio_render(
    video_id: str,
    request: Request,
    source: str = Form(...),
    target_language: str = Form(...),
    voice: str = Form(...),
    quality: str = Form(...),
    source_language: str = Form("auto"),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await _get_owned_video(db, video_id, current_user_id)

    # Validate source.
    if source not in ("summary", "transcript"):
        raise HTTPException(400, "invalid source")
    if source == "summary" and not video.summary:
        raise HTTPException(400, "no summary to render")
    if source == "transcript" and not video.transcript:
        raise HTTPException(400, "no transcript to render")

    # Validate target language + voice + quality against the catalogue.
    allowed_languages = {lang for lang, _ in _LANGUAGES}
    if target_language not in allowed_languages:
        raise HTTPException(400, "unsupported target language")
    # Validate source_language: "auto" (default) means "don't override what
    # we already know"; any explicit value must be in the catalogue.
    if source_language and source_language != "auto":
        if source_language not in allowed_languages:
            raise HTTPException(400, "invalid source language")
    qualities = tts_voices.qualities_for_voice(target_language, voice)
    if not qualities:
        raise HTTPException(
            400, f"voice {voice!r} is not available for {target_language!r}"
        )
    if quality not in qualities:
        raise HTTPException(
            400, f"voice {voice!r} does not ship a {quality!r} tier"
        )

    # Persist an explicit source-language pick IFF the video's column is
    # still NULL — `set_source_language` only writes when NULL, so a
    # user's manual pick can't silently overwrite an already-detected
    # value. The worker then naturally picks up the now-populated
    # `videos.source_language` and runs translation.
    if source_language and source_language != "auto":
        await videos_repo.set_source_language(db, video.id, source_language)

    job = await tts_jobs_repo.enqueue(
        db,
        video_id=video.id,
        source=source,
        target_language=target_language,
        voice=voice,
        quality=quality,
    )
    view = _view_for_status(job.status)
    # When the enqueue resolves directly to a cached `done` job, fire
    # `audio:rendered` so the persistent renderings list on the detail
    # page re-fetches itself (the block listens for the event via
    # `hx-trigger="audio:rendered from:body"`).
    headers = {"HX-Trigger": "audio:rendered"} if view == "done" else {}
    return templates.TemplateResponse(
        request,
        "audio_modal.html",
        {
            "view": view,
            "video": video,
            "job": job,
        },
        headers=headers,
    )


@router.get("/v/{video_id}/audio/status/{job_id}", response_class=HTMLResponse)
async def audio_status(
    video_id: str,
    job_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """HTMX-polled. The polling shape (view='progress') re-fetches
    itself every 2s; the terminal shapes (done/failed) replace the
    fragment with no further trigger."""
    video = await _get_owned_video(db, video_id, current_user_id)
    job = await tts_jobs_repo.get(db, job_id)
    if job is None or job.video_id != video.id:
        raise HTTPException(404)
    view = _view_for_status(job.status)
    # Polling tick that finds the job is now `done` is the moment the
    # persistent renderings list should refresh — emit the event so
    # the listening block re-fetches itself.
    headers = {"HX-Trigger": "audio:rendered"} if view == "done" else {}
    return templates.TemplateResponse(
        request,
        "audio_modal.html",
        {
            "view": view,
            "video": video,
            "job": job,
        },
        headers=headers,
    )


@router.get("/v/{video_id}/audio/renderings", response_class=HTMLResponse)
async def audio_renderings(
    video_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """Re-render the persistent renderings list. Fired by HTMX after
    a render completes — the modal emits ``HX-Trigger: audio:rendered``
    when it switches to the ``done`` view, and the block listens via
    ``hx-trigger="audio:rendered from:body"``."""
    video = await _get_owned_video(db, video_id, current_user_id)
    renderings = await tts_jobs_repo.list_for_video(db, video_id)
    return templates.TemplateResponse(
        request,
        "audio_renderings_block.html",
        {"video": video, "renderings": renderings},
    )


@router.post("/v/{video_id}/audio/{job_id}/delete", response_class=HTMLResponse)
async def audio_delete(
    video_id: str,
    job_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
    current_user_id: int = Depends(get_current_user_id),
):
    """Delete the row and the MP3 from disk. Re-renders the
    persistent renderings list so HTMX can swap it in-place."""
    video = await _get_owned_video(db, video_id, current_user_id)
    job = await tts_jobs_repo.get(db, job_id)
    if job is None or job.video_id != video.id:
        raise HTTPException(404)

    if job.audio_path:
        # Guard against path traversal before unlinking. If the path
        # escapes tts_audio_dir we silently skip the disk delete and
        # still drop the row — better than refusing the whole op.
        try:
            mp3 = _mp3_path(config, job.audio_path)
            mp3.unlink(missing_ok=True)
        except HTTPException:
            pass
    await tts_jobs_repo.delete(db, job_id)

    renderings = await tts_jobs_repo.list_for_video(db, video.id)
    return templates.TemplateResponse(
        request,
        "audio_renderings_block.html",
        {"video": video, "renderings": renderings},
    )


@router.get("/v/{video_id}/audio/file/{job_id}")
async def audio_file(
    video_id: str,
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
    current_user_id: int = Depends(get_current_user_id),
):
    """Stream the MP3 with audio/mpeg + a friendly filename."""
    video = await _get_owned_video(db, video_id, current_user_id)
    job = await tts_jobs_repo.get(db, job_id)
    if job is None or job.video_id != video.id or not job.audio_path:
        raise HTTPException(404)
    mp3 = _mp3_path(config, job.audio_path)
    if not mp3.exists():
        raise HTTPException(404)
    filename = f"{video.id}-{job.source}-{job.target_language}.mp3"
    return FileResponse(
        mp3, media_type="audio/mpeg", filename=filename,
    )
