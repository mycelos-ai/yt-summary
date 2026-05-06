"""Classify a submitted URL as a YouTube video or a generic web page.

YouTube wins iff the existing parse_video_id pattern matches. Anything
else is treated as web (no further validation; reader/yt-dlp surface
their own errors when they actually try to fetch).
"""

import hashlib
from typing import Literal

from app.services.youtube import parse_video_id

UrlKind = Literal["youtube", "web"]


def classify_url(url: str) -> UrlKind:
    try:
        parse_video_id(url)
        return "youtube"
    except ValueError:
        return "web"


def web_id_from_url(url: str) -> str:
    """Stable web-item id derived from the URL.

    11 alphanumeric characters (Base64 of a sha256 prefix), prefixed
    with 'web-' so a glance at the id reveals the kind. 11 chars from
    a 256-bit hash gives ~64 bits of entropy after prefix — collision
    probability is negligible at any realistic library size.
    """
    digest = hashlib.sha256(url.encode("utf-8")).digest()
    # Hex chars are [0-9a-f] which is fine for our id alphabet.
    return "web-" + digest.hex()[:11]
