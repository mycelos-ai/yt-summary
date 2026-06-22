import re
from datetime import datetime

import aiosqlite

from app.models import Speaker

_DEFAULT_USER = 1
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_name_key(name: str) -> str:
    s = _PUNCT.sub(" ", name.lower())
    return _WS.sub(" ", s).strip()


def _row_to_speaker(row: aiosqlite.Row) -> Speaker:
    return Speaker(
        id=row["id"], user_id=row["user_id"],
        known_speaker_id=row["known_speaker_id"],
        name=row["name"], name_key=row["name_key"], role=row["role"],
        avatar_id=row["avatar_id"], avatar_photo_path=row["avatar_photo_path"],
        style_note=row["style_note"], is_active=bool(row["is_active"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


async def resolve_speaker(
    db: aiosqlite.Connection, *, user_id: int = _DEFAULT_USER,
    name: str, role: str | None = None,
) -> int:
    key = normalize_name_key(name)
    cur = await db.execute(
        "SELECT id FROM speakers WHERE user_id=? AND name_key=?", (user_id, key)
    )
    row = await cur.fetchone()
    if row is not None:
        return row["id"]

    # Look up the known_speakers catalog by name_key to inherit curated identity.
    ks_cur = await db.execute(
        "SELECT id, role, avatar_id, style_note FROM known_speakers WHERE name_key=?",
        (key,),
    )
    ks = await ks_cur.fetchone()

    if ks is not None:
        # Caller-provided role wins; fall back to the seeded role when caller
        # passed no role (None means "I don't know", not "override to null").
        effective_role = role if role is not None else ks["role"]
        cur = await db.execute(
            "INSERT INTO speakers "
            "(user_id, name, name_key, role, known_speaker_id, avatar_id, style_note) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, name, key, effective_role, ks["id"], ks["avatar_id"], ks["style_note"]),
        )
    else:
        cur = await db.execute(
            "INSERT INTO speakers (user_id, name, name_key, role) VALUES (?,?,?,?)",
            (user_id, name, key, role),
        )

    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def get_speaker(db: aiosqlite.Connection, speaker_id: int) -> Speaker | None:
    cur = await db.execute("SELECT * FROM speakers WHERE id=?", (speaker_id,))
    row = await cur.fetchone()
    return _row_to_speaker(row) if row else None


async def list_for_user(
    db: aiosqlite.Connection, *, user_id: int = _DEFAULT_USER,
    active_only: bool = False,
) -> list[Speaker]:
    q = "SELECT * FROM speakers WHERE user_id=?"
    if active_only:
        q += " AND is_active=1"
    q += " ORDER BY name COLLATE NOCASE"
    cur = await db.execute(q, (user_id,))
    return [_row_to_speaker(r) for r in await cur.fetchall()]


async def set_active(db: aiosqlite.Connection, speaker_id: int, active: bool) -> None:
    await db.execute(
        "UPDATE speakers SET is_active=?, updated_at=datetime('now') WHERE id=?",
        (1 if active else 0, speaker_id),
    )
    await db.commit()


async def update_fields(
    db: aiosqlite.Connection, speaker_id: int, *,
    name: str, role: str | None, avatar_id: str | None, style_note: str | None,
) -> None:
    """Edit the user-facing identity fields. name_key is re-derived from
    name so a rename stays the identity anchor."""
    await db.execute(
        "UPDATE speakers SET name=?, name_key=?, role=?, avatar_id=?, "
        "style_note=?, updated_at=datetime('now') WHERE id=?",
        (name, normalize_name_key(name), role, avatar_id, style_note, speaker_id),
    )
    await db.commit()


async def set_photo_path(
    db: aiosqlite.Connection, speaker_id: int, path: str | None,
) -> None:
    await db.execute(
        "UPDATE speakers SET avatar_photo_path=?, updated_at=datetime('now') "
        "WHERE id=?",
        (path, speaker_id),
    )
    await db.commit()
