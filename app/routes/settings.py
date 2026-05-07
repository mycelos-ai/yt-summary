import asyncio

import aiosqlite
import httpx
import litellm
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_db
from app.repos import settings as settings_repo
from app.repos import users as users_repo
from app.services.auth import generate_api_key as _gen_key
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
    has_api_key = bool(settings.get("llm_api_key"))
    has_whisper_key = bool(settings.get("whisper_api_key"))
    safe_settings = {
        k: v for k, v in settings.items()
        if k not in ("llm_api_key", "whisper_api_key")
    }
    user = await users_repo.get_default_user(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": safe_settings,
            "has_api_key": has_api_key,
            "has_whisper_key": has_whisper_key,
            "has_cookies": has_cookies,
            "api_key_prefix": user.api_key_prefix if user else None,
            "api_key_created_at": user.api_key_created_at if user else None,
        },
    )


@router.post("/settings")
async def save_settings(
    llm_model: str = Form(""),
    llm_api_key: str = Form(""),
    llm_base_url: str = Form(""),
    whisper_model: str = Form("small"),
    whisper_base_url: str = Form(""),
    whisper_api_key: str = Form(""),
    summary_language: str = Form("auto"),
    summary_extra_instructions: str = Form(""),
    embedding_model: str = Form(""),
    embedding_base_url: str = Form(""),
    playlist_refresh_interval_hours: str = Form("1"),
    playlist_initial_import_limit: str = Form("20"),
    db: aiosqlite.Connection = Depends(get_db),
):
    # LiteLLM and httpx clients append paths to base; a trailing "/"
    # would produce "//audio/transcriptions" or "//api/chat" which some
    # providers reject with 405.
    llm_base_url = llm_base_url.strip().rstrip("/")
    embedding_base_url = embedding_base_url.strip().rstrip("/")
    whisper_base_url = whisper_base_url.strip().rstrip("/")
    for key, value in (
        ("llm_model", llm_model.strip()),
        ("llm_base_url", llm_base_url),
        ("whisper_model", whisper_model.strip() or "small"),
        ("whisper_base_url", whisper_base_url),
        ("summary_language", summary_language.strip() or "auto"),
        ("summary_extra_instructions", summary_extra_instructions.strip()),
        ("embedding_model", embedding_model.strip()),
        ("embedding_base_url", embedding_base_url),
        ("playlist_refresh_interval_hours", playlist_refresh_interval_hours.strip()),
        ("playlist_initial_import_limit", playlist_initial_import_limit.strip()),
    ):
        if value:
            await settings_repo.set(db, key, value)
        else:
            await settings_repo.delete(db, key)
    if llm_api_key:
        await settings_repo.set(db, "llm_api_key", llm_api_key)
    if whisper_api_key:
        await settings_repo.set(db, "whisper_api_key", whisper_api_key)
    return RedirectResponse("/settings", status_code=303)


async def _probe_ollama_reachable(base_url: str) -> str | None:
    """Return None if Ollama answers /api/tags, else a human error string."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/tags")
            if r.status_code != 200:
                return f"HTTP {r.status_code} from {base_url}/api/tags"
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


@router.post("/settings/test-llm", response_class=HTMLResponse)
async def test_llm(db: aiosqlite.Connection = Depends(get_db)):
    settings = await settings_repo.get_all(db)
    model = settings.get("llm_model")
    if not model:
        return HTMLResponse(
            '<p class="status status-failed">⚠ Configure a model first.</p>'
        )
    api_key = settings.get("llm_api_key")
    base_url = settings.get("llm_base_url")

    # For Ollama, do a direct reachability probe first so we surface a clear
    # error instead of a confusing LiteLLM/aiohttp wrapper message.
    if base_url and model.startswith(("ollama/", "ollama_chat/")):
        err = await _probe_ollama_reachable(base_url)
        if err is not None:
            return HTMLResponse(
                f'<p class="status status-failed">⚠ Cannot reach Ollama at '
                f'{base_url}: {err}</p>'
            )

    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "api_key": api_key or "",
        "max_tokens": 10,
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
        return HTMLResponse(
            f'<p class="status status-failed">⚠ {type(e).__name__}: {e}</p>'
        )


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


@router.post("/settings/api-key/generate", response_class=HTMLResponse)
async def generate_api_key_route(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    plaintext, key_hash, prefix = _gen_key()
    await users_repo.set_api_key(
        db, user_id=1, key_hash=key_hash, key_prefix=prefix
    )
    return templates.TemplateResponse(
        request,
        "api_key_reveal.html",
        {"plaintext": plaintext, "prefix": prefix},
    )


@router.post("/settings/api-key/revoke")
async def revoke_api_key_route(
    db: aiosqlite.Connection = Depends(get_db),
):
    await users_repo.clear_api_key(db, user_id=1)
    return RedirectResponse("/settings", status_code=303)
