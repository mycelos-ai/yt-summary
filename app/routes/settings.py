import asyncio
import time
from pathlib import Path

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
from app.services.embeddings import embed_text
from app.services.whisper import transcribe, transcribe_via_api
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

# Bundled audio sample for /settings/test-whisper. Short clip of "This
# is a test" so the round-trip stays cheap on a Pi5.
WHISPER_TEST_SAMPLE = Path("app/static/samples/whisper_test.m4a")


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


@router.post("/settings/test-whisper", response_class=HTMLResponse)
async def test_whisper(db: aiosqlite.Connection = Depends(get_db)):
    """Round-trip the bundled audio sample through whatever Whisper
    backend is configured. Local path uses faster-whisper, API path
    uses transcribe_via_api()."""
    if not await asyncio.to_thread(WHISPER_TEST_SAMPLE.exists):
        return HTMLResponse(
            '<p class="status status-failed">⚠ Test sample not found '
            f'at {WHISPER_TEST_SAMPLE}.</p>'
        )

    settings = await settings_repo.get_all(db)
    model = settings.get("whisper_model") or "small"
    base_url = (settings.get("whisper_base_url") or "").strip()
    api_key = settings.get("whisper_api_key") or ""

    started = time.monotonic()
    try:
        if base_url:
            text = await transcribe_via_api(
                WHISPER_TEST_SAMPLE,
                base_url=base_url,
                api_key=api_key,
                model_name=model,
            )
            backend = f"{base_url} ({model})"
        else:
            text = await asyncio.to_thread(
                transcribe, WHISPER_TEST_SAMPLE, model
            )
            backend = f"local faster-whisper ({model})"
    except Exception as e:
        return HTMLResponse(
            f'<p class="status status-failed">⚠ {type(e).__name__}: {e}</p>'
        )
    elapsed = time.monotonic() - started
    snippet = (text or "(empty)")[:120]
    return HTMLResponse(
        f'<p class="status status-done">✓ {backend} '
        f'transcribed in {elapsed:.1f}s: <em>{snippet}</em></p>'
    )


@router.post("/settings/test-embedding", response_class=HTMLResponse)
async def test_embedding(db: aiosqlite.Connection = Depends(get_db)):
    """Embed a fixed short string through the configured embedding
    backend. Reports the vector dimension and timing."""
    settings = await settings_repo.get_all(db)
    model = (settings.get("embedding_model") or "").strip()
    # embed_text falls back to ollama/nomic-embed-text but only makes
    # sense if there's *some* base URL it can hit.
    has_base = bool(
        settings.get("embedding_base_url") or settings.get("llm_base_url")
    )
    if not model and not has_base:
        return HTMLResponse(
            '<p class="status status-failed">⚠ Configure an embedding '
            'model (or an LLM Base URL to fall back on).</p>'
        )

    embedding_base = (
        settings.get("embedding_base_url") or settings.get("llm_base_url") or None
    )
    embedding_key = settings.get("llm_api_key") or ""
    started = time.monotonic()
    try:
        vec = await embed_text(
            "yt-summary embedding test",
            model=model or None,
            api_key=embedding_key,
            base_url=embedding_base,
        )
    except Exception as e:
        return HTMLResponse(
            f'<p class="status status-failed">⚠ {type(e).__name__}: {e}</p>'
        )
    elapsed = time.monotonic() - started
    dim = len(vec) if vec else 0
    if dim == 0:
        return HTMLResponse(
            '<p class="status status-failed">⚠ Embedding returned an '
            'empty vector.</p>'
        )
    preview = ", ".join(f"{v:.3f}" for v in vec[:3])
    label = model or "(default ollama/nomic-embed-text)"
    return HTMLResponse(
        f'<p class="status status-done">✓ {label} returned a '
        f'{dim}-dim vector in {elapsed:.2f}s. First values: '
        f'[{preview}, …]</p>'
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
