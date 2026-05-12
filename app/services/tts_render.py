"""Piper -> ffmpeg -> MP3 pipeline.

This module renders text to MP3 audio via two stages:

1. ``piper-tts`` synthesises raw WAV from text using a downloaded voice
   model (``.onnx`` + ``.onnx.json``).
2. ``ffmpeg`` re-encodes WAV to MP3 (single chunk) or concatenates
   multiple WAVs into one MP3 (long-form, chunked rendering).

Synthesis is CPU-bound and not natively async, so we run the Piper
calls in a worker thread via :func:`asyncio.to_thread`. ffmpeg is
invoked as a subprocess via :func:`asyncio.create_subprocess_exec`.

API note: targets ``piper-tts >= 1.4`` (installed: 1.4.2). The
``PiperVoice.synthesize_wav(text, wav_file)`` method requires a
``wave.Wave_write`` handle, not a generic binary file object, so we
open the temp WAV via :mod:`wave` rather than ``Path.open("wb")``.
"""
from __future__ import annotations

import asyncio
import subprocess
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from piper import PiperVoice


def _render_wav_sync(voice_file: Path, text: str, out_wav: Path) -> None:
    """Load the voice and write a single WAV file. Blocking."""
    voice = PiperVoice.load(str(voice_file))
    with wave.open(str(out_wav), "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)


async def _wav_to_mp3(wav: Path, mp3: Path) -> None:
    """Re-encode a single WAV to MP3 at 128 kbps."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(wav), "-b:a", "128k", str(mp3),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode()}")


async def render_text_to_mp3(
    text: str, voice_file: Path, out_path: Path
) -> None:
    """Render a single text string to an MP3 file at ``out_path``.

    The voice model is loaded fresh per call. Parent directories of
    ``out_path`` are created if needed.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as td:
        wav = Path(td) / "raw.wav"
        await asyncio.to_thread(_render_wav_sync, voice_file, text, wav)
        await _wav_to_mp3(wav, out_path)


async def render_chunks_to_mp3(
    chunks: list[str], voice_file: Path, out_path: Path
) -> None:
    """Render a list of chunks into a single concatenated MP3.

    Each chunk is synthesised to its own WAV under a temporary
    directory, then ffmpeg's concat demuxer stitches them in order
    and encodes the result to MP3 at 128 kbps.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as td:
        td_path = Path(td)
        wavs: list[Path] = []
        for i, chunk in enumerate(chunks):
            w = td_path / f"chunk_{i:03d}.wav"
            await asyncio.to_thread(_render_wav_sync, voice_file, chunk, w)
            wavs.append(w)
        manifest = td_path / "concat.txt"
        manifest.write_text(
            "\n".join(f"file '{w}'" for w in wavs) + "\n"
        )
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-b:a", "128k", str(out_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {err.decode()}")
