import asyncio

import aiosqlite
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_db
from app.repos import settings as settings_repo
from app.services.curl_parser import extract_cookies, write_netscape_cookies

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    settings = await settings_repo.get_all(db)
    has_cookies = await asyncio.to_thread(config.cookies_path.exists)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"settings": settings, "has_cookies": has_cookies},
    )


@router.post("/settings")
async def save_settings(
    llm_model: str = Form(""),
    llm_api_key: str = Form(""),
    llm_base_url: str = Form(""),
    whisper_model: str = Form("small"),
    db: aiosqlite.Connection = Depends(get_db),
):
    for key, value in (
        ("llm_model", llm_model),
        ("llm_api_key", llm_api_key),
        ("llm_base_url", llm_base_url),
        ("whisper_model", whisper_model),
    ):
        if value:
            await settings_repo.set(db, key, value)
        else:
            await settings_repo.delete(db, key)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/youtube-curl")
async def save_curl(
    curl: str = Form(...),
    config: Config = Depends(get_config),
):
    cookies = extract_cookies(curl)
    if not cookies:
        return RedirectResponse("/settings", status_code=303)
    await asyncio.to_thread(
        write_netscape_cookies, cookies, domain=".youtube.com", target=config.cookies_path
    )
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/youtube-curl/clear")
async def clear_curl(config: Config = Depends(get_config)):
    if await asyncio.to_thread(config.cookies_path.exists):
        await asyncio.to_thread(config.cookies_path.unlink)
    return RedirectResponse("/settings", status_code=303)
