import asyncio
from pathlib import Path

from app.models import TranscriptSource
from app.services.whisper import transcribe
from app.services.youtube import download_audio, fetch_subtitles


async def obtain_transcript(
    *,
    url: str,
    video_id: str,
    audio_dir: Path,
    cookies_path: Path | None,
    whisper_model: str,
) -> tuple[str, TranscriptSource]:
    subs = await fetch_subtitles(url, cookies_path=cookies_path)
    if subs is not None:
        text, source = subs
        return text, TranscriptSource(source)

    audio_path = await download_audio(url, video_id, audio_dir, cookies_path=cookies_path)
    try:
        text = await asyncio.to_thread(transcribe, audio_path, whisper_model)
    finally:
        if await asyncio.to_thread(audio_path.exists):
            await asyncio.to_thread(audio_path.unlink)
    return text, TranscriptSource.WHISPER
