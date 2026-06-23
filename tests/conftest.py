import asyncio
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from app.config import Config
from app.db import connect, init_schema


@pytest.fixture(autouse=True)
def _ensure_event_loop() -> None:
    """Make `asyncio.get_event_loop()` work in synchronous test bodies.

    Python 3.12 raises RuntimeError when get_event_loop() is called
    from a thread that doesn't already have one (and pytest-asyncio's
    auto mode closes the loop between tests). Several tests in this
    suite drive async setup from sync functions via
    `asyncio.get_event_loop().run_until_complete(...)` — the
    established pattern — so we re-create a loop for each test
    function that needs it. autouse=True so individual tests don't
    have to opt in.
    """
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

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


def _embed_model_is_cached() -> bool:
    """True only if the sentence-transformers model is already in the local
    HF cache — checked WITHOUT any network access.

    huggingface_hub's ``try_to_load_from_cache`` returns a path when the
    snapshot exists locally and a sentinel otherwise; it never hits the
    network. If the probe itself can't run, assume "not cached" so the
    warmup is skipped rather than risking a blocking download.
    """
    try:
        import os

        from huggingface_hub import try_to_load_from_cache

        from app.services.embeddings_local import MODEL_NAME

        # config.json is read at load time by every SentenceTransformer.
        # try_to_load_from_cache returns a real file path string when the
        # file is cached, and a non-str sentinel otherwise — so a path that
        # points at an existing file is a reliable "cached" signal without
        # importing version-specific sentinel constants.
        hit = try_to_load_from_cache(MODEL_NAME, "config.json")
        return isinstance(hit, str) and os.path.isfile(hit)
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def _warmup_local_embedder() -> None:
    """Warm the sentence-transformers singleton once per run — but ONLY when
    it can be done locally, so an offline machine without the model cached
    never blocks the whole suite in network retry-backoff.

    Skipped entirely when:
    - ``YTS_SKIP_EMBED_WARMUP`` is set (explicit opt-out), or
    - the model is not present in the local HF cache.

    Tests that genuinely need real embeddings will then fail with their own
    assertions instead of the suite stalling. Cached/online machines still
    get a warm singleton (fast first embed). autouse=True so opted-in tests
    don't have to request it. The body is sync — it spins up an event loop
    just for the warmup call.
    """
    import asyncio
    import contextlib
    import os

    from app.services import embeddings_local

    # Already loaded in this process (e.g. a prior session) — fast bail.
    if embeddings_local._model is not None:
        return
    # Explicit opt-out, or model not cached locally → do NOT touch the
    # network (which would hang offline). Skip warmup; real-embedding tests
    # own their own failure.
    if os.environ.get("YTS_SKIP_EMBED_WARMUP") or not _embed_model_is_cached():
        return
    # Belt-and-suspenders: pin HF offline for the warmup so even an
    # unexpected cache miss fails fast instead of downloading/backing off.
    prev_offline = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        with contextlib.suppress(Exception):
            asyncio.get_event_loop().run_until_complete(
                embeddings_local.embed_text("warmup")
            )
    finally:
        if prev_offline is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = prev_offline
