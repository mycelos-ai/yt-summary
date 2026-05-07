import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.models import TranscriptSource
from app.services.whisper import transcribe
from app.services.youtube import download_audio, fetch_subtitles

# Don't update job.step more than once every 3 seconds. Whisper yields
# many segments per minute on a Pi5; without throttling we'd hammer
# SQLite with no UI benefit (the HTMX poll only ticks every 2s anyway).
_PROGRESS_MIN_INTERVAL_S = 3.0


def _format_progress(current: float, total: float) -> str:
    def hms(seconds: float) -> str:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"

    if total <= 0:
        return f"transcribing {hms(current)}"
    pct = int(round(current / total * 100))
    return f"transcribing {hms(current)} / {hms(total)} ({pct}%)"


def _build_whisper_progress(
    progress_cb: Callable[[str], Awaitable[None]] | None,
    loop: asyncio.AbstractEventLoop,
):
    """Return a sync (current, total) callback that schedules the async
    progress_cb on the given loop. Throttled to one update every
    _PROGRESS_MIN_INTERVAL_S seconds plus the final 100% report."""
    if progress_cb is None:
        return None

    state = {"last_emit": 0.0}

    def on_segment(current: float, total: float) -> None:
        now = time.monotonic()
        is_final = total > 0 and current >= total
        if not is_final and now - state["last_emit"] < _PROGRESS_MIN_INTERVAL_S:
            return
        state["last_emit"] = now
        message = _format_progress(current, total)
        # Whisper runs in a worker thread; bounce the coroutine back
        # onto the main loop. Any failure (loop closed, etc.) is fine
        # to swallow — progress is best-effort.
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(progress_cb(message), loop)

    return on_segment


async def obtain_transcript(
    *,
    url: str,
    video_id: str,
    audio_dir: Path,
    cookies_path: Path | None,
    whisper_model: str,
    progress_cb: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, TranscriptSource]:
    subs = await fetch_subtitles(url, cookies_path=cookies_path)
    if subs is not None:
        text, source = subs
        return text, TranscriptSource(source)

    audio_path = await download_audio(url, video_id, audio_dir, cookies_path=cookies_path)
    loop = asyncio.get_running_loop()
    whisper_progress = _build_whisper_progress(progress_cb, loop)
    try:
        text = await asyncio.to_thread(
            transcribe, audio_path, whisper_model, progress=whisper_progress
        )
    finally:
        if await asyncio.to_thread(audio_path.exists):
            await asyncio.to_thread(audio_path.unlink)
    return text, TranscriptSource.WHISPER
