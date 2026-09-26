import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.models import TranscriptSource
from app.services.audio_chunks import extract_chunk
from app.services.audio_probe import probe_duration_seconds
from app.services.whisper import transcribe, transcribe_via_api
from app.services.youtube import download_audio, fetch_subtitles

# Don't update job.step more than once every 3 seconds. Whisper yields
# many segments per minute on a Pi5; without throttling we'd hammer
# SQLite with no UI benefit (the HTMX poll only ticks every 2s anyway).
_PROGRESS_MIN_INTERVAL_S = 3.0

# Audio longer than this is transcribed in chunks of this length.
# faster-whisper holds its whole input in RAM, so chunking keeps memory
# flat on a Pi; hosted endpoints get uploads under their size limit.
CHUNK_S = 600


class TranscriptTooLongError(RuntimeError):
    """Raised when a video has no subtitles and is longer than the
    local-Whisper duration cap. Surfaces as the job's error message."""


def _check_whisper_cap(duration_seconds: int, max_whisper_duration_s: int) -> None:
    if 0 < max_whisper_duration_s < duration_seconds:
        raise TranscriptTooLongError(
            f"no subtitles and video is {duration_seconds // 60} min long; "
            f"local Whisper is capped at {max_whisper_duration_s // 60} min "
            "(YTS_WHISPER_MAX_DURATION_S). Configure a hosted Whisper "
            "backend in Settings or raise the cap."
        )


def _offset_progress(progress, offset: float, total: int | None):
    """Map a chunk's (current, chunk_total) progress onto the whole
    video: current is shifted by the chunk offset, total becomes the
    full length."""
    if progress is None:
        return None
    if total is None:
        return progress

    def shifted(current: float, _chunk_total: float) -> None:
        progress(offset + current, float(total))

    return shifted


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
            async def _deliver() -> None:
                await progress_cb(message)

            asyncio.run_coroutine_threadsafe(_deliver(), loop)

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
    duration_seconds: int | None = None,
    max_whisper_duration_s: int = 0,
    whisper_cpu_threads: int = 0,
    duration_cb: Callable[[int], Awaitable[None]] | None = None,
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

    Local Whisper only: when `max_whisper_duration_s` > 0 and the
    video's known `duration_seconds` exceeds it, raise
    TranscriptTooLongError before downloading any audio. A 1-hour
    video pins a Pi5 for about an hour; this keeps the box usable.
    `whisper_cpu_threads` is forwarded to faster-whisper (0 = all
    cores).

    When `duration_seconds` is unknown (e.g. playlist entries from the
    YouTube Data API), the downloaded audio is probed instead: the cap
    is enforced before Whisper starts, and the measured length is
    handed to `duration_cb` so the caller can persist it.
    """
    subs = await fetch_subtitles(url, cookies_path=cookies_path)
    if subs is not None:
        text, segments, source, language = subs
        return text, segments, TranscriptSource(source), language

    if not whisper_base_url and duration_seconds is not None:
        _check_whisper_cap(duration_seconds, max_whisper_duration_s)

    audio_path = await download_audio(url, video_id, audio_dir, cookies_path=cookies_path)
    chunk_dir = audio_dir / f"{video_id}.chunks"
    try:
        if duration_seconds is None:
            duration_seconds = await probe_duration_seconds(audio_path)
            if duration_seconds is not None:
                if duration_cb is not None:
                    await duration_cb(duration_seconds)
                if not whisper_base_url:
                    _check_whisper_cap(duration_seconds, max_whisper_duration_s)

        # Unknown length (ffprobe missing) falls back to one pass over
        # the whole file, same as before chunking existed.
        chunked = duration_seconds is not None and duration_seconds > CHUNK_S
        n_expected = -(-(duration_seconds or 0) // CHUNK_S)

        texts: list[str] = []
        segments: list[tuple[float, str]] = []
        language: str | None = None
        loop = asyncio.get_running_loop()
        whisper_progress = _build_whisper_progress(progress_cb, loop)
        index = 0
        while True:
            if chunked:
                # Loop until ffmpeg finds no samples left rather than
                # trusting the metadata length, so no tail is lost.
                chunk = await extract_chunk(
                    audio_path,
                    chunk_dir / f"chunk_{index:03d}.flac",
                    start_s=index * CHUNK_S,
                    length_s=CHUNK_S,
                )
                if chunk is None:
                    break
            elif index == 0:
                chunk = audio_path
            else:
                break
            offset = float(index * CHUNK_S)
            if whisper_base_url:
                if progress_cb is not None:
                    part = f" (part {index + 1}/{n_expected})" if chunked else ""
                    await progress_cb(f"sending audio to {whisper_base_url}{part}")
                text, chunk_segments, chunk_lang = await transcribe_via_api(
                    chunk,
                    base_url=whisper_base_url,
                    api_key=whisper_api_key,
                    model_name=whisper_model,
                    language=language,
                )
            else:
                text, chunk_segments, chunk_lang = await asyncio.to_thread(
                    transcribe,
                    chunk,
                    whisper_model,
                    progress=_offset_progress(
                        whisper_progress, offset, duration_seconds
                    ),
                    cpu_threads=whisper_cpu_threads,
                    language=language,
                )
            if text:
                texts.append(text)
            segments.extend((start + offset, seg) for start, seg in chunk_segments)
            language = language or chunk_lang
            if chunk != audio_path:
                await asyncio.to_thread(chunk.unlink, missing_ok=True)
            index += 1
    finally:
        if await asyncio.to_thread(audio_path.exists):
            await asyncio.to_thread(audio_path.unlink)
        await asyncio.to_thread(shutil.rmtree, chunk_dir, ignore_errors=True)
    return " ".join(texts), segments, TranscriptSource.WHISPER, language
