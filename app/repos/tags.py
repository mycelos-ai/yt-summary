"""Tags repo.

Tags come from yt-dlp's per-video `tags` field (uploader-set keywords).
We store them as a normalized two-table schema:
- `tags(id, name UNIQUE COLLATE NOCASE)` — one row per distinct tag
- `video_tags(video_id, tag_id)` — many-to-many linking

`COLLATE NOCASE` means "Python" and "python" are the same tag.
First-writer wins on capitalization.
"""

import aiosqlite


async def upsert_tag(db: aiosqlite.Connection, name: str) -> int:
    """Insert tag if missing, return its id either way."""
    name = name.strip()
    if not name:
        raise ValueError("Tag name must be non-empty")
    await db.execute(
        "INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,)
    )
    cursor = await db.execute(
        "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def set_tags_for_video(
    db: aiosqlite.Connection, video_id: str, tag_names: list[str]
) -> None:
    """Replace the video's tag set with `tag_names`. Idempotent.

    Empty list → all links removed. Whitespace-only / duplicate names
    in the input are filtered out.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in tag_names:
        name = raw.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)

    await db.execute("DELETE FROM video_tags WHERE video_id=?", (video_id,))
    for name in cleaned:
        tag_id = await upsert_tag(db, name)
        await db.execute(
            "INSERT OR IGNORE INTO video_tags (video_id, tag_id) VALUES (?, ?)",
            (video_id, tag_id),
        )
    await db.commit()


async def tags_for_videos(
    db: aiosqlite.Connection, video_ids: list[str]
) -> dict[str, list[str]]:
    """For a batch of video ids, return each video's tag names.

    Returns: {video_id: [tag_name, ...]}, sorted by tag name.
    Videos without tags are absent from the result dict.
    """
    if not video_ids:
        return {}
    placeholders = ",".join("?" * len(video_ids))
    cursor = await db.execute(
        f"""
        SELECT vt.video_id, t.name
        FROM video_tags vt
        JOIN tags t ON t.id = vt.tag_id
        WHERE vt.video_id IN ({placeholders})
        ORDER BY t.name COLLATE NOCASE
        """,
        tuple(video_ids),
    )
    rows = await cursor.fetchall()
    out: dict[str, list[str]] = {}
    for video_id, name in rows:
        out.setdefault(video_id, []).append(name)
    return out


async def tags_for_video(
    db: aiosqlite.Connection, video_id: str
) -> list[str]:
    """Return one video's tag names, sorted."""
    result = await tags_for_videos(db, [video_id])
    return result.get(video_id, [])
