"""Local embedding via sentence-transformers.

Loads `paraphrase-multilingual-MiniLM-L12-v2` (384d) lazily on first
use and keeps it in a process-wide singleton. Inference runs in a
worker thread so the asyncio event loop stays responsive.

The model auto-downloads to ``~/.cache/huggingface/`` on first call
(~120 MB). Subsequent process starts hit the cache.

Offline / fresh-machine safety:
    ``_load_model_sync`` checks whether the model is cached locally
    BEFORE attempting to import sentence_transformers or call
    SentenceTransformer().  When not cached it raises ``RuntimeError``
    immediately — no network attempt, no HF DNS retry-backoff hang.
    Every caller of ``embed_text`` is best-effort and catches Exception,
    so the raise degrades cleanly (embedding skipped, recency fallback
    used for retrieval).
"""
from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger(__name__)

EMBEDDING_DIM = 384
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Module-level singleton + load lock. The lock prevents two
# concurrent first-time loads if embed_text is called twice on a
# fresh process before the first call has finished. After the first
# successful load, _model is non-None and the lock is uncontended.
_model = None  # type: ignore[var-annotated]
_load_lock = threading.Lock()


def _model_is_cached() -> bool:
    """Return True only when the HF model snapshot is present on disk.

    Uses ``huggingface_hub.try_to_load_from_cache`` which is a pure
    local probe — it never touches the network. If the probe itself
    errors (e.g. huggingface_hub not installed), returns False so the
    caller fast-fails rather than risking a blocking download.

    Mirrors the ``_embed_model_is_cached`` helper in tests/conftest.py.
    """
    try:
        import os

        from huggingface_hub import try_to_load_from_cache

        # config.json is the first file SentenceTransformer reads at
        # load time. A real file-path return value means it is cached.
        hit = try_to_load_from_cache(MODEL_NAME, "config.json")
        return isinstance(hit, str) and os.path.isfile(hit)
    except Exception:  # noqa: BLE001
        return False


def _load_model_sync():
    """Heavy import + load. Called inside a worker thread."""
    global _model
    with _load_lock:
        if _model is not None:
            return _model
        # Fast-fail when the model is not cached locally: attempting to
        # load without the cache would trigger HF network retry-backoff
        # which can hang for tens of seconds before failing — unacceptable
        # inside a request handler.  All callers are best-effort and catch
        # Exception, so the raise degrades cleanly.
        if not _model_is_cached():
            raise RuntimeError(
                f"embedding model {MODEL_NAME} not cached locally; "
                "refusing to download in-request"
            )
        import os
        log.info("Loading sentence-transformers model %s …", MODEL_NAME)
        # Belt-and-suspenders: even though the cache gate above already
        # prevents reaching here when uncached, pin HF_HUB_OFFLINE for
        # the load itself so a false-positive cache probe can never
        # trigger a download or backoff.  We restore the previous value
        # afterwards so a subsequent intentional online use (e.g. a
        # different part of the app) is not broken.
        prev_offline = os.environ.get("HF_HUB_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            # Imported lazily so the rest of the app doesn't pay the
            # transformers+torch import cost when embeddings aren't used.
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME)
        finally:
            if prev_offline is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = prev_offline
        log.info("Model %s ready (dim=%d)", MODEL_NAME, EMBEDDING_DIM)
    return _model


def _encode_sync(text: str) -> list[float]:
    """Run the actual encode call. Numpy → plain list at the boundary.

    `normalize_embeddings=True` returns unit-length vectors so that the
    vec0 table's L2 distance is a monotonic function of cosine distance
    (L2² = 2·cosine_distance) — see related.related_video_ids.
    """
    model = _load_model_sync()
    # convert_to_numpy=True keeps memory predictable; we tolist() right after.
    arr = model.encode(
        text,
        convert_to_numpy=True,
        show_progress_bar=False,
        normalize_embeddings=True,
    )
    return [float(x) for x in arr]


async def embed_text(text: str) -> list[float]:
    """Return the 384d embedding vector for `text`.

    Empty / whitespace-only input raises ``ValueError`` (matches the
    contract of the previous LiteLLM-backed implementation).

    Raises ``RuntimeError`` when the model is not cached locally (no
    network hang — callers are expected to be best-effort).
    """
    text = text.strip()
    if not text:
        raise ValueError("Cannot embed empty text")
    return await asyncio.to_thread(_encode_sync, text)
