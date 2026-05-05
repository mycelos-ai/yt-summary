import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx
from yt_dlp import YoutubeDL

_VIDEO_ID_RE = re.compile(
    r"(?:v=|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})"
)


def parse_video_id(url: str) -> str:
    match = _VIDEO_ID_RE.search(url)
    if not match:
        raise ValueError(f"Could not extract video id from {url!r}")
    return match.group(1)


@dataclass(frozen=True)
class VideoMetadata:
    id: str
    url: str
    title: str
    description: str
    duration_seconds: int | None
    thumbnail_url: str | None


def _extract_info(url: str, cookies_path: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {"skip_download": True, "quiet": True, "no_warnings": True}
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)  # type: ignore[return-value]


async def fetch_metadata(url: str, cookies_path: Path | None) -> VideoMetadata:
    info = await asyncio.to_thread(_extract_info, url, cookies_path)
    return VideoMetadata(
        id=info["id"],
        url=info.get("webpage_url", url),
        title=info.get("title", ""),
        description=info.get("description") or "",
        duration_seconds=info.get("duration"),
        thumbnail_url=info.get("thumbnail"),
    )


async def download_thumbnail(url: str | None, target: Path) -> None:
    if not url:
        return
    async_target = anyio.Path(target)
    await async_target.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        await async_target.write_bytes(resp.content)
