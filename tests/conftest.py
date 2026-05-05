from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from app.config import Config
from app.db import connect, init_schema


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


@pytest_asyncio.fixture
async def db(config: Config) -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(config)
    await init_schema(conn)
    yield conn
    await conn.close()
