import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import httpx
import litellm
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_current_user, get_current_user_id, get_db
from app.repos import embeddings as embeddings_repo
from app.repos import jobs as jobs_repo
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.repos import tts_jobs as tts_jobs_repo
from app.repos import users as users_repo
from app.services.auth import generate_api_key as _gen_key
from app.services.curl_parser import extract_cookies, write_netscape_cookies
from app.services.providers import (
    PROVIDER_PRESETS,
    apply_preset,
    fetch_ollama_models,
    list_chat_models,
    split_ollama_tags,
)
from app.services.tts_voices import LANGUAGES as _TTS_VOICE_LANGUAGES
from app.services.tts_voices import voices_for_language
from app.services.whisper import transcribe, transcribe_via_api
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)

# Bundled audio sample for /settings/test-whisper. Short clip of "This
# is a test" so the round-trip stays cheap on a Pi5.
WHISPER_TEST_SAMPLE = Path("app/static/samples/whisper_test.m4a")

# Languages exposed in the Audio (TTS) settings card. The (code, label)
# pairs live in app/services/tts_voices.py so the audio modal and
# settings card share the same flag-prefixed labels; here we just
# prepend the "Auto" sentinel that's specific to the settings card.
_TTS_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("auto",  "Auto (use video's language)"),
    *_TTS_VOICE_LANGUAGES,
)
_TTS_VOICE_LANGS: tuple[str, ...] = tuple(
    code for code, _ in _TTS_VOICE_LANGUAGES
)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    applied: str | None = None,
    onboarding: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
    current_user=Depends(get_current_user),
):
    settings = await settings_repo.get_all(db)
    has_cookies = await asyncio.to_thread(config.cookies_path.exists)
    # Surface a sensible default in the UI: if only the legacy
    # `_hours` setting is present, render it as the equivalent minutes
    # so the user sees a meaningful number instead of an empty field.
    if (
        "playlist_refresh_interval_minutes" not in settings
        and "playlist_refresh_interval_hours" in settings
    ):
        try:
            hours = float(settings["playlist_refresh_interval_hours"])
            settings["playlist_refresh_interval_minutes"] = str(int(hours * 60))
        except ValueError:
            pass
    scheduled_playlists = await playlists_repo.list_for_user(db, 1)
    has_api_key = bool(settings.get("llm_api_key"))
    has_whisper_key = bool(settings.get("whisper_api_key"))
    safe_settings = {
        k: v for k, v in settings.items()
        if k not in ("llm_api_key", "whisper_api_key")
    }
    # API keys live on user_id=1 (the seeded admin) regardless of which
    # profile is active — they're a household credential, not a per-
    # profile thing. Pull from there for the API access card.
    user = await users_repo.get_default_user(db)

    # Build preset dropdown data for the Quick Setup wizard.
    presets = list(PROVIDER_PRESETS.values())
    applied_preset = None
    if applied and applied in PROVIDER_PRESETS:
        applied_preset = PROVIDER_PRESETS[applied]

    # Detect which provider is currently active so the wizard can
    # pre-select that tile and show the matching detail panel
    # without the user having to remember what they configured.
    # Falls back to '' for fresh installs (no llm_model yet) and
    # for custom/manual setups whose llm_model prefix doesn't match
    # any preset.
    current_provider_id = ""
    current_model = settings.get("llm_model", "")
    if current_model:
        head = current_model.split("/", 1)[0]
        for p in presets:
            if head == p.litellm_provider or head.startswith(p.litellm_provider):
                current_provider_id = p.id
                break

    # Per-provider chat model lists for cloud providers.
    # Ollama gets its list dynamically from /api/tags via HTMX, so we
    # don't pre-fill those here.
    preset_chat_models: dict[str, list[str]] = {}
    preset_chat_models_full: dict[str, list[str]] = {}
    for p in presets:
        if p.id == "ollama":
            continue
        # Curated short list — what the dropdown shows by default.
        preset_chat_models[p.id] = list_chat_models(p.id)
        # Full LiteLLM-backed list — surfaced by the "Show all" toggle
        # so power users can pick a specific older / specialized model
        # without us shipping a release for every preference.
        preset_chat_models_full[p.id] = list_chat_models(
            p.id, include_legacy=True
        )

    # TTS voice cache: scan tts_voices_dir for .onnx files so the card
    # can surface "N voices installed · M MB" without keeping a separate
    # index. Cheap — the dir tops out at ~10 files for the curated set.
    voices_dir = config.tts_voices_dir
    voice_files = list(voices_dir.glob("*.onnx")) if voices_dir.exists() else []
    voice_cache_summary = {
        "count": len(voice_files),
        "size_mb": round(
            sum(f.stat().st_size for f in voice_files) / (1024 * 1024)
        ),
    }
    # Pre-compute per-language voice options so the template stays
    # logic-free; cheaper than registering a Jinja global for the helper.
    voices_by_language = {
        lang: voices_for_language(lang) for lang in _TTS_VOICE_LANGS
    }

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "presets": presets,
            "preset_chat_models": preset_chat_models,
            "preset_chat_models_full": preset_chat_models_full,
            "applied_preset": applied_preset,
            "current_provider_id": current_provider_id,
            "settings": safe_settings,
            "has_api_key": has_api_key,
            "has_whisper_key": has_whisper_key,
            "has_cookies": has_cookies,
            "api_key_prefix": user.api_key_prefix if user else None,
            "api_key_created_at": user.api_key_created_at if user else None,
            "current_user": current_user,
            "onboarding_done": onboarding == "done",
            "scheduled_playlists": scheduled_playlists,
            "scheduler_last_tick_at": settings.get(
                "scheduler_last_tick_at"
            ),
            "tts_languages": _TTS_LANGUAGES,
            "tts_voice_langs": _TTS_VOICE_LANGS,
            "voices_by_language": voices_by_language,
            "voice_cache_summary": voice_cache_summary,
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
    playlist_refresh_interval_hours: str = Form(""),
    playlist_refresh_interval_minutes: str = Form(""),
    playlist_initial_import_limit: str = Form("20"),
    default_tts_language: str = Form("auto"),
    default_tts_voice_de: str = Form(""),
    default_tts_voice_en_US: str = Form(""),  # noqa: N803 - locale code is upstream-defined
    default_tts_voice_en_GB: str = Form(""),  # noqa: N803 - locale code is upstream-defined
    default_tts_voice_fr: str = Form(""),
    default_tts_voice_es: str = Form(""),
    default_tts_quality: str = Form("medium"),
    default_tts_length_scale: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    # LiteLLM and httpx clients append paths to base; a trailing "/"
    # would produce "//audio/transcriptions" or "//api/chat" which some
    # providers reject with 405.
    llm_base_url = llm_base_url.strip().rstrip("/")
    whisper_base_url = whisper_base_url.strip().rstrip("/")
    for key, value in (
        ("llm_model", llm_model.strip()),
        ("llm_base_url", llm_base_url),
        ("whisper_model", whisper_model.strip() or "small"),
        ("whisper_base_url", whisper_base_url),
        ("summary_language", summary_language.strip() or "auto"),
        ("playlist_refresh_interval_minutes", playlist_refresh_interval_minutes.strip()),
        ("playlist_initial_import_limit", playlist_initial_import_limit.strip()),
        # TTS defaults — empty value clears the key, matching the
        # set/delete pattern used by every field above.
        ("default_tts_language", default_tts_language.strip()),
        ("default_tts_voice_de", default_tts_voice_de.strip()),
        ("default_tts_voice_en_US", default_tts_voice_en_US.strip()),
        ("default_tts_voice_en_GB", default_tts_voice_en_GB.strip()),
        ("default_tts_voice_fr", default_tts_voice_fr.strip()),
        ("default_tts_voice_es", default_tts_voice_es.strip()),
        ("default_tts_quality", default_tts_quality.strip()),
    ):
        if value:
            await settings_repo.set(db, key, value)
        else:
            await settings_repo.delete(db, key)
    # Speech speed: must be a float in [0.5, 2.0]. Anything outside
    # that range (or unparseable) clears the setting so the renderer
    # falls back to the voice's built-in default — better than letting
    # a user accidentally save length_scale=50 and produce minutes of
    # silence per sentence.
    raw_length_scale = default_tts_length_scale.strip()
    if raw_length_scale:
        try:
            v = float(raw_length_scale)
        except ValueError:
            await settings_repo.delete(db, "default_tts_length_scale")
        else:
            if 0.5 <= v <= 2.0:
                await settings_repo.set(
                    db, "default_tts_length_scale", f"{v:.2f}",
                )
            else:
                await settings_repo.delete(db, "default_tts_length_scale")
    else:
        await settings_repo.delete(db, "default_tts_length_scale")
    if llm_api_key:
        await settings_repo.set(db, "llm_api_key", llm_api_key)
    if whisper_api_key:
        await settings_repo.set(db, "whisper_api_key", whisper_api_key)
    # Keep the playlist-interval setting unambiguous: as soon as the
    # user saves the minutes-based form, drop the legacy hours setting
    # so the scheduler has a single source of truth.
    if playlist_refresh_interval_minutes.strip():
        await settings_repo.delete(db, "playlist_refresh_interval_hours")
    elif playlist_refresh_interval_hours.strip():
        # Older form payloads (or a manual API user) sent the legacy
        # field — honour it but normalise the storage by converting
        # to minutes immediately.
        try:
            hours = float(playlist_refresh_interval_hours.strip())
            await settings_repo.set(
                db,
                "playlist_refresh_interval_minutes",
                str(int(hours * 60)),
            )
            await settings_repo.delete(db, "playlist_refresh_interval_hours")
        except ValueError:
            pass
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/quick-setup")
async def quick_setup(
    provider: str = Form(...),
    api_key: str = Form(""),
    llm_model: str = Form(""),
    llm_base_url: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Apply a curated provider preset.

    Writes only the settings keys the preset's provider supports. A
    blank api_key keeps whatever's already in the DB.
    """
    if provider not in PROVIDER_PRESETS:
        raise HTTPException(400, detail=f"Unknown provider: {provider!r}")

    current = await settings_repo.get_all(db)
    updates = apply_preset(
        provider_id=provider,
        api_key=api_key.strip(),
        current_settings=current,
        llm_model_override=llm_model.strip() or None,
        llm_base_url_override=llm_base_url.strip() or None,
    )

    for key, value in updates.items():
        if value:
            await settings_repo.set(db, key, value)
        else:
            # Empty string means "clear this setting" (e.g. when
            # switching away from Ollama, clear stale llm_base_url).
            await settings_repo.delete(db, key)

    return RedirectResponse(
        f"/settings?applied={provider}", status_code=303
    )


@router.get(
    "/settings/quick-setup/ollama-models",
    response_class=HTMLResponse,
)
async def quick_setup_ollama_models(llm_base_url: str = ""):
    """HTMX fragment: list available models on a given Ollama server.

    The query param is named `llm_base_url` so it matches the form
    field's name attribute and gets serialised by HTMX's hx-include.

    Returns a <select name="llm_model"> populated with the server's
    /api/tags response, or an inline error if unreachable.
    """
    base_url = llm_base_url.strip()
    if not base_url:
        return HTMLResponse(
            '<p class="status status-failed">⚠ Enter a server URL first.</p>'
        )
    try:
        tags = await fetch_ollama_models(base_url)
    except Exception as e:
        return HTMLResponse(
            f'<p class="status status-failed">⚠ Cannot reach Ollama at '
            f'{base_url}: {type(e).__name__}: {e}</p>'
        )
    if not tags:
        return HTMLResponse(
            '<p class="status status-failed">⚠ Ollama server has no '
            'models pulled yet. Run e.g. <code>ollama pull llama3.1</code> '
            'first.</p>'
        )

    chat_tags, embed_tags = split_ollama_tags(tags)

    # Chat dropdown — every non-embedder model. The /api/tags response
    # doesn't tell us which are chat-capable, but the heuristic in
    # split_ollama_tags catches the 3 standard embedders by name.
    if chat_tags:
        chat_options = "".join(
            f'<option value="ollama_chat/{tag}">{tag}</option>'
            for tag in chat_tags
        )
        chat_block = (
            '<label class="settings-field">'
            '<span class="settings-label">LLM model</span>'
            f'<select name="llm_model">{chat_options}</select>'
            '</label>'
        )
    else:
        chat_block = (
            '<p class="status status-failed">⚠ No chat-capable model '
            'found. Pull one with <code>ollama pull llama3.1</code>.</p>'
        )

    summary = (
        f'<small class="settings-test-hint">Found {len(tags)} model'
        f'{"" if len(tags) == 1 else "s"} on {base_url} — '
        f'{len(chat_tags)} chat, {len(embed_tags)} embedding.</small>'
    )
    return HTMLResponse(chat_block + summary)


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
    current_user=Depends(get_current_user),
):
    plaintext, key_hash, prefix = _gen_key()
    await users_repo.set_api_key(
        db, user_id=1, key_hash=key_hash, key_prefix=prefix
    )
    # Build the user-facing base URL (scheme + host:port) so the snippets
    # on the reveal page point at the same address the user is browsing
    # us on. request.url.scheme picks up X-Forwarded-Proto when behind a
    # proxy that sets it; netloc honours the Host header.
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    # mcp-remote refuses non-https URLs unless --allow-http is passed.
    # yt-summary's primary use case is LAN-only (http://192.168.x.x),
    # so the reveal page injects --allow-http whenever we're not https.
    is_https = request.url.scheme == "https"
    return templates.TemplateResponse(
        request,
        "api_key_reveal.html",
        {
            "plaintext": plaintext,
            "prefix": prefix,
            "base_url": base_url,
            "is_https": is_https,
            "current_user": current_user,
        },
    )


@router.post("/settings/api-key/revoke")
async def revoke_api_key_route(
    db: aiosqlite.Connection = Depends(get_db),
):
    await users_repo.clear_api_key(db, user_id=1)
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/diagnostics", response_class=HTMLResponse)
async def diagnostics_page(
    request: Request,
    lines: int = 200,
    db: aiosqlite.Connection = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Render the diagnostics dashboard.

    Reads from app.state (heartbeats, log_buffer, the three workers
    for poll-interval thresholds), the DB (queue counts + recent
    jobs), and the persisted scheduler tick stamp. No mutation.
    """
    heartbeats = request.app.state.heartbeats.snapshot()
    log_buffer = request.app.state.log_buffer
    worker = request.app.state.worker
    tts_worker = request.app.state.tts_worker
    scheduler = request.app.state.scheduler

    # Resolve the per-worker stale threshold = 3 × poll_interval.
    # 3× gives the 1s pollers a ~3s budget and the 60-min scheduler
    # a ~3h budget — matches what the spec calls for.
    worker_thresholds_seconds = {
        "summary_worker": worker.poll_interval_seconds * 3,
        "tts_worker":     tts_worker.poll_interval_seconds * 3,
        "scheduler":      (await scheduler.current_interval_seconds()) * 3,
    }

    summary_counts = await jobs_repo.counts(db)
    summary_queue = await jobs_repo.list_queue(db, limit=10)
    summary_failed = await jobs_repo.list_recent_failed(db, limit=10)

    tts_counts = await tts_jobs_repo.counts(db)
    tts_queue = await tts_jobs_repo.list_queue(db, limit=10)
    tts_failed = await tts_jobs_repo.list_recent_failed(db, limit=10)

    scheduler_last_tick_at = await settings_repo.get(
        db, "scheduler_last_tick_at",
    )
    scheduled_playlists = await playlists_repo.list_for_user(db, 1)
    reembed_pending = await embeddings_repo.count_pending_reembed(db)

    # Bound the requested line count to the buffer capacity.
    log_lines = log_buffer.snapshot(limit=max(1, min(lines, 500)))

    now_naive = datetime.now(UTC).replace(tzinfo=None)

    return templates.TemplateResponse(
        request,
        "diagnostics.html",
        {
            "current_user": current_user,
            "now": now_naive,
            "heartbeats": heartbeats,
            "worker_thresholds_seconds": worker_thresholds_seconds,
            "summary_counts": summary_counts,
            "summary_queue": summary_queue,
            "summary_failed": summary_failed,
            "tts_counts": tts_counts,
            "tts_queue": tts_queue,
            "tts_failed": tts_failed,
            "scheduler_last_tick_at": scheduler_last_tick_at,
            "scheduled_playlists": scheduled_playlists,
            "reembed_pending": reembed_pending,
            "log_lines": log_lines,
            "lines_requested": lines,
        },
    )


@router.post("/settings/diagnostics/retry-job/{job_id}")
async def diagnostics_retry_job(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Reset a failed summary job back to 'pending'. 404 if the job
    is missing or not in 'failed' state."""
    affected = await jobs_repo.retry(db, job_id)
    if affected == 0:
        raise HTTPException(404, detail=f"No failed job {job_id}")
    return RedirectResponse("/settings/diagnostics", status_code=303)


@router.post("/settings/diagnostics/delete-job/{job_id}")
async def diagnostics_delete_job(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Delete a failed summary job row. The video is untouched. 404
    if the job is missing or not in 'failed' state."""
    affected = await jobs_repo.delete(db, job_id)
    if affected == 0:
        raise HTTPException(404, detail=f"No failed job {job_id}")
    return RedirectResponse("/settings/diagnostics", status_code=303)


@router.post("/settings/diagnostics/retry-tts/{job_id}")
async def diagnostics_retry_tts(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Reset a failed TTS job back to 'queued'. Preserves
    translated_text so we don't pay the LLM cost again."""
    affected = await tts_jobs_repo.retry(db, job_id)
    if affected == 0:
        raise HTTPException(404, detail=f"No failed tts_job {job_id}")
    return RedirectResponse("/settings/diagnostics", status_code=303)


@router.post("/settings/diagnostics/delete-tts/{job_id}")
async def diagnostics_delete_tts(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Delete a failed TTS job row. We deliberately do NOT clean up any
    MP3 on disk — failed jobs typically have none; orphan cleanup is a
    separate concern. 404 unless the row exists AND is in 'failed' state."""
    # Pre-check that the row is failed; the existing tts_jobs_repo.delete()
    # is unconditional, so we gate manually here. The two-statement
    # pattern is a small race but harmless on a single-writer SQLite —
    # a failed → succeeded transition between the check and delete is
    # impossible (terminal states don't change).
    async with db.execute(
        "SELECT id FROM tts_jobs WHERE id=? AND status='failed'",
        (job_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, detail=f"No failed tts_job {job_id}")
    await tts_jobs_repo.delete(db, job_id)
    return RedirectResponse("/settings/diagnostics", status_code=303)


@router.post("/settings/diagnostics/tick-scheduler")
async def diagnostics_tick_scheduler(request: Request):
    """Wake the PlaylistScheduler so its next iteration runs
    immediately, instead of waiting out the rest of the interval."""
    request.app.state.scheduler.request_tick()
    return RedirectResponse("/settings/diagnostics", status_code=303)


@router.post("/settings/custom-prompt")
async def save_custom_prompt(
    custom_summary_prompt: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """Update the active profile's custom summary prompt.

    Empty string clears the prompt (falls back to the standard
    summarizer prompt). Per-profile, scoped via the cookie-resolved
    current_user_id — provider/model settings stay global.
    """
    text = custom_summary_prompt.strip()
    await users_repo.update(
        db,
        current_user_id,
        custom_summary_prompt=text or None,
        custom_summary_prompt_set=True,
    )
    return RedirectResponse("/settings", status_code=303)
