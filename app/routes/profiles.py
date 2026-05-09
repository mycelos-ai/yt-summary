"""Profile-management routes.

Netflix-style multi-profile picker. Each profile owns its own video
library, playlists, chat history, custom summary prompt, and avatar
emoji. Provider/model/API key settings stay global.

No auth on these routes by design — see the spec. This is a family
tool on a single Pi5; anyone with browser access to the box can switch
between profiles.
"""

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.main import (
    PROFILE_COOKIE,
    get_current_user,
    get_current_user_id,
    get_db,
)
from app.repos import users as users_repo
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)


@router.get("/profiles", response_class=HTMLResponse)
async def profiles_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user=Depends(get_current_user),
):
    profiles = await users_repo.list_all(db)
    return templates.TemplateResponse(
        request,
        "profiles.html",
        {
            "profiles": profiles,
            "current_user": current_user,
        },
    )


@router.post("/profiles/switch")
async def profiles_switch(
    user_id: int = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    user = await users_repo.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(404, detail="Profile not found")
    response = RedirectResponse("/", status_code=303)
    # 1 year, plenty for "remember which profile I'm on" — the cookie
    # is harmless if it expires (we fall back to id=1 silently).
    response.set_cookie(
        PROFILE_COOKIE,
        str(user_id),
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/profiles/new", response_class=HTMLResponse)
async def profile_new_form(
    request: Request,
    current_user=Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
        "profile_form.html",
        {
            "profile": None,
            "current_user": current_user,
            "form_action": "/profiles/new",
            "submit_label": "Create profile",
        },
    )


@router.post("/profiles/new")
async def profile_new(
    name: str = Form(...),
    avatar_emoji: str = Form("👤"),
    custom_summary_prompt: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    # New profiles inherit the active profile's prompt. The user can
    # then edit it freely on the new profile. This way "create a
    # profile for my son" doesn't dump them in front of a blank
    # textarea — they start from whatever is working for me, and
    # tweak from there. (If the form was filled in explicitly, we
    # respect that override.)
    submitted_prompt = custom_summary_prompt.strip()
    if not submitted_prompt:
        active = await users_repo.get_by_id(db, current_user_id)
        if active and active.custom_summary_prompt:
            submitted_prompt = active.custom_summary_prompt
    try:
        user = await users_repo.create(
            db,
            name=name,
            avatar_emoji=avatar_emoji or "👤",
            custom_summary_prompt=submitted_prompt or None,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        PROFILE_COOKIE,
        str(user.id),
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/profiles/{user_id}/edit", response_class=HTMLResponse)
async def profile_edit_form(
    user_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user=Depends(get_current_user),
):
    profile = await users_repo.get_by_id(db, user_id)
    if profile is None:
        raise HTTPException(404, detail="Profile not found")
    return templates.TemplateResponse(
        request,
        "profile_form.html",
        {
            "profile": profile,
            "current_user": current_user,
            "form_action": f"/profiles/{user_id}/edit",
            "submit_label": "Save changes",
        },
    )


@router.post("/profiles/{user_id}/edit")
async def profile_edit(
    user_id: int,
    name: str = Form(...),
    avatar_emoji: str = Form("👤"),
    custom_summary_prompt: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    profile = await users_repo.get_by_id(db, user_id)
    if profile is None:
        raise HTTPException(404, detail="Profile not found")

    # An empty submitted value means "reset to the standard prompt" —
    # the textarea on the form is pre-filled with the current prompt,
    # so clearing it is an explicit reset gesture, not "I forgot to
    # type anything." Re-seed with the standard prompt so the field
    # is never NULL at runtime.
    submitted = custom_summary_prompt.strip()
    if not submitted:
        from app.services.summarizer import build_system_prompt
        submitted = build_system_prompt(language=None)

    try:
        await users_repo.update(
            db,
            user_id,
            name=name,
            avatar_emoji=avatar_emoji or "👤",
            custom_summary_prompt=submitted,
            custom_summary_prompt_set=True,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    return RedirectResponse("/profiles", status_code=303)


@router.post("/profiles/{user_id}/delete")
async def profile_delete(
    user_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    profile = await users_repo.get_by_id(db, user_id)
    if profile is None:
        raise HTTPException(404, detail="Profile not found")

    profiles = await users_repo.list_all(db)
    if len(profiles) <= 1:
        raise HTTPException(
            400, detail="Cannot delete the last remaining profile."
        )

    # Figure out where to redirect the active session if we're deleting
    # the cookie's profile. We have to read the cookie before the
    # response is built so the new cookie value reflects "fall back to 1"
    # (or some other surviving profile).
    raw_cookie = request.cookies.get(PROFILE_COOKIE)
    try:
        active_id = int(raw_cookie) if raw_cookie else 1
    except (TypeError, ValueError):
        active_id = 1

    await users_repo.delete(db, user_id)

    response = RedirectResponse("/profiles", status_code=303)
    if active_id == user_id:
        # Switch to the seeded profile (or the first surviving one if 1
        # was the deleted profile, though we generally protect 1 by
        # convention — the spec doesn't forbid deleting it explicitly,
        # so handle it anyway).
        survivors = await users_repo.list_all(db)
        target = next((p.id for p in survivors if p.id == 1), None)
        if target is None and survivors:
            target = survivors[0].id
        if target is not None:
            response.set_cookie(
                PROFILE_COOKIE,
                str(target),
                max_age=60 * 60 * 24 * 365,
                httponly=True,
                samesite="lax",
            )
    return response
