from datetime import datetime

import aiosqlite

from app.models import User


def _row_to_user(row: aiosqlite.Row) -> User:
    created_at = row["api_key_created_at"]
    return User(
        id=row["id"],
        name=row["name"],
        api_key_hash=row["api_key_hash"],
        api_key_prefix=row["api_key_prefix"],
        api_key_created_at=datetime.fromisoformat(created_at) if created_at else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def get_default_user(db: aiosqlite.Connection) -> User | None:
    cursor = await db.execute("SELECT * FROM users WHERE id = 1")
    row = await cursor.fetchone()
    return _row_to_user(row) if row else None


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
