import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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


async def fetch_subtitles(
    url: str, cookies_path: Path | None
) -> tuple[str, SubtitleSource] | None:
    info = await asyncio.to_thread(_extract_info_with_subs, url, cookies_path)
    manual_url = _pick_subtitle_url(info, "subtitles")
    if manual_url:
        text = await _download_text(manual_url)
        return vtt_to_plain_text(text), "manual_subs"
    auto_url = _pick_subtitle_url(info, "automatic_captions")
    if auto_url:
        text = await _download_text(auto_url)
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
