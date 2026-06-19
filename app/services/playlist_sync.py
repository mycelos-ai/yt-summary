import asyncio
import logging
from dataclasses import dataclass

import aiosqlite

from app.config import Config
from app.repos import jobs as jobs_repo
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services import playlist_index
from app.services.playlist import PlaylistEntry, PlaylistMetadata, fetch_playlist
from app.services.youtube import download_thumbnail

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    total_in_playlist: int
    newly_linked: int
    newly_enqueued: int


async def _resolve_cookies(config: Config):
    p = config.cookies_path
    exists = await asyncio.to_thread(p.exists)
    return p if exists else None


async def _index_playlist(db, config, playlist) -> "PlaylistMetadata":
    """Index a playlist's entries: Data API when a youtube_api_key is set and
    succeeds, else (or on API error) the yt-dlp fetch_playlist path."""
    api_key = await settings_repo.get_for_user(
        db, playlist.user_id, "youtube_api_key",
    )
    if api_key:
        try:
            return await playlist_index.fetch_via_api(
                playlist.url, api_key=api_key,
            )
        except playlist_index.PlaylistApiError as e:
            log.warning(
                "YouTube API index failed for %s, falling back to yt-dlp: %s",
                playlist.id, e,
            )
    cookies = await _resolve_cookies(config)
    return await fetch_playlist(playlist.url, cookies_path=cookies)


async def _process_entries(
    db: aiosqlite.Connection,
    config: Config,
    playlist_id: str,
    user_id: int,
    entries: list[PlaylistEntry],
) -> SyncResult:
    """Common logic shared by sync_playlist and load_older_videos.

    Caller is responsible for filtering / slicing the entries list.
    """
    newly_linked = 0
    newly_enqueued = 0
    for entry in entries:
        existing = await videos_repo.get(db, entry.id)
        if existing is None:
            thumb_target = config.thumbnails_dir / f"{entry.id}.jpg"
            await download_thumbnail(entry.thumbnail_url, thumb_target)
            thumb_db_path = str(thumb_target) if thumb_target.exists() else None
            await videos_repo.upsert_metadata(
                db,
                video_id=entry.id,
                url=f"https://www.youtube.com/watch?v={entry.id}",
                title=entry.title,
                description=entry.description,
                thumbnail_path=thumb_db_path,
                duration_seconds=entry.duration_seconds,
                user_id=user_id,
            )
            existing = await videos_repo.get(db, entry.id)

        if await playlists_repo.link_video(db, playlist_id, entry.id, position=entry.position):
            newly_linked += 1
            assert existing is not None
            if existing.summary is None:
                await jobs_repo.enqueue(db, entry.id)
                newly_enqueued += 1

    return SyncResult(
        total_in_playlist=0,  # caller will fill in
        newly_linked=newly_linked,
        newly_enqueued=newly_enqueued,
    )


async def sync_playlist(
    db: aiosqlite.Connection,
    config: Config,
    playlist_id: str,
    *,
    initial_limit: int | None = None,
) -> SyncResult:
    playlist = await playlists_repo.get(db, playlist_id)
    if playlist is None:
        raise KeyError(f"Unknown playlist: {playlist_id}")

    meta = await _index_playlist(db, config, playlist)
    total = len(meta.entries)
    entries = meta.entries[:initial_limit] if initial_limit else meta.entries

    result = await _process_entries(
        db, config, playlist_id, playlist.user_id, entries
    )
    result.total_in_playlist = total
    await playlists_repo.set_last_refreshed(db, playlist_id)
    return result


async def load_older_videos(
    db: aiosqlite.Connection,
    config: Config,
    playlist_id: str,
    *,
    count: int,
) -> SyncResult:
    playlist = await playlists_repo.get(db, playlist_id)
    if playlist is None:
        raise KeyError(f"Unknown playlist: {playlist_id}")

    meta = await _index_playlist(db, config, playlist)
    total = len(meta.entries)
    already_linked = await playlists_repo.linked_video_ids(db, playlist_id)
    candidates = [e for e in meta.entries if e.id not in already_linked]
    to_process = candidates[:count]

    result = await _process_entries(
        db, config, playlist_id, playlist.user_id, to_process
    )
    result.total_in_playlist = total
    return result
