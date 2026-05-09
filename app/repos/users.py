from datetime import datetime

import aiosqlite

from app.models import User


def _row_to_user(row: aiosqlite.Row) -> User:
    created_at = row["api_key_created_at"]
    # avatar_emoji / custom_summary_prompt landed in V5; tolerate older
    # row shapes during partial migrations / tests that mock the table.
    try:
        avatar = row["avatar_emoji"] or "👤"
    except (IndexError, KeyError):
        avatar = "👤"
    try:
        custom_prompt = row["custom_summary_prompt"]
    except (IndexError, KeyError):
        custom_prompt = None
    return User(
        id=row["id"],
        name=row["name"],
        api_key_hash=row["api_key_hash"],
        api_key_prefix=row["api_key_prefix"],
        api_key_created_at=datetime.fromisoformat(created_at) if created_at else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        avatar_emoji=avatar,
        custom_summary_prompt=custom_prompt,
    )


async def get_default_user(db: aiosqlite.Connection) -> User | None:
    """Return the seeded user (id=1).

    Deprecated for new code paths — multi-profile callers should use
    `get_by_id` with the cookie-resolved current user id. Kept because
    the API/MCP auth layer still ties keys to a single canonical user
    and the boot-warning logger reaches for it.
    """
    cursor = await db.execute("SELECT * FROM users WHERE id = 1")
    row = await cursor.fetchone()
    return _row_to_user(row) if row else None


async def get_by_id(
    db: aiosqlite.Connection, user_id: int
) -> User | None:
    cursor = await db.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    return _row_to_user(row) if row else None


async def list_all(db: aiosqlite.Connection) -> list[User]:
    cursor = await db.execute("SELECT * FROM users ORDER BY id ASC")
    rows = await cursor.fetchall()
    return [_row_to_user(r) for r in rows]


async def create(
    db: aiosqlite.Connection,
    *,
    name: str,
    avatar_emoji: str = "👤",
    custom_summary_prompt: str | None = None,
) -> User:
    name = name.strip()
    if not name:
        raise ValueError("Profile name must be non-empty")
    avatar_emoji = (avatar_emoji or "👤").strip() or "👤"
    cursor = await db.execute(
        """
        INSERT INTO users (name, avatar_emoji, custom_summary_prompt)
        VALUES (?, ?, ?)
        """,
        (name, avatar_emoji, custom_summary_prompt),
    )
    await db.commit()
    new_id = cursor.lastrowid
    assert new_id is not None
    user = await get_by_id(db, new_id)
    assert user is not None
    return user


async def update(
    db: aiosqlite.Connection,
    user_id: int,
    *,
    name: str | None = None,
    avatar_emoji: str | None = None,
    custom_summary_prompt: str | None = None,
    custom_summary_prompt_set: bool = False,
) -> None:
    """Update profile fields. Only provided fields are touched.

    `custom_summary_prompt_set=True` forces the prompt column to be
    written (including to NULL) — needed for the "reset to default"
    path which writes NULL explicitly. Without it, passing
    custom_summary_prompt=None means "leave it alone."
    """
    sets: list[str] = []
    args: list[object] = []
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ValueError("Profile name must be non-empty")
        sets.append("name = ?")
        args.append(clean)
    if avatar_emoji is not None:
        clean_emoji = avatar_emoji.strip() or "👤"
        sets.append("avatar_emoji = ?")
        args.append(clean_emoji)
    if custom_summary_prompt_set:
        sets.append("custom_summary_prompt = ?")
        args.append(custom_summary_prompt)
    if not sets:
        return
    args.append(user_id)
    await db.execute(
        f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(args)
    )
    await db.commit()


async def delete(db: aiosqlite.Connection, user_id: int) -> None:
    """Delete a profile and all data scoped to it.

    Wipes videos / playlists / chat / playlist_videos / video_tags
    rows owned by the profile. We do this in code rather than via
    cascading FKs because the existing schema doesn't declare ON
    DELETE CASCADE on user_id (it was added as a defaulted column
    later) and bolting it on retroactively would require rebuilding
    every table.
    """
    # Collect the video ids owned by this profile so we can clean up
    # the satellite tables (playlist_videos, video_tags, chat_messages,
    # jobs) by video_id — those tables don't carry user_id directly.
    cursor = await db.execute(
        "SELECT id FROM videos WHERE user_id = ?", (user_id,)
    )
    video_ids = [row[0] for row in await cursor.fetchall()]

    if video_ids:
        placeholders = ",".join("?" * len(video_ids))
        await db.execute(
            f"DELETE FROM video_tags WHERE video_id IN ({placeholders})",
            tuple(video_ids),
        )
        await db.execute(
            f"DELETE FROM playlist_videos WHERE video_id IN ({placeholders})",
            tuple(video_ids),
        )
        await db.execute(
            f"DELETE FROM chat_messages WHERE video_id IN ({placeholders})",
            tuple(video_ids),
        )
        await db.execute(
            f"DELETE FROM jobs WHERE video_id IN ({placeholders})",
            tuple(video_ids),
        )
        await db.execute(
            f"DELETE FROM video_embeddings WHERE video_id IN ({placeholders})",
            tuple(video_ids),
        )
        await db.execute(
            f"DELETE FROM videos WHERE id IN ({placeholders})",
            tuple(video_ids),
        )

    # Chat messages may also be scoped via user_id directly (for safety
    # when a video was reassigned).
    await db.execute(
        "DELETE FROM chat_messages WHERE user_id = ?", (user_id,)
    )
    await db.execute(
        "DELETE FROM playlists WHERE user_id = ?", (user_id,)
    )
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()


async def set_api_key(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    key_hash: str,
    key_prefix: str,
) -> None:
    await db.execute(
        """
        UPDATE users SET
            api_key_hash = ?,
            api_key_prefix = ?,
            api_key_created_at = datetime('now')
        WHERE id = ?
        """,
        (key_hash, key_prefix, user_id),
    )
    await db.commit()


async def clear_api_key(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        """
        UPDATE users SET
            api_key_hash = NULL,
            api_key_prefix = NULL,
            api_key_created_at = NULL
        WHERE id = ?
        """,
        (user_id,),
    )
    await db.commit()


async def find_by_api_key_hash(
    db: aiosqlite.Connection, key_hash: str
) -> User | None:
    cursor = await db.execute(
        "SELECT * FROM users WHERE api_key_hash = ?", (key_hash,)
    )
    row = await cursor.fetchone()
    return _row_to_user(row) if row else None
