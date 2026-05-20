"""Shared service layer for the REST API and MCP server.

Each function takes the db connection + parameters, returns a plain
Python value (dict / list / str). No HTTP, no MCP. Both surface
adapters serialize from these returns.
"""

import asyncio
from typing import Any, TypedDict

import aiosqlite

from app.config import Config
from app.models import VideoKind
from app.repos import jobs as jobs_repo
from app.repos import playlists as playlists_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services.reader import fetch_article
from app.services.url_classify import classify_url, web_id_from_url
from app.services.youtube import download_thumbnail, fetch_metadata


class VideoResource(TypedDict, total=False):
    id: str
    kind: str
    url: str
    title: str
    description: str
    thumbnail_url: str | None
    duration_seconds: int | None
    transcript_source: str | None
    summary_model: str | None
    summary_ready: bool
    tags: list[str]
    playlists: list[dict]
    job: dict | None
    created_at: str
    updated_at: str


def _video_to_resource(
    video,
    *,
    tag_names: list[str] | None = None,
    playlist_links: list[tuple[str, str]] | None = None,
    job=None,
    elapsed_s: int | None = None,
) -> VideoResource:
    return {
        "id": video.id,
        "kind": video.kind.value,
        "url": video.url,
        "title": video.title,
        "description": video.description,
        "thumbnail_url": (
            f"/thumbnails/{video.id}.jpg" if video.thumbnail_path else None
        ),
        "duration_seconds": video.duration_seconds,
        "transcript_source": (
            video.transcript_source.value if video.transcript_source else None
        ),
        "summary_model": video.summary_model,
        "summary_ready": bool(video.summary),
        "tags": tag_names or [],
        "playlists": [
            {"id": pid, "title": ptitle}
            for pid, ptitle in (playlist_links or [])
        ],
        "job": (
            {
                "state": job.state.value,
                "step": job.step,
                "error_message": job.error_message,
                "elapsed_seconds": elapsed_s,
            }
            if job
            else None
        ),
        "created_at": video.created_at.isoformat(),
        "updated_at": video.updated_at.isoformat(),
    }


async def get_video_resource(
    db: aiosqlite.Connection, video_id: str
) -> VideoResource | None:
    video = await videos_repo.get(db, video_id)
    if video is None:
        return None
    tags = await tags_repo.tags_for_video(db, video_id)
    plinks_map = await playlists_repo.playlists_for_videos(db, [video_id])
    plinks = plinks_map.get(video_id, [])
    job = await jobs_repo.latest_for_video(db, video_id)
    return _video_to_resource(
        video, tag_names=tags, playlist_links=plinks, job=job
    )


async def list_videos(
    db: aiosqlite.Connection,
    limit: int = 50,
    offset: int = 0,
    *,
    tag: str | None = None,
    playlist_id: str | None = None,
    user_id: int = 1,
) -> list[VideoResource]:
    if playlist_id:
        videos = await playlists_repo.videos_for_playlist(db, playlist_id)
        videos = videos[offset : offset + limit]
    else:
        videos = await videos_repo.list_recent(
            db, limit=limit + offset, tag=tag, user_id=user_id
        )
        videos = videos[offset : offset + limit]
    if not videos:
        return []
    ids = [v.id for v in videos]
    tags_map = await tags_repo.tags_for_videos(db, ids)
    plinks_map = await playlists_repo.playlists_for_videos(db, ids)
    return [
        _video_to_resource(
            v,
            tag_names=tags_map.get(v.id, []),
            playlist_links=plinks_map.get(v.id, []),
        )
        for v in videos
    ]


async def submit_video(
    db: aiosqlite.Connection,
    config: Config,
    *,
    url: str,
    user_id: int,
    wait: bool = False,
    wait_timeout: int = 60,
    llm_model_id: int | None = None,
    additional_prompt: str | None = None,
) -> VideoResource:
    """Submit a URL. Async by default; sync waits up to `wait_timeout` seconds
    for the summary to finish."""
    from app.models import TranscriptSource as TranscriptSrc
    from app.repos import tags as _tags_repo

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Not an http(s) URL: {url!r}")

    cookies = config.cookies_path if config.cookies_path.exists() else None

    if classify_url(url) == "youtube":
        meta = await fetch_metadata(url, cookies_path=cookies)
        item_id = f"{user_id}:{meta.id}"
        thumb_target = config.thumbnails_dir / f"{item_id}.jpg"
        await download_thumbnail(meta.thumbnail_url, thumb_target)
        thumb_db_path = str(thumb_target) if thumb_target.exists() else None
        await videos_repo.upsert_metadata(
            db,
            video_id=item_id,
            url=meta.url,
            title=meta.title,
            description=meta.description,
            thumbnail_path=thumb_db_path,
            duration_seconds=meta.duration_seconds,
            user_id=user_id,
            kind=VideoKind.YOUTUBE,
            youtube_id=meta.id,
        )
        if meta.tags:
            await _tags_repo.set_tags_for_video(db, item_id, list(meta.tags))
        await jobs_repo.enqueue(
            db, item_id,
            llm_model_id=llm_model_id,
            additional_prompt=additional_prompt,
        )
    else:
        article = await fetch_article(url)
        base_id = web_id_from_url(article.url)
        item_id = f"{user_id}:{base_id}"
        thumb_target = config.thumbnails_dir / f"{item_id}.jpg"
        thumb_db_path: str | None = None
        if article.thumbnail_url:
            try:
                await download_thumbnail(article.thumbnail_url, thumb_target)
                if thumb_target.exists():
                    thumb_db_path = str(thumb_target)
            except Exception:
                pass
        await videos_repo.upsert_metadata(
            db,
            video_id=item_id,
            url=article.url,
            title=article.title,
            description=article.description,
            thumbnail_path=thumb_db_path,
            duration_seconds=None,
            user_id=user_id,
            kind=VideoKind.WEB,
        )
        await videos_repo.set_transcript(db, item_id, article.body, TranscriptSrc.WEB)
        await jobs_repo.enqueue(
            db, item_id,
            llm_model_id=llm_model_id,
            additional_prompt=additional_prompt,
        )

    if wait and wait_timeout > 0:
        await _wait_for_summary(db, item_id, wait_timeout)

    resource = await get_video_resource(db, item_id)
    assert resource is not None
    return resource


async def _wait_for_summary(
    db: aiosqlite.Connection, video_id: str, max_seconds: int
) -> None:
    """Poll videos.summary every second up to `max_seconds` seconds."""
    deadline_iters = max(1, min(max_seconds, 300))
    for _ in range(deadline_iters):
        video = await videos_repo.get(db, video_id)
        if video and video.summary:
            return
        # also stop early if the latest job failed
        job = await jobs_repo.latest_for_video(db, video_id)
        if job and job.state.value == "failed":
            return
        await asyncio.sleep(1.0)


async def search_videos(
    db: aiosqlite.Connection,
    query: str,
    limit: int = 20,
    *,
    tag: str | None = None,
    user_id: int = 1,
) -> list[VideoResource]:
    """FTS-only search (the route layer is responsible for fusing in
    embeddings if it has the embedding service available, see
    routes/home.py for the pattern)."""
    videos = await videos_repo.search(
        db, query, limit=limit, tag=tag, user_id=user_id
    )
    if not videos:
        return []
    ids = [v.id for v in videos]
    tags_map = await tags_repo.tags_for_videos(db, ids)
    plinks_map = await playlists_repo.playlists_for_videos(db, ids)
    return [
        _video_to_resource(
            v,
            tag_names=tags_map.get(v.id, []),
            playlist_links=plinks_map.get(v.id, []),
        )
        for v in videos
    ]


async def chat_about_video(
    db: aiosqlite.Connection,
    video_id: str,
    content: str,
    *,
    user_id: int,
    llm_model_id: int | None = None,
) -> dict[str, Any]:
    """Append a user turn, run the LLM, persist the assistant turn,
    return both as a dict."""
    from app.repos import chat as chat_repo
    from app.repos import llm_models as llm_models_repo
    from app.services.chat import stream_reply

    video = await videos_repo.get(db, video_id)
    if video is None or video.transcript is None:
        raise ValueError("Video or transcript not found")
    model_row = (
        await llm_models_repo.get(db, llm_model_id)
        if llm_model_id is not None
        else await llm_models_repo.get_default(db)
    )
    if model_row is None:
        raise ValueError("LLM not configured")
    model = model_row.model
    api_key = model_row.api_key or ""
    base_url = model_row.base_url or None

    history = await chat_repo.history(db, video_id)
    await chat_repo.append(db, video_id, "user", content, user_id=user_id)

    collected: list[str] = []
    async for token in stream_reply(
        transcript=video.transcript,
        history=history,
        user_message=content,
        model=model,
        api_key=api_key,
        base_url=base_url,
    ):
        collected.append(token)
    answer = "".join(collected)
    await chat_repo.append(db, video_id, "assistant", answer, user_id=user_id)
    return {"answer": answer, "history_length": len(history) + 2}


async def list_playlists(
    db: aiosqlite.Connection, *, user_id: int
) -> list[dict[str, Any]]:
    playlists = await playlists_repo.list_for_user(db, user_id)
    out: list[dict[str, Any]] = []
    for p in playlists:
        videos = await playlists_repo.videos_for_playlist(db, p.id)
        out.append(
            {
                "id": p.id,
                "url": p.url,
                "title": p.title,
                "description": p.description,
                "video_count": len(videos),
                "last_refreshed_at": (
                    p.last_refreshed_at.isoformat()
                    if p.last_refreshed_at else None
                ),
                "created_at": p.created_at.isoformat(),
            }
        )
    return out


async def list_tags(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await db.execute(
        """
        SELECT t.name, COUNT(vt.video_id) AS n
        FROM tags t
        LEFT JOIN video_tags vt ON vt.tag_id = t.id
        GROUP BY t.id
        HAVING n > 0
        ORDER BY n DESC, t.name COLLATE NOCASE
        """
    )
    rows = await cursor.fetchall()
    return [{"name": row[0], "count": row[1]} for row in rows]


async def reindex_video(
    db: aiosqlite.Connection,
    video_id: str,
    *,
    llm_model_id: int | None = None,
    additional_prompt: str | None = None,
) -> None:
    if await videos_repo.get(db, video_id) is None:
        raise ValueError(f"Unknown video: {video_id}")
    await jobs_repo.enqueue(
        db, video_id,
        llm_model_id=llm_model_id,
        additional_prompt=additional_prompt,
    )
