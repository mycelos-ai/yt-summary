"""First-time-user onboarding wizard.

Four full-page steps — welcome, provider, profile, first-content — each
with a Skip link in the header. POST handlers redirect through the flow
and write the bare minimum: an LLM provider preset on step 2, the
profile name + avatar on step 3, and the ``onboarding_completed`` flag
on Skip / Finish.

Step 2 (provider) writes directly into ``llm_models`` via
:func:`app.repos.llm_models.insert` with ``make_default=True``. The
legacy ``settings.llm_model`` / ``settings.llm_api_key`` path was
removed in the multi-model migration. Whisper config still lives in the
settings table and is written directly when the preset carries a Whisper
endpoint. For profile updates we hit the existing
:func:`app.repos.users.update`.

Skip semantics: users can bail out at any step. Both ``/onboarding/skip``
and ``/onboarding/finish`` set the same flag — the difference is purely
intent (the user told us what they did vs. didn't do during the flow).
Either way, we don't ambush them with the wizard again.
"""

import aiosqlite
import litellm
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.main import get_current_user, get_current_user_id, get_db
from app.repos import llm_models as llm_models_repo
from app.repos import settings as settings_repo
from app.repos import users as users_repo
from app.services import avatars as avatars_service
from app.services.providers import PROVIDER_PRESETS
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

# YouTube id of the embedded promo video on the welcome step. Edit
# in one place if we ever cut a new intro reel.
PROMO_VIDEO_ID = "wUkqSNn63Hk"


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
    # When the user re-enters the wizard with a default model already
    # configured, pre-select that provider's tile. The current_default
    # row's provider_id is the source of truth; the legacy
    # settings.llm_model key was removed in the multi-model migration.
    current_default = await llm_models_repo.get_default(db)
    selected = current_default.provider_id if current_default else ""
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
            "current_default": current_default,
        },
    )


@router.post("/onboarding/provider")
async def provider_submit(
    provider: str = Form(...),
    api_key: str = Form(""),
    llm_model: str = Form(""),
    llm_base_url: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Apply the selected provider preset by writing a single row
    into llm_models with make_default=True.

    On re-enters (user comes back to the wizard step with a default
    already configured), update that row in place instead of creating
    a second default — keeps the wizard's "one configured model"
    mental model intact.

    Whisper config is preserved separately: if the preset hosts a
    Whisper-compatible endpoint, those keys still go through
    settings_repo (Whisper isn't part of the multi-model migration).
    The LLM keys go straight into llm_models.
    """
    if provider not in PROVIDER_PRESETS:
        # Unknown provider id — treat like a skip rather than 400-ing.
        # The user can always come back to /settings to pick again.
        return RedirectResponse("/onboarding/profile", status_code=303)

    preset = PROVIDER_PRESETS[provider]
    chosen_model = (llm_model.strip() or preset.default_llm).strip()
    chosen_base_url = (
        llm_base_url.strip().rstrip("/")
        or preset.default_llm_base_url
        or ""
    )

    existing_default = await llm_models_repo.get_default(db)
    # If the user is re-entering the wizard with the same provider
    # already configured (Back+Continue scenario), update the row in
    # place. provider_id is immutable in the repo, so a provider switch
    # requires inserting a fresh row and discarding the old one.
    # The api_key behaviour mirrors the Settings edit flow: a blank
    # form value keeps whatever's already stored.
    if existing_default is not None and existing_default.provider_id == provider:
        effective_key = api_key.strip() or existing_default.api_key
        await llm_models_repo.update(
            db, existing_default.id,
            label=preset.name,
            model=chosen_model,
            api_key=effective_key,
            base_url=chosen_base_url,
        )
    else:
        # Fresh install, or the user switched to a different provider.
        # insert(make_default=True) clears the default flag on any
        # existing row first, so after the insert the old row is safe
        # to delete (it is no longer the default).
        old_id = existing_default.id if existing_default is not None else None
        await llm_models_repo.insert(
            db,
            label=preset.name,
            provider_id=provider,
            model=chosen_model,
            api_key=api_key.strip(),
            base_url=chosen_base_url,
            make_default=True,
        )
        if old_id is not None:
            await llm_models_repo.delete(db, old_id)

    # Whisper settings still live in the settings table (Whisper is
    # not part of the multi-model migration). Write them directly when
    # the preset carries a Whisper endpoint.
    if preset.whisper_base_url:
        await settings_repo.set(db, "whisper_base_url", preset.whisper_base_url)
        await settings_repo.set(db, "whisper_model", preset.whisper_model)
        if api_key.strip():
            await settings_repo.set(db, "whisper_api_key", api_key.strip())

    return RedirectResponse("/onboarding/profile", status_code=303)


@router.post("/onboarding/test-provider", response_class=HTMLResponse)
async def test_provider(
    provider: str = Form(...),
    api_key: str = Form(""),
    llm_model: str = Form(""),
    llm_base_url: str = Form(""),
):
    """Pre-flight LLM round-trip on the wizard's unsaved form values.

    Same shape as :func:`app.routes.settings.test_llm` but operates on
    the form payload directly — we don't write settings before
    Continue. Returns an HTMX-friendly fragment (200 even on failure;
    the body carries the ✓/✗).
    """
    if provider not in PROVIDER_PRESETS:
        return HTMLResponse(
            '<p class="status status-failed">⚠ Unknown provider.</p>'
        )
    preset = PROVIDER_PRESETS[provider]

    model = llm_model.strip() or preset.default_llm
    base_url = (
        llm_base_url.strip().rstrip("/")
        or preset.default_llm_base_url
        or ""
    )

    # Use kwargs as Any-typed dict so litellm's overloaded signature
    # doesn't trip pyright. The runtime call is unchanged.
    from typing import Any
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Reply OK in one word."}
        ],
        "api_key": api_key.strip() or "",
        "max_tokens": 5,
    }
    if base_url:
        kwargs["api_base"] = base_url

    try:
        response = await litellm.acompletion(**kwargs)
        text = (response.choices[0].message.content or "").strip()
        return HTMLResponse(
            f'<p class="status status-done">✓ {model} responded: '
            f'{text[:50] or "(empty)"}</p>'
        )
    except Exception as e:
        # First 100 chars of message keeps the fragment compact even
        # when LiteLLM hands back a multi-line traceback string.
        msg = str(e)[:100]
        return HTMLResponse(
            f'<p class="status status-failed">⚠ {type(e).__name__}: {msg}</p>'
        )


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


# Both finish and skip drop the user on the home page with an
# onboarding-done banner. /settings would land them on a page they
# don't immediately need (and have no obvious back-button from), so
# we lean the other direction: home is the primary tool, settings
# is one click away when they actually need to tweak something.
_ONBOARDING_DONE_REDIRECT = "/?onboarding=done"


@router.post("/onboarding/finish")
async def finish(db: aiosqlite.Connection = Depends(get_db)):
    await _mark_completed(db)
    return RedirectResponse(_ONBOARDING_DONE_REDIRECT, status_code=303)


@router.post("/onboarding/skip")
async def skip(db: aiosqlite.Connection = Depends(get_db)):
    await _mark_completed(db)
    return RedirectResponse(_ONBOARDING_DONE_REDIRECT, status_code=303)
