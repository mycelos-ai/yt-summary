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
