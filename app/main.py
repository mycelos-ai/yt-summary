import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Config
from app.db import connect, init_schema
from app.repos import jobs as jobs_repo


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.pipeline import process_video
    from app.scheduler import PlaylistScheduler
    from app.services.playlist_sync import sync_playlist
    from app.worker import Worker

    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    await init_schema(db)
    await jobs_repo.reset_orphaned_running(db)

    worker = Worker(db=db, config=config, process_video=process_video)
    worker_task = asyncio.create_task(worker.run())

    scheduler = PlaylistScheduler(db=db, config=config, sync_fn=sync_playlist)
    scheduler_task = asyncio.create_task(scheduler.run())

    app.state.config = config
    app.state.db = db
    app.state.worker = worker
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.stop()
        worker.stop()
        await scheduler_task
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
    from app.routes.playlists import router as playlists_router
    app.include_router(playlists_router)
    from app.routes.api import router as api_router
    app.include_router(api_router)
    return app


app = create_app()
