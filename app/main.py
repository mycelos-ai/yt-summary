from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.config import Config
from app.db import connect, init_schema
from app.repos import jobs as jobs_repo


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    await init_schema(db)
    await jobs_repo.reset_orphaned_running(db)
    app.state.config = config
    app.state.db = db
    try:
        yield
    finally:
        await db.close()


def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def get_config(request: Request) -> Config:
    return request.app.state.config


def create_app() -> FastAPI:
    app = FastAPI(title="yt-summary", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return "<h1>yt-summary</h1>"

    return app


app = create_app()
