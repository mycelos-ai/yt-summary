from collections.abc import AsyncIterator
from pathlib import Path
import urllib.request

import aiosqlite
import pytest
import pytest_asyncio

from app.config import Config
from app.db import connect, init_schema


# Test voice cache. Lives outside the repo so the 60 MB binary never
# enters git history. First run on a machine downloads the model from
# Hugging Face; subsequent runs hit the cache. To force a refresh,
# delete the directory and re-run the tests.
VOICE_CACHE = Path.home() / ".cache" / "yt-summary-test-voices"
VOICE_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low"
)


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


@pytest.fixture(scope="session")
def amy_low_voice() -> Path:
    """Path to the en_US-amy-low.onnx model used by render tests.

    Cached at ``~/.cache/yt-summary-test-voices/``. Downloaded once
    per machine on first test run (~60 MB from Hugging Face).
    Subsequent runs hit the cache. The matching ``.onnx.json`` sidecar
    lands next to the model; derive its path with
    ``amy_low_voice.with_suffix(amy_low_voice.suffix + ".json")``.

    Provenance:
      https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/amy/low
    """
    VOICE_CACHE.mkdir(parents=True, exist_ok=True)
    onnx = VOICE_CACHE / "en_US-amy-low.onnx"
    onnx_json = VOICE_CACHE / "en_US-amy-low.onnx.json"
    for path, suffix in ((onnx, ""), (onnx_json, ".json")):
        if not path.exists():
            url = f"{VOICE_BASE}/en_US-amy-low.onnx{suffix}"
            with urllib.request.urlopen(url) as r, path.open("wb") as f:
                f.write(r.read())
    return onnx
