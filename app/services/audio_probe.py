import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


async def probe_duration(audio_path: Path) -> float | None:
    """Audio length in seconds via ffprobe, or None when ffprobe is
    missing or its output can't be parsed. Reads only the container
    header, so it is cheap even for multi-hour files."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("ffprobe not found on PATH; audio duration unknown")
        return None
    out, _err = await proc.communicate()
    try:
        return float(out.decode().strip())
    except (ValueError, UnicodeDecodeError):
        return None


async def probe_duration_seconds(audio_path: Path) -> int | None:
    """Like probe_duration, truncated to whole seconds."""
    seconds = await probe_duration(audio_path)
    return int(seconds) if seconds is not None else None
