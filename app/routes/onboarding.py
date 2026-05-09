"""First-time-user onboarding wizard.

Four full-page steps — welcome, provider, profile, first-content — each
with a Skip link in the header. POST handlers redirect through the flow
and write the bare minimum: an LLM provider preset on step 2, the
profile name + avatar on step 3, and the ``onboarding_completed`` flag
on Skip / Finish.

The wizard reuses :func:`app.services.providers.apply_preset` so it
can never drift from the Quick-Setup wizard's setting-key contract.
For provider settings we delegate; for profile updates we hit the
existing :func:`app.repos.users.update`.

Skip semantics: users can bail out at any step. Both ``/onboarding/skip``
and ``/onboarding/finish`` set the same flag — the difference is purely
intent (the user told us what they did vs. didn't do during the flow).
Either way, we don't ambush them with the wizard again.
"""

import aiosqlite
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.main import get_current_user, get_current_user_id, get_db
from app.repos import settings as settings_repo
from app.repos import users as users_repo
from app.services import avatars as avatars_service
from app.services.providers import PROVIDER_PRESETS, apply_preset
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

# YouTube id of the embedded promo video on the welcome step. Edit
# in one place if we ever cut a new intro reel.
PROMO_VIDEO_ID = "wUkqSNn63Hk"


def _detect_active_provider(llm_model: str) -> str:
    """Best-effort: pick the preset whose default model id has the same
    provider prefix as the user's saved ``llm_model``.

    Used by the provider step to pre-select the right tile when a user
    re-enters the wizard with settings already saved. Falls back to
    "" so the form starts unselected for genuine first-run users.
    """
    if not llm_model:
        return ""
    # Compare on the litellm prefix (the part before the first slash).
    head = llm_model.split("/", 1)[0]
    for preset in PROVIDER_PRESETS.values():
        # ollama_chat/* and ollama/* both belong to the ollama tile.
        if head == preset.litellm_provider or head.startswith(
            preset.litellm_provider
        ):
            return preset.id
    return ""


# ── Step 1: welcome ────────────────────────────────────────────────


@router.get("/onboarding/welcome", response_class=HTMLResponse)
async def welcome(
    request: Request,
    current_user=Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
        "onboarding/welcome.html",
        {
            "current_user": current_user,
            "step": 1,
            "total_steps": 4,
            "promo_video_id": PROMO_VIDEO_ID,
        },
    )


# ── Step 2: provider ───────────────────────────────────────────────


@router.get("/onboarding/provider", response_class=HTMLResponse)
async def provider_form(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user=Depends(get_current_user),
):
    settings = await settings_repo.get_all(db)
    selected = _detect_active_provider(settings.get("llm_model", ""))
    return templates.TemplateResponse(
        request,
        "onboarding/provider.html",
        {
            "current_user": current_user,
            "step": 2,
            "total_steps": 4,
            "presets": list(PROVIDER_PRESETS.values()),
            "settings": settings,
            "selected_provider": selected,
        },
    )


@router.post("/onboarding/provider")
async def provider_submit(
    provider: str = Form(...),
    api_key: str = Form(""),
    llm_base_url: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Apply the selected provider preset. Same write-set as the
    Quick-Setup wizard via :func:`apply_preset` — keep them aligned by
    delegation, not duplication.
    """
    if provider not in PROVIDER_PRESETS:
        # Unknown provider id — treat like a skip rather than 400-ing.
        # The user can always come back to /settings to pick again.
        return RedirectResponse("/onboarding/profile", status_code=303)

    current = await settings_repo.get_all(db)
    updates = apply_preset(
        provider_id=provider,
        api_key=api_key.strip(),
        current_settings=current,
        llm_base_url_override=llm_base_url.strip() or None,
    )
    for key, value in updates.items():
        if value:
            await settings_repo.set(db, key, value)
        else:
            await settings_repo.delete(db, key)
    return RedirectResponse("/onboarding/profile", status_code=303)


# ── Step 3: profile ────────────────────────────────────────────────


@router.get("/onboarding/profile", response_class=HTMLResponse)
async def profile_form(
    request: Request,
    current_user=Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
        "onboarding/profile.html",
        {
            "current_user": current_user,
            "step": 3,
            "total_steps": 4,
            "avatar_groups": avatars_service.grouped(),
        },
    )


@router.post("/onboarding/profile")
async def profile_submit(
    name: str = Form(...),
    avatar_image: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    img = avatar_image.strip()
    if img and not avatars_service.is_valid_id(img):
        # Same defensive pattern as profile_edit — ignore unknown ids
        # so the column never holds a path-traversal payload.
        img = ""
    try:
        await users_repo.update(
            db,
            current_user_id,
            name=name,
            avatar_image=img,
        )
    except ValueError:
        # Empty name etc. — don't block the wizard, just bounce back.
        return RedirectResponse("/onboarding/profile", status_code=303)
    return RedirectResponse("/onboarding/first-content", status_code=303)


# ── Step 4: first content ──────────────────────────────────────────


@router.get("/onboarding/first-content", response_class=HTMLResponse)
async def first_content(
    request: Request,
    current_user=Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
        "onboarding/first_content.html",
        {
            "current_user": current_user,
            "step": 4,
            "total_steps": 4,
        },
    )


# ── Finish / Skip ──────────────────────────────────────────────────


async def _mark_completed(db: aiosqlite.Connection) -> None:
    await settings_repo.set(db, "onboarding_completed", "1")


@router.post("/onboarding/finish")
async def finish(db: aiosqlite.Connection = Depends(get_db)):
    await _mark_completed(db)
    return RedirectResponse("/settings?onboarding=done", status_code=303)


@router.post("/onboarding/skip")
async def skip(db: aiosqlite.Connection = Depends(get_db)):
    await _mark_completed(db)
    return RedirectResponse("/settings?onboarding=done", status_code=303)
