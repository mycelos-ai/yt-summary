import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite

from app.config import Config
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services.summarizer import summarize
from app.services.transcript import obtain_transcript


def _resolve_cookies(config: Config) -> Path | None:
    p = config.cookies_path
    return p if p.exists() else None


async def process_video(
    db: aiosqlite.Connection,
    config: Config,
    video_id: str,
    set_step: Callable[[str], Awaitable[None]],
) -> None:
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise RuntimeError(f"Video {video_id} not found")

    settings = await settings_repo.get_all(db)
    model = settings.get("llm_model")
    api_key = settings.get("llm_api_key")
    base_url = settings.get("llm_base_url")
    whisper_model = settings.get("whisper_model", "small")

    cookies = await asyncio.to_thread(_resolve_cookies, config)

    if not video.transcript:
        await set_step("fetching transcript")
        text, source = await obtain_transcript(
            url=video.url,
            video_id=video_id,
            audio_dir=config.audio_dir,
            cookies_path=cookies,
            whisper_model=whisper_model,
        )
        await videos_repo.set_transcript(db, video_id, text, source)
    else:
        text = video.transcript

    if not model:
        await set_step("transcript only (no LLM model configured)")
        return

    await set_step("summarizing")

    async def _persist_partial(partial: str) -> None:
        # Map-reduce: surface progress to the UI by writing the working
        # summary back to the videos row. The detail page polls this
        # while the job is running.
        await videos_repo.set_summary(db, video_id, partial, model)

    summary = await summarize(
        transcript=text,
        model=model,
        api_key=api_key or "",
        base_url=base_url,
        title=video.title,
        description=video.description,
        language=settings.get("summary_language"),
        extra_instructions=settings.get("summary_extra_instructions"),
        progress=set_step,
        on_partial=_persist_partial,
    )
    await videos_repo.set_summary(db, video_id, summary, model)
