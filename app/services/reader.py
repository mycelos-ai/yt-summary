"""HTML article extraction via trafilatura.

We trade accuracy for predictability: trafilatura's extract gives us
plain-text body (no HTML), which feeds the same summary/chat pipeline
as a YouTube transcript. Title and og:image come from trafilatura's
metadata extractor.

Fetching: we use httpx with a real browser User-Agent rather than
trafilatura's built-in fetch_url, because many news sites (e.g.
Spiegel, NYT, Wired) block the default trafilatura user-agent
outright. The extraction itself stays trafilatura's job.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx
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

# Pretend to be a regular browser so news sites don't reject us.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}


def _pick_og_image(html: str) -> str | None:
    match = _OG_IMAGE_RE.search(html)
    return match.group(1) if match else None


def _fetch_html(url: str) -> tuple[str, str]:
    """Fetch HTML with a browser-like User-Agent.

    Returns (canonical_url, html). canonical_url is whatever the chain
    of redirects landed on. Raises ValueError on any failure with a
    user-readable message.
    """
    try:
        with httpx.Client(
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
            timeout=15.0,
        ) as client:
            resp = client.get(url)
    except httpx.HTTPError as e:
        raise ValueError(
            f"Could not reach the page ({type(e).__name__}: {e})"
        ) from e

    if resp.status_code == 403:
        raise ValueError(
            "The site refused to send the page (403 Forbidden). "
            "It may block bots or require login."
        )
    if resp.status_code == 404:
        raise ValueError("The page does not exist (404 Not Found).")
    if resp.status_code == 429:
        raise ValueError(
            "The site is rate-limiting us (429 Too Many Requests). "
            "Try again in a minute."
        )
    if resp.status_code >= 400:
        raise ValueError(
            f"The site returned HTTP {resp.status_code}."
        )

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        raise ValueError(
            f"The URL didn't return an HTML page (got {content_type or 'unknown content-type'}). "
            "PDFs and other formats aren't supported yet."
        )

    return str(resp.url), resp.text


def _extract_sync(url: str) -> ArticleMetadata:
    canonical_url, html = _fetch_html(url)

    body = trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    if not body or not body.strip():
        raise ValueError(
            "We reached the page but couldn't pull any article text out of "
            "it. The site may be JavaScript-rendered, behind a paywall, or "
            "structured in an unusual way."
        )

    metadata: Any = extract_metadata(html)
    title = ""
    description = ""
    if metadata is not None:
        title = (getattr(metadata, "title", None) or "").strip()
        description = (getattr(metadata, "description", None) or "").strip()

    return ArticleMetadata(
        url=canonical_url,
        title=title or canonical_url,
        description=description,
        body=body.strip(),
        thumbnail_url=_pick_og_image(html),
    )


async def fetch_article(url: str) -> ArticleMetadata:
    return await asyncio.to_thread(_extract_sync, url)
