import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite

from app.config import Config
from app.models import TranscriptSource, VideoKind
from app.repos import embeddings as embeddings_repo
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services.embeddings import embed_text
from app.services.reader import fetch_article
from app.services.summarizer import summarize
from app.services.transcript import obtain_transcript
from app.services.youtube import fetch_metadata

log = logging.getLogger(__name__)


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
        if video.kind == VideoKind.WEB:
            await set_step("fetching article")
            article = await fetch_article(video.url)
            await videos_repo.set_transcript(
                db, video_id, article.body, TranscriptSource.WEB
            )
            text = article.body
        else:
            await set_step("fetching transcript")
            text, source = await obtain_transcript(
                url=video.url,
                video_id=video_id,
                audio_dir=config.audio_dir,
                cookies_path=cookies,
                whisper_model=whisper_model,
                progress_cb=set_step,
                whisper_base_url=settings.get("whisper_base_url", ""),
                whisper_api_key=settings.get("whisper_api_key", ""),
            )
            await videos_repo.set_transcript(db, video_id, text, source)
    else:
        text = video.transcript

    # Backfill tags only for YouTube videos (yt-dlp surfaces them).
    # Web pages have no equivalent metadata field worth chasing.
    if video.kind == VideoKind.YOUTUBE:
        existing_tags = await tags_repo.tags_for_video(db, video_id)
        if not existing_tags:
            try:
                meta = await fetch_metadata(video.url, cookies_path=cookies)
                if meta.tags:
                    await tags_repo.set_tags_for_video(
                        db, video_id, list(meta.tags)
                    )
            except Exception:
                # Tag backfill is a nice-to-have; don't fail the whole job.
                pass

    if not model:
        await set_step("transcript only (no LLM model configured)")
        return

    await set_step("summarizing")

    async def _persist_partial(partial: str) -> None:
        # Map-reduce: surface progress to the UI by writing the working
        # summary back to the videos row. The detail page polls this
        # while the job is running.
        await videos_repo.set_summary(db, video_id, partial, model)

    # Playlists this video lives in are topical hints — the user files
    # videos thematically (e.g. "AI", "Long-form interviews"), so we
    # surface those names to the summarizer for better focus.
    playlist_links = await playlists_repo.playlists_for_videos(db, [video_id])
    playlist_context = [title for _id, title in playlist_links.get(video_id, [])]

    summary = await summarize(
        transcript=text,
        model=model,
        api_key=api_key or "",
        base_url=base_url,
        title=video.title,
        description=video.description,
        language=settings.get("summary_language"),
        extra_instructions=settings.get("summary_extra_instructions"),
        playlist_context=playlist_context or None,
        progress=set_step,
        on_partial=_persist_partial,
    )
    await videos_repo.set_summary(db, video_id, summary, model)
    await _try_embed_summary(db, video_id, summary, settings, set_step)


async def _try_embed_summary(
    db: aiosqlite.Connection,
    video_id: str,
    summary: str,
    settings: dict[str, str],
    set_step: Callable[[str], Awaitable[None]],
) -> None:
    """Best-effort: embed the new summary so semantic search picks it up.

    A failure here is logged but does NOT fail the job — the user
    still has their summary, only semantic search is degraded.
    """
    embedding_model = settings.get("embedding_model", "").strip() or None
    embedding_base_url = (
        settings.get("embedding_base_url", "").strip()
        or settings.get("llm_base_url", "").strip()
        or None
    )
    api_key = settings.get("llm_api_key", "")
    try:
        await set_step("embedding summary")
        vector = await embed_text(
            summary,
            model=embedding_model,
            api_key=api_key,
            base_url=embedding_base_url,
        )
        await embeddings_repo.upsert_summary_embedding(db, video_id, vector)
    except Exception as e:
        log.warning(
            "summary embedding failed for %s: %s: %s",
            video_id,
            type(e).__name__,
            e,
        )
