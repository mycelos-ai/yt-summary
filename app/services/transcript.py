import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.models import TranscriptSource
from app.services.whisper import transcribe, transcribe_via_api
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
    whisper_base_url: str = "",
    whisper_api_key: str = "",
) -> tuple[str, list[tuple[float, str]], TranscriptSource, str | None]:
    """Obtain a transcript for `url`.

    Returns (plain_text, segments, source, language) where:
      * segments is a list of (start_seconds, text) tuples. Empty
        list if the transcript source didn't expose timing.
      * language is the BCP-47-ish two-letter code surfaced by the
        transcript backend — VTT `Language:` header on the subs
        path, faster-whisper / hosted-Whisper detection on the
        audio path. None when the backend didn't surface one.

    Tries YouTube subtitles first. Falls back to Whisper. If
    `whisper_base_url` is set, audio goes to a hosted endpoint
    instead of local faster-whisper.
    """
    subs = await fetch_subtitles(url, cookies_path=cookies_path)
    if subs is not None:
        text, segments, source, language = subs
        return text, segments, TranscriptSource(source), language

    audio_path = await download_audio(url, video_id, audio_dir, cookies_path=cookies_path)
    try:
        if whisper_base_url:
            if progress_cb is not None:
                await progress_cb(f"sending audio to {whisper_base_url}")
            text, segments, language = await transcribe_via_api(
                audio_path,
                base_url=whisper_base_url,
                api_key=whisper_api_key,
                model_name=whisper_model,
            )
        else:
            loop = asyncio.get_running_loop()
            whisper_progress = _build_whisper_progress(progress_cb, loop)
            text, segments, language = await asyncio.to_thread(
                transcribe, audio_path, whisper_model, progress=whisper_progress
            )
    finally:
        if await asyncio.to_thread(audio_path.exists):
            await asyncio.to_thread(audio_path.unlink)
    return text, segments, TranscriptSource.WHISPER, language
