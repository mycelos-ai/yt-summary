"""HTML article extraction via trafilatura.

We trade accuracy for predictability: trafilatura's extract gives us
plain-text body (no HTML), which feeds the same summary/chat pipeline
as a YouTube transcript. Title and og:image come from trafilatura's
metadata extractor.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import trafilatura
from trafilatura.metadata import extract_metadata


@dataclass(frozen=True)
class ArticleMetadata:
    url: str
    title: str
    description: str
    body: str
    thumbnail_url: str | None


_OG_IMAGE_RE = re.compile(
    r'<meta\s+[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _pick_og_image(html: str) -> str | None:
    match = _OG_IMAGE_RE.search(html)
    return match.group(1) if match else None


def _extract_sync(url: str) -> ArticleMetadata:
    html = trafilatura.fetch_url(url)
    if html is None:
        raise ValueError(f"Could not fetch {url!r}")

    body = trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    if not body or not body.strip():
        raise ValueError(f"Could not extract article body from {url!r}")

    metadata: Any = extract_metadata(html)
    title = ""
    description = ""
    if metadata is not None:
        title = (getattr(metadata, "title", None) or "").strip()
        description = (getattr(metadata, "description", None) or "").strip()

    return ArticleMetadata(
        url=url,
        title=title or url,
        description=description,
        body=body.strip(),
        thumbnail_url=_pick_og_image(html),
    )


async def fetch_article(url: str) -> ArticleMetadata:
    return await asyncio.to_thread(_extract_sync, url)
