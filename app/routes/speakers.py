from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import escape

from app.config import Config
from app.main import get_config, get_current_user_id, get_db
from app.repos import chat as chat_repo
from app.repos import chat_threads as threads_repo
from app.repos import llm_models as llm_models_repo
from app.repos import source_speakers as ss_repo
from app.repos import speaker_claims as claims_repo
from app.repos import speakers as sp_repo
from app.repos import videos as videos_repo
from app.services import avatars, speaker_pipeline
from app.services import speakers as speakers_svc
from app.services.markdown import render_markdown
from app.services.speaker_chat import stream_speaker_reply
from app.services.speaker_claims import extract_claims_for_source, retrieve_for_prompt
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

# Photo upload size limit: 5 MB
_MAX_PHOTO_BYTES = 5 * 1024 * 1024

# Allowlisted photo extensions
_ALLOWED_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


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
    grouped = await claims_repo.list_for_speaker(db, speaker_id, grouped_by_topic=True)
    return templates.TemplateResponse(
        request, "speaker.html",
        {"speaker": speaker, "sources": sources, "avatars": avatars.AVATARS,
         "grouped": grouped},
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
    speaker = await _owned_speaker(db, speaker_id, current_user_id)
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
        # Check extension against allowlist
        suffix = Path(photo.filename).suffix.lower()
        if suffix not in _ALLOWED_PHOTO_EXTENSIONS:
            raise HTTPException(400, "Unsupported image type")
        # Read and check size
        data = await photo.read()
        if len(data) > _MAX_PHOTO_BYTES:
            raise HTTPException(400, "Photo too large (max 5 MB)")
        # Write file
        dest_dir = Path(config.data_dir) / "speaker_photos"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{speaker_id}{suffix}"
        dest.write_bytes(data)
        # Clean up old photo if it has a different extension
        if speaker.avatar_photo_path:
            old_path = Path(speaker.avatar_photo_path)
            if old_path != dest and old_path.exists() and old_path.parent == dest.parent:
                # Ensure the old file belongs to this speaker (stem matches speaker_id)
                if old_path.stem == str(speaker_id):
                    try:
                        old_path.unlink()
                    except Exception:
                        # Cleanup failure must not break the upload
                        pass
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


# ---------------------------------------------------------------------------
# Task 7: Activate / deactivate (flip is_active flag)
# ---------------------------------------------------------------------------

def _chip_panel(request: Request, speaker) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_speaker_chip_panel.html", {"speaker": speaker}
    )


def _actions_panel(request: Request, speaker) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_speaker_actions.html", {"speaker": speaker}
    )


@router.post("/speaker/{speaker_id}/activate", response_class=HTMLResponse)
async def activate_speaker(
    request: Request,
    speaker_id: int,
    caller: str = "",
    video_id: str = "",
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    await _owned_speaker(db, speaker_id, current_user_id)
    await speakers_svc.activate(db, speaker_id)
    speaker = await sp_repo.get_speaker(db, speaker_id)
    if caller == "page":
        return _actions_panel(request, speaker)
    if caller == "chips" and video_id:
        video = await _owned_video(db, video_id, current_user_id)
        speakers = await ss_repo.list_for_source(db, video_id)
        return _chips_response(request, video, speakers)
    return _chip_panel(request, speaker)


@router.post("/speaker/{speaker_id}/deactivate", response_class=HTMLResponse)
async def deactivate_speaker(
    request: Request,
    speaker_id: int,
    caller: str = "",
    video_id: str = "",
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    await _owned_speaker(db, speaker_id, current_user_id)
    await sp_repo.set_active(db, speaker_id, False)
    speaker = await sp_repo.get_speaker(db, speaker_id)
    if caller == "page":
        return _actions_panel(request, speaker)
    if caller == "chips" and video_id:
        video = await _owned_video(db, video_id, current_user_id)
        speakers = await ss_repo.list_for_source(db, video_id)
        return _chips_response(request, video, speakers)
    return _chip_panel(request, speaker)


# ---------------------------------------------------------------------------
# Task 7 (PR 3): Persona chat routes
# ---------------------------------------------------------------------------

def _speaker_msg_html(role: str, content: str, *, avatar_id: str | None = None,
                      is_error: bool = False) -> str:
    """Persona chat bubble. User text escaped; assistant rendered as
    markdown. Assistant bubbles tinted with the speaker's avatar colour
    via an inline --avatar-bg var (see services/avatars.bg_color_for)."""
    if role == "user":
        return f'<div class="chat-bubble-user">{escape(content)}</div>'
    if is_error:
        return (f'<div class="chat-answer chat-msg-error">'
                f'<div class="chat-answer-content">{escape(content)}</div></div>')
    bg = avatars.bg_color_for(avatar_id or "")
    body = render_markdown(content)
    return (f'<div class="chat-answer chat-answer-speaker" '
            f'style="--avatar-bg: {bg}">'
            f'<div class="chat-answer-content">{body}</div></div>')


async def _resolve_model(db, llm_model_id: str):
    chosen_id: int | None = None
    if llm_model_id.strip():
        try:
            chosen_id = int(llm_model_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid llm_model_id: {e}") from None
    row = (await llm_models_repo.get(db, chosen_id) if chosen_id is not None
           else await llm_models_repo.get_default(db))
    if row is None:
        raise HTTPException(400, "LLM not configured")
    return row.model, (row.api_key or ""), (row.base_url or None)


async def _run_persona_turn(
    db, *, speaker, source_context: str, content: str, thread_id: int,
    video_id: str | None, model: str, api_key: str, base_url: str | None,
    seed_ts: str | None, seed_quote: str | None,
) -> str:
    # video_id is the episode id for per-episode turns, None for whole-dossier
    # (scope='speaker') turns. PR 1 made chat_messages.video_id nullable; when
    # thread_id is set, history() selects by thread_id and ignores video_id.
    claims = await retrieve_for_prompt(db, speaker.id, query=content, limit=12)
    history = await chat_repo.history(db, video_id, thread_id=thread_id)
    await chat_repo.append(db, None, "user", content,
                           user_id=speaker.user_id, thread_id=thread_id)
    collected: list[str] = []
    error: str | None = None
    try:
        async for tok in stream_speaker_reply(
            speaker=speaker, source_context=source_context, claims=claims,
            history=history, user_message=content, seed_ts=seed_ts,
            seed_quote=seed_quote, model=model, api_key=api_key, base_url=base_url,
        ):
            collected.append(tok)
    except Exception as e:  # noqa: BLE001 — surface as an error bubble
        error = f"{type(e).__name__}: {e}"
    answer = "".join(collected)
    await chat_repo.append(
        db, None, "assistant", answer if answer else f"[error: {error}]",
        user_id=speaker.user_id, thread_id=thread_id)
    parts = [_speaker_msg_html("user", content)]
    if answer:
        parts.append(_speaker_msg_html("assistant", answer, avatar_id=speaker.avatar_id))
    if error:
        parts.append(_speaker_msg_html("assistant", error, is_error=True))
    elif not answer:
        parts.append(_speaker_msg_html("assistant", "(empty response from model)", is_error=True))
    return "".join(parts)


@router.post("/v/{video_id}/speaker/{speaker_id}/chat", response_class=HTMLResponse)
async def post_speaker_chat(
    video_id: str, speaker_id: int,
    content: str = Form(...), llm_model_id: str = Form(""),
    seed_ts: str = Form(""), seed_quote: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await videos_repo.get(db, video_id)
    speaker = await sp_repo.get_speaker(db, speaker_id)
    if video is None or speaker is None:
        raise HTTPException(404, "Not found")
    if video.user_id != current_user_id or speaker.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    model, api_key, base_url = await _resolve_model(db, llm_model_id)
    thread_id = await threads_repo.get_or_create(
        db, user_id=current_user_id, scope="source_speaker",
        source_id=video_id, speaker_id=speaker_id)
    html = await _run_persona_turn(
        db, speaker=speaker, source_context=(video.transcript or ""),
        content=content, thread_id=thread_id, video_id=video_id,
        model=model, api_key=api_key, base_url=base_url,
        seed_ts=seed_ts.strip() or None, seed_quote=seed_quote.strip() or None)
    return HTMLResponse(html)


@router.post("/speaker/{speaker_id}/chat", response_class=HTMLResponse)
async def post_dossier_chat(
    speaker_id: int,
    content: str = Form(...), llm_model_id: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await sp_repo.get_speaker(db, speaker_id)
    if speaker is None or speaker.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    model, api_key, base_url = await _resolve_model(db, llm_model_id)
    thread_id = await threads_repo.get_or_create(
        db, user_id=current_user_id, scope="speaker", speaker_id=speaker_id)
    # Whole-dossier chat has no single episode → no transcript context.
    html = await _run_persona_turn(
        db, speaker=speaker, source_context="", content=content,
        thread_id=thread_id, video_id=None, model=model, api_key=api_key,
        base_url=base_url, seed_ts=None, seed_quote=None)
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Task 8 (PR 3): On-demand extract + claim edit/review routes
# ---------------------------------------------------------------------------

async def _claims_fragment(db, request: Request, speaker) -> str:
    """Render the topic-grouped dossier partial for HTMX swap responses."""
    grouped = await claims_repo.list_for_speaker(db, speaker.id, grouped_by_topic=True)
    return templates.get_template("_speaker_claims.html").render(
        request=request, speaker=speaker, grouped=grouped)


async def _owned_claim(
    db: aiosqlite.Connection,
    speaker_id: int,
    claim_id: int,
):
    """Load a claim and verify it belongs to speaker_id — else 404.

    Complements the profile-ownership gate (speaker.user_id check) already
    done before calling this helper. Finding 6: within one profile a
    hand-crafted URL /speaker/{B}/claims/{A}/... could otherwise mutate
    speaker A's claim under speaker B's page.
    """
    claim_row = await claims_repo.get(db, claim_id)
    if claim_row is None or claim_row.speaker_id != speaker_id:
        raise HTTPException(404, "Not found")
    return claim_row


@router.post("/speaker/{speaker_id}/sources/{source_id}/extract",
             response_class=HTMLResponse)
async def post_extract_source(
    speaker_id: int, source_id: str, request: Request,
    llm_model_id: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await sp_repo.get_speaker(db, speaker_id)
    source = await videos_repo.get(db, source_id)
    if speaker is None or source is None:
        raise HTTPException(404, "Not found")
    if speaker.user_id != current_user_id or source.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    model, api_key, base_url = await _resolve_model(db, llm_model_id)
    await extract_claims_for_source(
        db, source, [speaker_id], model=model, api_key=api_key, base_url=base_url)
    return HTMLResponse(await _claims_fragment(db, request, speaker))


@router.post("/speaker/{speaker_id}/claims/{claim_id}/review",
             response_class=HTMLResponse)
async def post_claim_review(
    speaker_id: int, claim_id: int, request: Request,
    status: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await sp_repo.get_speaker(db, speaker_id)
    if speaker is None or speaker.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    # Finding 6: also verify the claim belongs to this speaker (cross-speaker check).
    await _owned_claim(db, speaker_id, claim_id)
    if status not in ("unreviewed", "accepted", "rejected"):
        raise HTTPException(400, "bad status")
    await claims_repo.set_review_status(db, claim_id, status)
    return HTMLResponse(await _claims_fragment(db, request, speaker))


@router.post("/speaker/{speaker_id}/claims/{claim_id}/edit",
             response_class=HTMLResponse)
async def post_claim_edit(
    speaker_id: int, claim_id: int, request: Request,
    claim: str = Form(""), topic: str = Form(""),
    evidence_text: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await sp_repo.get_speaker(db, speaker_id)
    if speaker is None or speaker.user_id != current_user_id:
        raise HTTPException(404, "Not found")
    # Finding 6: also verify the claim belongs to this speaker (cross-speaker check).
    await _owned_claim(db, speaker_id, claim_id)
    fields: dict = {}
    if claim.strip():
        fields["claim"] = claim.strip()
    if topic.strip():
        fields["topic"] = topic.strip()
    if evidence_text.strip():
        fields["evidence_text"] = evidence_text.strip()
    await claims_repo.edit_claim(db, claim_id, **fields)
    return HTMLResponse(await _claims_fragment(db, request, speaker))
