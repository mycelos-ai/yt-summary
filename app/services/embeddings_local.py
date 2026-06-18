"""Local embedding via sentence-transformers.

Loads `paraphrase-multilingual-MiniLM-L12-v2` (384d) lazily on first
use and keeps it in a process-wide singleton. Inference runs in a
worker thread so the asyncio event loop stays responsive.

The model auto-downloads to ``~/.cache/huggingface/`` on first call
(~120 MB). Subsequent process starts hit the cache.
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


def _load_model_sync():
    """Heavy import + load. Called inside a worker thread."""
    global _model
    with _load_lock:
        if _model is not None:
            return _model
        log.info("Loading sentence-transformers model %s …", MODEL_NAME)
        # Imported lazily so the rest of the app doesn't pay the
        # transformers+torch import cost when embeddings aren't used.
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
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
    """
    text = text.strip()
    if not text:
        raise ValueError("Cannot embed empty text")
    return await asyncio.to_thread(_encode_sync, text)
