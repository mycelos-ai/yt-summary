import asyncio
from pathlib import Path

from app.services.audio_probe import probe_duration


async def extract_chunk(
    audio_path: Path, out_path: Path, *, start_s: int, length_s: int
) -> Path | None:
    """Write `length_s` seconds of `audio_path` from `start_s` on to
    `out_path` as 16 kHz mono FLAC. Returns None when `start_s` lies
    past the end of the audio (no samples left).

    faster-whisper decodes its whole input into RAM as float32, so a
    2-hour video needs about 1 GB before the model even runs. Feeding
    it fixed-length chunks keeps memory flat regardless of length.
    FLAC is lossless and keeps a 10-minute chunk around 10 MB — under
    the 25 MB upload limit of hosted Whisper endpoints.

    One ffmpeg call per chunk instead of the segment muxer: the muxer
    leaves FLAC headers without a valid duration, which hosted
    endpoints may reject. `-ss` before `-i` with re-encoding is
    sample-accurate, so chunk i starts at exactly i * length_s.

    Raises RuntimeError if ffmpeg is missing or fails.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-v", "error",
            "-nostdin",
            "-y",
            "-ss", str(start_s),
            "-t", str(length_s),
            "-i", str(audio_path),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "flac",
            str(out_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not found on PATH; cannot chunk audio") from exc
    _, err = await proc.communicate()
    if proc.returncode != 0:
        detail = err.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"ffmpeg failed to chunk audio: {detail[0]}")
    # Past the end ffmpeg still exits 0 but writes a header-only file.
    seconds = await probe_duration(out_path)
    if seconds is None or seconds <= 0:
        await asyncio.to_thread(out_path.unlink, missing_ok=True)
        return None
    return out_path
