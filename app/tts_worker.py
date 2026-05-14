"""Background worker that drains the ``tts_jobs`` queue.

Mirrors :class:`app.worker.Worker` but for the TTS pipeline (translate
→ download voice → render → ffprobe duration). Three collaborators
are injected via the constructor so tests can stub them without
touching LiteLLM, Hugging Face, or Piper:

* ``translate(text, *, source_language, target_language, complete, progress=...)``
* ``render_chunks_to_mp3(chunks, voice_file, out_path, length_scale=..., progress=...)``
* ``ensure_voice(language, voice, quality) -> Path``

The first active state in the tts_jobs state machine is
``translating``: :func:`app.repos.tts_jobs.claim_next` lands jobs
there. When the source language matches the target (or no source
language is known) the worker skips translation entirely — no
:func:`translate` call, ``translated_text`` stays ``None`` — and
flips status straight to ``rendering`` via :func:`set_status`.

The render path is chunked: text is split at sentence boundaries via
:func:`_split_into_sentence_chunks` so the worker can report per-chunk
progress to the UI while a long render is in flight. Single-sentence
texts collapse to a one-chunk list and still go through the same
path — no branching here.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import aiosqlite
import litellm

if TYPE_CHECKING:
    from app.services.heartbeat import HeartbeatRegistry

from app.config import Config
from app.models import TtsJob
from app.repos import settings as settings_repo
from app.repos import tts_jobs as tts_jobs_repo
from app.repos import videos as videos_repo
from app.services.tts_render import _split_into_sentence_chunks

log = logging.getLogger(__name__)


class _TranslateFn(Protocol):
    async def __call__(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
        complete: Callable[[str], Awaitable[str]],
        progress: Callable[[int, int], None] | None = ...,
    ) -> str: ...


class RenderChunksFn(Protocol):
    async def __call__(
        self,
        chunks: list[str],
        voice_file: Path,
        out_path: Path,
        length_scale: float | None = ...,
        progress: Callable[[int, int], None] | None = ...,
    ) -> None: ...


EnsureVoiceFn = Callable[[str, str, str], Awaitable[Path]]


class TtsWorker:
    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        config: Config,
        translate: _TranslateFn,
        render_chunks_to_mp3: RenderChunksFn,
        ensure_voice: EnsureVoiceFn,
        poll_interval: float = 1.0,
        heartbeat: HeartbeatRegistry | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._translate = translate
        self._render_chunks = render_chunks_to_mp3
        self._ensure_voice = ensure_voice
        self._poll_interval = poll_interval
        self._heartbeat = heartbeat
        self._stopped = asyncio.Event()

    @property
    def poll_interval_seconds(self) -> float:
        """Public read-only accessor — the diagnostics page uses this
        to compute the alive/stale threshold (3 × poll_interval)."""
        return self._poll_interval

    def _touch(
        self, *, current_job_id: int | None = None, current_step: str | None = None,
    ) -> None:
        if self._heartbeat is not None:
            self._heartbeat.touch(
                "tts_worker",
                current_job_id=current_job_id,
                current_step=current_step,
            )

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            job = await tts_jobs_repo.claim_next(self._db)
            if job is None:
                self._touch()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopped.wait(), self._poll_interval
                    )
                continue
            try:
                self._touch(current_job_id=job.id, current_step=job.step or "starting")
                audio_rel, duration, translated = await self._process(job)
                await tts_jobs_repo.complete(
                    self._db,
                    job.id,
                    audio_path=audio_rel,
                    duration_seconds=duration,
                    translated_text=translated,
                )
            except Exception as exc:  # noqa: BLE001 — surfaced via .fail()
                log.exception("tts job %s failed", job.id)
                await tts_jobs_repo.fail(self._db, job.id, str(exc))

    async def _process(self, job: TtsJob) -> tuple[str, float | None, str | None]:
        """Translate (if needed) → fetch voice → render → ffprobe.

        Returns ``(audio_path_relative_to_data_dir, duration_seconds,
        translated_text_or_None)`` for :func:`tts_jobs_repo.complete`.
        """
        job_id = job.id

        async def set_step(step: str) -> None:
            await tts_jobs_repo.set_step(self._db, job_id, step)
            self._touch(current_job_id=job_id, current_step=step)

        video = await videos_repo.get(self._db, job.video_id)
        if video is None:
            raise RuntimeError(f"video {job.video_id} not found")

        if job.source == "summary":
            text = video.summary
            source_lang = (
                video.summary_language
                or video.source_language
            )
        elif job.source == "transcript":
            text = video.transcript
            source_lang = (
                video.transcript_language
                or video.source_language
            )
        else:
            raise RuntimeError(f"unknown tts source: {job.source!r}")
        if not text:
            raise RuntimeError(
                f"video {job.video_id} has no {job.source} text to render"
            )

        target_lang = job.target_language

        # Step 1: translate if source and target differ. When source
        # language is unknown we skip translation rather than risk
        # asking the LLM to translate from a wrong assumed language.
        #
        # If a previous run already produced a translation (and crashed
        # before completing the render), reuse it instead of paying for
        # the LLM call a second time. The `translated_text` column is
        # written immediately after translate() returns — see below.
        translated_text: str | None = job.translated_text
        needs_translation = bool(
            source_lang
            and source_lang != target_lang
            and not translated_text
        )
        if needs_translation:
            await set_step("translating")
            complete = await self._build_complete()
            chunks_step_total = {"n": 0}

            def _progress(done: int, total: int) -> None:
                # Schedule the async set_step from a sync callback.
                # We fire-and-forget — losing the occasional progress
                # tick is preferable to blocking translate().
                chunks_step_total["n"] = total
                asyncio.create_task(  # noqa: RUF006
                    set_step(f"translating chunk {done}/{total}")
                )

            translated_text = await self._translate(
                text,
                source_language=source_lang,
                target_language=target_lang,
                complete=complete,
                progress=_progress,
            )
            # Persist immediately so a restart between here and complete()
            # doesn't waste the LLM call. complete() will rewrite the
            # same value at job end — harmless idempotent double-write.
            await tts_jobs_repo.set_translated_text(
                self._db, job_id, translated_text,
            )
        elif translated_text:
            # Resuming from a prior crashed run — signal the reuse in the UI.
            await set_step("translation reused from prior run")

        final_text = translated_text if translated_text else text

        # Step 2: status → rendering. Mirror to the heartbeat so the
        # diagnostics page reflects the transition without a DB read,
        # consistent with the set_step() mirror above.
        await tts_jobs_repo.set_status(self._db, job_id, "rendering")
        self._touch(current_job_id=job_id, current_step="rendering")

        # Step 3: ensure voice file is downloaded.
        await set_step("downloading voice")
        voice_path = await self._ensure_voice(
            target_lang, job.voice, job.quality,
        )

        # Step 4: render to MP3. The global speech-speed setting is
        # read at render time (cheap query, keeps the worker reactive
        # to settings changes mid-run, same pattern as _build_complete).
        # An unparseable value is logged and ignored so a bad setting
        # can't wedge the queue — render simply falls back to the
        # voice's built-in default.
        settings = await settings_repo.get_all(self._db)
        length_scale_raw = settings.get(
            "default_tts_length_scale", ""
        ).strip()
        length_scale: float | None = None
        if length_scale_raw:
            try:
                length_scale = float(length_scale_raw)
            except ValueError:
                log.warning(
                    "ignoring invalid default_tts_length_scale=%r",
                    length_scale_raw,
                )
        await set_step("rendering audio")
        out_path = (
            self._config.tts_audio_dir
            / job.video_id
            / f"{job.source}-{target_lang}-{job.voice}-{job.quality}.mp3"
        )
        chunks = _split_into_sentence_chunks(final_text)
        # Capture the running loop BEFORE entering the render: the
        # progress callback fires synchronously from inside the
        # render coroutine, and we want to schedule an async
        # ``set_step`` from it. Using ``run_coroutine_threadsafe`` is
        # safe regardless of whether the callback eventually fires
        # from the loop thread or a worker thread — Piper synth uses
        # ``asyncio.to_thread`` under the hood, and a future refactor
        # could relocate the callback off-loop. Unlike the translate
        # progress callback (which always runs on-loop and uses
        # ``create_task``), this one is defensive.
        loop = asyncio.get_running_loop()

        def _on_chunk(done: int, total: int) -> None:
            asyncio.run_coroutine_threadsafe(  # noqa: RUF006
                set_step(f"rendering audio chunk {done}/{total}"), loop,
            )

        await self._render_chunks(
            chunks=chunks,
            voice_file=voice_path,
            out_path=out_path,
            length_scale=length_scale,
            progress=_on_chunk,
        )

        # Step 5: ffprobe for duration (best-effort, nullable).
        duration = await _probe_duration(out_path)

        audio_rel = str(out_path.relative_to(self._config.data_dir))
        return audio_rel, duration, translated_text

    async def _build_complete(self) -> Callable[[str], Awaitable[str]]:
        """Build the LLM completion closure that :func:`translate`
        expects. Reads model / api_key / base_url from settings on
        each job (cheap query, keeps the worker reactive to
        settings changes mid-run).

        The settings check is deferred to call time: the closure may
        never actually fire (e.g. if the translator chunks an empty
        body or — in tests — ignores ``complete`` entirely), in which
        case requiring ``llm_model`` would be a false-positive fail.

        Inlined here rather than importing the private ``_completion``
        in :mod:`app.services.summarizer`. When a third caller appears
        this should move to a shared ``app.services.llm`` module.
        """
        settings = await settings_repo.get_all(self._db)
        model = settings.get("llm_model")
        api_key = settings.get("llm_api_key") or ""
        base_url = settings.get("llm_base_url")

        async def _complete(prompt: str) -> str:
            if not model:
                raise RuntimeError(
                    "TTS translation requires `llm_model` in settings"
                )
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "api_key": api_key,
            }
            if base_url:
                kwargs["api_base"] = base_url
            response = await litellm.acompletion(**kwargs)
            return response.choices[0].message.content or ""

        return _complete


async def _probe_duration(mp3_path: Path) -> float | None:
    """Return the audio duration in seconds via ``ffprobe``, or
    ``None`` if ffprobe isn't installed / the output is unparseable
    (the column is nullable — a missing duration just means the UI
    won't show a length pill)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(mp3_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("ffprobe not found on PATH; duration_seconds left NULL")
        return None
    out, _err = await proc.communicate()
    try:
        return float(out.decode().strip())
    except (ValueError, UnicodeDecodeError):
        log.warning(
            "ffprobe gave unparseable output for %s; duration left NULL",
            mp3_path,
        )
        return None
