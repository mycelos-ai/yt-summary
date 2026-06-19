from datetime import datetime

import aiosqlite

from app.models import Playlist, Video
from app.repos.videos import _row_to_video  # noqa: PLC2701


def _row_to_playlist(row: aiosqlite.Row) -> Playlist:
    last = row["last_refreshed_at"]
    return Playlist(
        id=row["id"],
        user_id=row["user_id"],
        url=row["url"],
        title=row["title"],
        description=row["description"],
        thumbnail_path=row["thumbnail_path"],
        last_refreshed_at=datetime.fromisoformat(last) if last else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def create(
    db: aiosqlite.Connection,
    *,
    playlist_id: str,
    user_id: int,
    url: str,
    title: str,
    description: str,
    thumbnail_path: str | None,
) -> None:
    await db.execute(
        """
        INSERT INTO playlists (id, user_id, url, title, description, thumbnail_path)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            url=excluded.url,
            title=excluded.title,
            description=excluded.description,
            thumbnail_path=COALESCE(excluded.thumbnail_path, playlists.thumbnail_path)
        """,
        (playlist_id, user_id, url, title, description, thumbnail_path),
    )
    await db.commit()


async def get(db: aiosqlite.Connection, playlist_id: str) -> Playlist | None:
    cursor = await db.execute(
        "SELECT * FROM playlists WHERE id=?", (playlist_id,)
    )
    row = await cursor.fetchone()
    return _row_to_playlist(row) if row else None


async def list_for_user(
    db: aiosqlite.Connection,
    user_id: int,
    *,
    limit: int | None = None,
) -> list[Playlist]:
    """Return playlists for ``user_id`` ordered most-recently-active first.

    "Most recently active" means ``last_refreshed_at`` if present, else
    ``created_at``. ``limit`` caps the row count when set; pass it as
    ``N + 1`` to distinguish "exactly N" from "more than N" without a
    separate count query.
    """
    if limit is not None:
        cursor = await db.execute(
            "SELECT * FROM playlists WHERE user_id=? "
            "ORDER BY COALESCE(last_refreshed_at, created_at) DESC, id DESC "
            "LIMIT ?",
            (user_id, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM playlists WHERE user_id=? "
            "ORDER BY COALESCE(last_refreshed_at, created_at) DESC, id DESC",
            (user_id,),
        )
    rows = await cursor.fetchall()
    return [_row_to_playlist(r) for r in rows]


async def list_with_stats(
    db: aiosqlite.Connection, user_id: int
) -> list[tuple[Playlist, int]]:
    """Return ``(playlist, video_count)`` pairs for ``user_id``.

    Ordered most-recently-active first (``last_refreshed_at`` then
    ``created_at``). Single LEFT JOIN + GROUP BY query so we don't
    N+1 on the playlist list page.
    """
    cursor = await db.execute(
        """
        SELECT p.*, COUNT(v.id) AS video_count
        FROM playlists p
        LEFT JOIN playlist_videos pv ON pv.playlist_id = p.id
        LEFT JOIN videos v ON v.id = pv.video_id AND v.archived_at IS NULL
        WHERE p.user_id = ?
        GROUP BY p.id
        ORDER BY COALESCE(p.last_refreshed_at, p.created_at) DESC, p.id DESC
        """,
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [(_row_to_playlist(r), r["video_count"]) for r in rows]


async def delete(db: aiosqlite.Connection, playlist_id: str) -> None:
    await db.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    await db.commit()


async def set_last_refreshed(db: aiosqlite.Connection, playlist_id: str) -> None:
    await db.execute(
        "UPDATE playlists SET last_refreshed_at=datetime('now') WHERE id=?",
        (playlist_id,),
    )
    await db.commit()


async def link_video(
    db: aiosqlite.Connection,
    playlist_id: str,
    video_id: str,
    position: int | None = None,
) -> bool:
    """Link a video to a playlist, storing its playlist position.

    Returns True if the link was newly created, False if it already
    existed. In BOTH cases `position` is written (insert or update), so a
    refresh re-numbers existing links. Newness is determined by an
    explicit existence check BEFORE the upsert, because ON CONFLICT DO
    UPDATE makes rowcount positive on updates too.
    """
    cur = await db.execute(
        "SELECT 1 FROM playlist_videos WHERE playlist_id=? AND video_id=?",
        (playlist_id, video_id),
    )
    is_new = await cur.fetchone() is None
    await db.execute(
        """
        INSERT INTO playlist_videos (playlist_id, video_id, position)
        VALUES (?, ?, ?)
        ON CONFLICT(playlist_id, video_id)
        DO UPDATE SET position = excluded.position
        """,
        (playlist_id, video_id, position),
    )
    await db.commit()
    return is_new


async def linked_video_ids(
    db: aiosqlite.Connection, playlist_id: str
) -> set[str]:
    cursor = await db.execute(
        "SELECT video_id FROM playlist_videos WHERE playlist_id=?",
        (playlist_id,),
    )
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def videos_for_playlist(
    db: aiosqlite.Connection, playlist_id: str
) -> list[Video]:
    cursor = await db.execute(
        """
        SELECT v.* FROM videos v
        JOIN playlist_videos pv ON v.id = pv.video_id
        WHERE pv.playlist_id = ? AND v.archived_at IS NULL
        ORDER BY pv.added_at DESC, pv.video_id DESC
        """,
        (playlist_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_video(r) for r in rows]


async def playlists_for_videos(
    db: aiosqlite.Connection, video_ids: list[str]
) -> dict[str, list[tuple[str, str]]]:
    """For a batch of video ids, return the playlists each is linked to.

    Returns: {video_id: [(playlist_id, playlist_title), ...]}.
    Videos without playlist links are absent from the result dict.
    Single query, scales for the home page video grid.
    """
    if not video_ids:
        return {}
    placeholders = ",".join("?" * len(video_ids))
    cursor = await db.execute(
        f"""
        SELECT pv.video_id, p.id, p.title
        FROM playlist_videos pv
        JOIN playlists p ON p.id = pv.playlist_id
        WHERE pv.video_id IN ({placeholders})
        ORDER BY p.title
        """,
        tuple(video_ids),
    )
    rows = await cursor.fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    for video_id, playlist_id, title in rows:
        out.setdefault(video_id, []).append((playlist_id, title))
    return out


async def latest_video_ids(
    db: aiosqlite.Connection, playlist_ids: list[str]
) -> dict[str, str]:
    """Map each playlist id to the id of its most-recently-added video.

    "Newest" = greatest ``playlist_videos.added_at`` (tie-break
    ``video_id DESC``, matching ``videos_for_playlist``). Playlists with no
    linked videos are absent from the result. Single window-function query,
    no N+1.
    """
    if not playlist_ids:
        return {}
    placeholders = ",".join("?" * len(playlist_ids))
    cursor = await db.execute(
        f"""
        SELECT playlist_id, video_id FROM (
            SELECT
                pv.playlist_id,
                pv.video_id,
                ROW_NUMBER() OVER (
                    PARTITION BY pv.playlist_id
                    ORDER BY pv.added_at DESC, pv.video_id DESC
                ) AS rn
            FROM playlist_videos pv
            JOIN videos v ON v.id = pv.video_id AND v.archived_at IS NULL
            WHERE pv.playlist_id IN ({placeholders})
        )
        WHERE rn = 1
        """,
        tuple(playlist_ids),
    )
    rows = await cursor.fetchall()
    return {row["playlist_id"]: row["video_id"] for row in rows}
