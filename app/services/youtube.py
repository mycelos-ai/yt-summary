import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
from yt_dlp import YoutubeDL

log = logging.getLogger(__name__)

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
    tags: tuple[str, ...] = ()


def _extract_info(url: str, cookies_path: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {"skip_download": True, "quiet": True, "no_warnings": True}
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)  # type: ignore[return-value]


def _pick_best_thumbnail(info: dict[str, Any]) -> str | None:
    """Pick the highest-resolution thumbnail yt-dlp surfaced.

    yt-dlp's top-level info["thumbnail"] is often hqdefault (480x360),
    which looks soft when scaled to the card width. The thumbnails list
    typically also has maxresdefault (1280x720) and sddefault (640x480).
    We pick the largest by width; fall back to any URL if width is
    missing; final fallback is info["thumbnail"].
    """
    thumbs = info.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        with_width = [
            t for t in thumbs
            if isinstance(t, dict) and t.get("url") and isinstance(t.get("width"), int)
        ]
        if with_width:
            best = max(with_width, key=lambda t: t["width"])
            return best["url"]
        # No width info → take the last entry (yt-dlp orders ascending in
        # most extractors, so the last is usually the largest).
        for t in reversed(thumbs):
            if isinstance(t, dict) and t.get("url"):
                return t["url"]
    return info.get("thumbnail")


async def fetch_metadata(url: str, cookies_path: Path | None) -> VideoMetadata:
    info = await asyncio.to_thread(_extract_info, url, cookies_path)
    raw_tags = info.get("tags") or []
    tags = tuple(t for t in raw_tags if isinstance(t, str) and t.strip())
    return VideoMetadata(
        id=info["id"],
        url=info.get("webpage_url", url),
        title=info.get("title", ""),
        description=info.get("description") or "",
        duration_seconds=info.get("duration"),
        thumbnail_url=_pick_best_thumbnail(info),
        tags=tags,
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


SubtitleSource = Literal["manual_subs", "auto_subs"]


def _extract_info_with_subs(url: str, cookies_path: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "de"],
        "subtitlesformat": "vtt",
    }
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)  # type: ignore[return-value]


async def _download_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


_VTT_TIMESTAMP = re.compile(r"\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}.*")
_VTT_TAG = re.compile(r"<[^>]+>")


def vtt_to_plain_text(vtt: str) -> str:
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "WEBVTT":
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        if _VTT_TIMESTAMP.match(line):
            continue
        if line.isdigit():
            continue
        line = _VTT_TAG.sub("", line)
        if line:
            lines.append(line)
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        if line not in seen:
            deduped.append(line)
            seen.add(line)
    return "\n".join(deduped)


def _pick_subtitle_url(info: dict[str, Any], key: str) -> str | None:
    subs = info.get(key) or {}
    for lang in ("en", "de"):
        for entry in subs.get(lang) or []:
            if entry.get("ext") == "vtt" and entry.get("url"):
                return entry["url"]
    for entries in subs.values():
        for entry in entries:
            if entry.get("ext") == "vtt" and entry.get("url"):
                return entry["url"]
    return None


async def _try_download_subtitle(url: str) -> str | None:
    """Wrap _download_text so that 429 and 5xx errors return None
    instead of raising. The caller treats None as 'no subtitle' and
    the pipeline falls back to Whisper.

    Other HTTP errors (auth, 4xx) are not transient and still bubble
    so we don't silently mask broken cookies."""
    try:
        return await _download_text(url)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 429 or 500 <= status < 600:
            log.warning(
                "Subtitle fetch transient error %s for %s — falling "
                "back to Whisper",
                status,
                url,
            )
            return None
        raise


async def fetch_subtitles(
    url: str, cookies_path: Path | None
) -> tuple[str, SubtitleSource] | None:
    info = await asyncio.to_thread(_extract_info_with_subs, url, cookies_path)
    manual_url = _pick_subtitle_url(info, "subtitles")
    if manual_url:
        text = await _try_download_subtitle(manual_url)
        if text is not None:
            return vtt_to_plain_text(text), "manual_subs"
    auto_url = _pick_subtitle_url(info, "automatic_captions")
    if auto_url:
        text = await _try_download_subtitle(auto_url)
        if text is not None:
            return vtt_to_plain_text(text), "auto_subs"
    return None


def _run_yt_dlp_download(opts: dict[str, Any], url: str) -> None:
    with YoutubeDL(opts) as ydl:
        ydl.download([url])


def _find_audio_file(audio_dir: Path, video_id: str) -> Path | None:
    for path in audio_dir.iterdir():
        if path.stem == video_id:
            return path
    return None


async def download_audio(
    url: str, video_id: str, audio_dir: Path, cookies_path: Path | None
) -> Path:
    await asyncio.to_thread(audio_dir.mkdir, parents=True, exist_ok=True)
    template = str(audio_dir / f"{video_id}.%(ext)s")
    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": template,
        "quiet": True,
        "no_warnings": True,
    }
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    await asyncio.to_thread(_run_yt_dlp_download, opts, url)
    path = await asyncio.to_thread(_find_audio_file, audio_dir, video_id)
    if path:
        return path
    raise RuntimeError(f"Audio download produced no file for {video_id}")
