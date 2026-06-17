"""Fetch a fitting stock photo from Pexels as an item thumbnail.

Fully fault-tolerant: a missing key, no search hit, a rate-limit (429),
a timeout, or malformed JSON all return False. Thumbnails are cosmetic
and must never block ingestion.
"""
from __future__ import annotations

import logging
from pathlib import Path

import anyio
import httpx
import litellm

from app.config import Config
from app.models import Video, VideoKind
from app.repos import videos as videos_repo
from app.services.youtube import download_thumbnail

log = logging.getLogger(__name__)

_PEXELS_SEARCH = "https://api.pexels.com/v1/search"


async def fetch_pexels_thumbnail(
    *, query: str, api_key: str, target: Path,
) -> bool:
    """Search Pexels for `query`, download the top landscape photo to
    `target`. Returns True only if a file was written."""
    if not api_key or not query.strip():
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.get(
                _PEXELS_SEARCH,
                headers={"Authorization": api_key},
                params={
                    "query": query,
                    "per_page": 1,
                    "orientation": "landscape",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        photos = data.get("photos") or []
        if not photos:
            return False
        src = (photos[0].get("src") or {}).get("large")
        if not src:
            return False
        await download_thumbnail(src, target)
        return await anyio.Path(target).exists()
    except Exception as e:  # pragma: no cover - defensive
        log.info("pexels: thumbnail fetch failed for %r: %s", query, e)
        return False


_ELIGIBLE_KINDS = (VideoKind.EMAIL, VideoKind.WEB)


async def ensure_stock_thumbnail(
    db, video: Video, *, config: Config, api_key: str, force: bool,
) -> bool:
    """Fetch+set a Pexels thumbnail for an email/web item when missing
    (or always, when `force`). Returns True if a thumbnail was written.

    No-ops (returns False) for: ineligible kind, empty api_key, no
    image_query, or an existing thumbnail when not forcing.
    """
    if video.kind not in _ELIGIBLE_KINDS:
        return False
    if not api_key:
        return False
    if video.thumbnail_path and not force:
        return False
    query = (video.image_query or "").strip()
    if not query:
        return False
    target = config.thumbnails_dir / f"{video.id}.jpg"
    ok = await fetch_pexels_thumbnail(
        query=query, api_key=api_key, target=target,
    )
    if not ok:
        return False
    await videos_repo.set_thumbnail_path(db, video.id, str(target))
    return True


_QUERY_PROMPT = (
    "Given this article/newsletter summary, output ONLY 2-4 English "
    "keywords for a fitting stock photo (concrete and visual, no proper "
    "nouns, no quotes, no punctuation).\n\nSUMMARY:\n{summary}"
)


async def generate_image_query(*, summary: str, model_row) -> str | None:
    """Cheap one-off LLM call to derive a stock-photo query from a
    summary. Returns None on empty summary, no model, or any error —
    image queries are cosmetic and must never block."""
    if not summary or not summary.strip() or model_row is None:
        return None
    try:
        kwargs: dict = {
            "model": model_row.model,
            "messages": [
                {
                    "role": "user",
                    "content": _QUERY_PROMPT.format(summary=summary),
                },
            ],
            "api_key": model_row.api_key,
        }
        if model_row.base_url:
            kwargs["api_base"] = model_row.base_url
        resp = await litellm.acompletion(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:  # pragma: no cover - defensive
        log.info("image-query generation failed: %s", e)
        return None
