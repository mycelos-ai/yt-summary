from datetime import datetime
from pathlib import Path

import aiosqlite

from app.models import User


def _row_to_user(row: aiosqlite.Row) -> User:
    created_at = row["api_key_created_at"]
    # avatar_emoji / avatar_image / custom_summary_prompt landed in V5;
    # tolerate older row shapes during partial migrations / tests that
    # mock the table.
    try:
        avatar = row["avatar_emoji"] or "👤"
    except (IndexError, KeyError):
        avatar = "👤"
    try:
        avatar_img = row["avatar_image"] or ""
    except (IndexError, KeyError):
        avatar_img = ""
    try:
        custom_prompt = row["custom_summary_prompt"]
    except (IndexError, KeyError):
        custom_prompt = None
    try:
        podcast_token = row["podcast_token"]
    except (IndexError, KeyError):
        podcast_token = None
    return User(
        id=row["id"],
        name=row["name"],
        api_key_hash=row["api_key_hash"],
        api_key_prefix=row["api_key_prefix"],
        api_key_created_at=datetime.fromisoformat(created_at) if created_at else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        avatar_emoji=avatar,
        avatar_image=avatar_img,
        custom_summary_prompt=custom_prompt,
        podcast_token=podcast_token,
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
    avatar_image: str = "",
    custom_summary_prompt: str | None = None,
) -> User:
    name = name.strip()
    if not name:
        raise ValueError("Profile name must be non-empty")
    avatar_emoji = (avatar_emoji or "👤").strip() or "👤"
    avatar_image = (avatar_image or "").strip()
    cursor = await db.execute(
        """
        INSERT INTO users (
            name, avatar_emoji, avatar_image, custom_summary_prompt
        )
        VALUES (?, ?, ?, ?)
        """,
        (name, avatar_emoji, avatar_image, custom_summary_prompt),
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
    avatar_image: str | None = None,
    custom_summary_prompt: str | None = None,
    custom_summary_prompt_set: bool = False,
) -> None:
    """Update profile fields. Only provided fields are touched.

    `custom_summary_prompt_set=True` forces the prompt column to be
    written (including to NULL) — needed for the "reset to default"
    path which writes NULL explicitly. Without it, passing
    custom_summary_prompt=None means "leave it alone."

    For `avatar_image`, the empty string IS a meaningful value
    (clear the image, fall back to emoji). None means "don't touch."
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
    if avatar_image is not None:
        sets.append("avatar_image = ?")
        args.append(avatar_image.strip())
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


async def delete(
    db: aiosqlite.Connection,
    user_id: int,
    *,
    data_dir: Path | None = None,
) -> None:
    """Delete a profile and all data scoped to it.

    Wipes videos / playlists / chat / playlist_videos / video_tags
    rows owned by the profile. We do this in code rather than via
    cascading FKs because the existing schema doesn't declare ON
    DELETE CASCADE on user_id (it was added as a defaulted column
    later) and bolting it on retroactively would require rebuilding
    every table.

    When `data_dir` is provided, also unlink TTS audio files for the
    deleted videos. The FK cascade on `tts_jobs.video_id` removes the
    DB rows when the parent `videos` row is deleted, but the MP3s on
    disk would otherwise become orphaned. Pre-fetch the audio paths
    before the DELETE so we know what to clean up after it commits.
    Pass `data_dir=None` to skip the on-disk cleanup (preserves
    behaviour for tests / callers that don't care).
    """
    # Collect the video ids owned by this profile so we can clean up
    # the satellite tables (playlist_videos, video_tags, chat_messages,
    # jobs) by video_id — those tables don't carry user_id directly.
    cursor = await db.execute(
        "SELECT id FROM videos WHERE user_id = ?", (user_id,)
    )
    video_ids = [row[0] for row in await cursor.fetchall()]

    # Pre-fetch TTS audio paths so we can unlink them after the
    # cascade commits (FK ON DELETE CASCADE on tts_jobs.video_id will
    # wipe the DB rows when we DELETE from videos below).
    tts_audio_paths: list[str] = []
    if data_dir is not None and video_ids:
        placeholders = ",".join("?" * len(video_ids))
        cursor = await db.execute(
            f"SELECT audio_path FROM tts_jobs "
            f"WHERE video_id IN ({placeholders}) "
            f"AND audio_path IS NOT NULL",
            tuple(video_ids),
        )
        tts_audio_paths = [row[0] for row in await cursor.fetchall()]

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

    if data_dir is not None:
        for rel in tts_audio_paths:
            (data_dir / rel).unlink(missing_ok=True)
        # Tidy up empty per-video directories under tts-audio/.
        for vid in video_ids:
            video_dir = data_dir / "tts-audio" / vid
            if video_dir.exists() and not any(video_dir.iterdir()):
                video_dir.rmdir()


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


async def set_podcast_token(db: aiosqlite.Connection, user_id: int) -> str:
    """Generate (or regenerate) the profile's podcast-feed token and
    return the plaintext. Regenerating invalidates any old feed URL.
    Stored in plaintext deliberately — see the column comment in db.py."""
    import secrets
    token = secrets.token_urlsafe(24)  # ~32 urlsafe chars
    await db.execute(
        "UPDATE users SET podcast_token = ? WHERE id = ?", (token, user_id),
    )
    await db.commit()
    return token


async def clear_podcast_token(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "UPDATE users SET podcast_token = NULL WHERE id = ?", (user_id,),
    )
    await db.commit()


async def get_by_podcast_token(
    db: aiosqlite.Connection, token: str
) -> User | None:
    """Resolve a profile by its podcast token. None for unknown/empty
    tokens (the feed routes 404 on a miss — no information leak)."""
    if not token:
        return None
    cursor = await db.execute(
        "SELECT * FROM users WHERE podcast_token = ?", (token,)
    )
    row = await cursor.fetchone()
    return _row_to_user(row) if row else None


async def find_by_api_key_hash(
    db: aiosqlite.Connection, key_hash: str
) -> User | None:
    cursor = await db.execute(
        "SELECT * FROM users WHERE api_key_hash = ?", (key_hash,)
    )
    row = await cursor.fetchone()
    return _row_to_user(row) if row else None


async def get_interest_profile(
    db: aiosqlite.Connection, *, user_id: int,
) -> tuple[str | None, int]:
    """Return (markdown, version). Missing row → (None, 0)."""
    cur = await db.execute(
        "SELECT interest_profile_md, interest_profile_version "
        "FROM users WHERE id=?",
        (user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return (None, 0)
    # interest_profile_version is NOT NULL DEFAULT 0 (see app/db.py SCHEMA);
    # trust the schema rather than defending the read path.
    return (row[0], row[1])


async def set_interest_profile(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    markdown: str,
    expected_version: int,
) -> bool:
    """Optimistic lock: writes only if the current version matches
    `expected_version`. Returns True on success, False on conflict.
    Successful writes increment the version by 1.
    """
    cur = await db.execute(
        """
        UPDATE users
        SET interest_profile_md = ?,
            interest_profile_version = interest_profile_version + 1
        WHERE id = ?
          AND interest_profile_version = ?
        """,
        (markdown, user_id, expected_version),
    )
    await db.commit()
    return cur.rowcount > 0


async def get_digest_prefs(
    db: aiosqlite.Connection, *, user_id: int,
) -> tuple[bool, int]:
    cur = await db.execute(
        "SELECT digest_enabled, digest_hour_local FROM users WHERE id=?",
        (user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return (False, 7)
    # Both columns are NOT NULL DEFAULT in SCHEMA (db.py); trust the
    # schema rather than coalescing here.
    return (bool(row[0]), int(row[1]))


async def set_digest_prefs(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    digest_enabled: bool,
    digest_hour_local: int,
) -> None:
    if not 0 <= digest_hour_local <= 23:
        raise ValueError("digest_hour_local must be 0..23")
    await db.execute(
        "UPDATE users SET digest_enabled=?, digest_hour_local=? WHERE id=?",
        (1 if digest_enabled else 0, digest_hour_local, user_id),
    )
    await db.commit()
