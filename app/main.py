import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Config
from app.db import connect, init_schema
from app.repos import jobs as jobs_repo
from app.repos import settings as settings_repo

log = logging.getLogger("yt_summary.startup")


async def _probe_llm_at_startup(db: aiosqlite.Connection) -> str:
    """Best-effort connectivity probe. Logs and returns a status string."""
    settings = await settings_repo.get_all(db)
    model = settings.get("llm_model")
    base_url = settings.get("llm_base_url")
    if not model:
        msg = "LLM startup probe: no model configured (transcript-only mode)."
        log.info(msg)
        return msg

    if base_url and model.startswith(("ollama/", "ollama_chat/")):
        url = f"{base_url.rstrip('/')}/api/tags"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url)
            if r.status_code == 200:
                try:
                    names = [m.get("name") for m in r.json().get("models", [])]
                except Exception:
                    names = []
                msg = (
                    f"LLM startup probe: OK — {url} returned {len(names)} model(s). "
                    f"Configured: {model}. Available: {', '.join(names) or '(none)'}."
                )
                log.info(msg)
                if model.split("/", 1)[1] not in names:
                    log.warning(
                        "Configured model %r is not in the Ollama tag list. "
                        "Available tags: %s",
                        model.split("/", 1)[1],
                        names,
                    )
                return msg
            msg = f"LLM startup probe: FAIL — {url} returned HTTP {r.status_code}."
            log.error(msg)
            return msg
        except Exception as e:
            msg = (
                f"LLM startup probe: FAIL — {url} raised "
                f"{type(e).__name__}: {e}"
            )
            log.error(msg)
            return msg

    msg = (
        f"LLM startup probe: skipped (model={model!r}, base_url={base_url!r}; "
        "active probe only runs for ollama/ollama_chat backends)."
    )
    log.info(msg)
    return msg


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.pipeline import process_video
    from app.worker import Worker

    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    await init_schema(db)
    await jobs_repo.reset_orphaned_running(db)

    app.state.startup_probe = await _probe_llm_at_startup(db)

    worker = Worker(db=db, config=config, process_video=process_video)
    worker_task = asyncio.create_task(worker.run())

    app.state.config = config
    app.state.db = db
    app.state.worker = worker
    try:
        yield
    finally:
        worker.stop()
        await worker_task
        await db.close()


def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def get_config(request: Request) -> Config:
    return request.app.state.config


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
    return app


app = create_app()
