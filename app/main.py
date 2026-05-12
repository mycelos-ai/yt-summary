import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Config
from app.db import connect, init_schema
from app.repos import jobs as jobs_repo
from app.repos import settings as settings_repo
from app.repos import users as users_repo

# Cookie carrying the active profile id. Single integer, no signing —
# this is a single-user-self-hosted family tool, not a SaaS app. The
# attacker model is "nobody on the LAN" and the cookie is local-only.
PROFILE_COOKIE = "yts_user_id"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.pipeline import process_video
    from app.scheduler import PlaylistScheduler
    from app.services.playlist_sync import sync_playlist
    from app.services.translation import translate
    from app.services.tts_render import render_chunks_to_mp3
    from app.services.tts_voices import download_voice
    from app.tts_worker import TtsWorker
    from app.worker import Worker

    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    await init_schema(db)
    await jobs_repo.reset_orphaned_running(db)

    # Warn loudly if no API key is set — anyone on the LAN can call
    # the API. Useful default for first run, but the user should
    # generate one before exposing the box.
    from app.repos import users as _users_repo
    _user = await _users_repo.get_default_user(db)
    if _user is None or _user.api_key_hash is None:
        logging.getLogger("yt_summary.boot").warning(
            "No API key configured — /api/v1 and /mcp/sse are open to "
            "anyone on the LAN. Generate one at /settings."
        )

    worker = Worker(db=db, config=config, process_video=process_video)
    worker_task = asyncio.create_task(worker.run())

    async def _ensure_voice(language: str, voice: str, quality: str):
        return await download_voice(
            config.tts_voices_dir, language, voice, quality, progress=None,
        )

    tts_worker = TtsWorker(
        db=db,
        config=config,
        translate=translate,
        render_chunks_to_mp3=render_chunks_to_mp3,
        ensure_voice=_ensure_voice,
    )
    tts_worker_task = asyncio.create_task(tts_worker.run())

    scheduler = PlaylistScheduler(db=db, config=config, sync_fn=sync_playlist)
    scheduler_task = asyncio.create_task(scheduler.run())

    app.state.config = config
    app.state.db = db
    app.state.worker = worker
    app.state.tts_worker = tts_worker
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.stop()
        worker.stop()
        tts_worker.stop()
        await scheduler_task
        await worker_task
        await tts_worker_task
        await db.close()


def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def get_config(request: Request) -> Config:
    return request.app.state.config


async def get_current_user_id(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> int:
    """Resolve the active profile id from the ``yts_user_id`` cookie.

    Falls back to ``1`` (the seeded default profile) for missing or
    malformed cookies, deleted profiles, or when the seeded user is
    somehow gone too. There's no auth here on purpose — the spec
    calls for "anyone with browser access can switch profiles" and
    the cookie is just bookkeeping.
    """
    raw = request.cookies.get(PROFILE_COOKIE)
    try:
        uid = int(raw) if raw else 1
    except (TypeError, ValueError):
        uid = 1
    user = await users_repo.get_by_id(db, uid)
    if user is None:
        return 1
    return uid


async def _onboarding_status(db: aiosqlite.Connection) -> dict[str, object]:
    """Decide whether to push a fresh visitor into the onboarding wizard.

    Returns ``{'pending': True, 'next_step': '/onboarding/welcome'}``
    when there's no ``onboarding_completed`` marker AND no LLM model
    is configured. The marker means the user has been through (or
    skipped) the wizard at least once; the configuration check is a
    fallback for users who set the box up manually before this code
    shipped — we don't want to ambush them.

    We key off ``llm_model`` rather than ``llm_api_key`` because Ollama
    setups have a model + base URL but no API key, and they're a
    perfectly valid first-run state. (The old API-key heuristic broke
    onboarding for Ollama users, who got pushed back into the wizard
    even after a clean configuration.)
    """
    s = await settings_repo.get_all(db)
    if s.get("onboarding_completed"):
        return {"pending": False, "next_step": None}
    if not s.get("llm_model"):
        return {"pending": True, "next_step": "/onboarding/welcome"}
    # An LLM model is set — the user has a working configuration.
    # No need to push them through the wizard.
    return {"pending": False, "next_step": None}


async def get_current_user(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Return the full User row for the active profile.

    Convenience wrapper around get_current_user_id that the templates
    and routes need (the header dropdown shows the avatar + name, the
    settings page shows the custom prompt). Falls back to id=1 the
    same way.
    """
    raw = request.cookies.get(PROFILE_COOKIE)
    try:
        uid = int(raw) if raw else 1
    except (TypeError, ValueError):
        uid = 1
    user = await users_repo.get_by_id(db, uid)
    if user is None:
        user = await users_repo.get_by_id(db, 1)
    return user


def create_app() -> FastAPI:
    from app.routes.home import router as home_router

    app = FastAPI(title="yt-summary", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.get("/thumbnails/{video_id}.jpg")
    async def thumbnail(video_id: str, request: Request) -> FileResponse:
        cfg: Config = request.app.state.config
        path = cfg.thumbnails_dir / f"{video_id}.jpg"
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path)

    app.include_router(home_router)
    from app.routes.videos import router as videos_router
    app.include_router(videos_router)
    from app.routes.chat import router as chat_router
    app.include_router(chat_router)
    from app.routes.settings import router as settings_router
    app.include_router(settings_router)
    from app.routes.playlists import router as playlists_router
    app.include_router(playlists_router)
    from app.routes.profiles import router as profiles_router
    app.include_router(profiles_router)
    from app.routes.onboarding import router as onboarding_router
    app.include_router(onboarding_router)
    from app.routes.audio import router as audio_router
    app.include_router(audio_router)
    from app.routes.api import router as api_router
    app.include_router(api_router)
    from app.routes.mcp import build_mcp_server
    mcp_server = build_mcp_server(app.state)
    app.mount("/mcp", mcp_server.sse_app())
    return app


app = create_app()
