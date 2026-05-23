import aiosqlite

# All public functions act as user 1 implicitly. When auth lands, they
# will accept a user_id parameter and the routes will pass the
# authenticated user's id.
_DEFAULT_USER = 1


async def get(db: aiosqlite.Connection, key: str) -> str | None:
    cursor = await db.execute(
        "SELECT value FROM settings WHERE user_id=? AND key=?",
        (_DEFAULT_USER, key),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value
        """,
        (_DEFAULT_USER, key, value),
    )
    await db.commit()


async def get_all(db: aiosqlite.Connection) -> dict[str, str]:
    cursor = await db.execute(
        "SELECT key, value FROM settings WHERE user_id=?", (_DEFAULT_USER,)
    )
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def delete(db: aiosqlite.Connection, key: str) -> None:
    await db.execute(
        "DELETE FROM settings WHERE user_id=? AND key=?",
        (_DEFAULT_USER, key),
    )
    await db.commit()


# ── Per-user variants ────────────────────────────────────────────────
# Most settings are household-global and live on user 1 (see the
# functions above). The IMAP/newsletter config is the exception: it's
# scoped per profile so each profile can poll its own dedicated mailbox.
# These helpers take an explicit user_id; the scheduler iterates every
# profile and reads each one's mailbox config through them.


async def get_for_user(
    db: aiosqlite.Connection, user_id: int, key: str
) -> str | None:
    cursor = await db.execute(
        "SELECT value FROM settings WHERE user_id=? AND key=?",
        (user_id, key),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_for_user(
    db: aiosqlite.Connection, user_id: int, key: str, value: str
) -> None:
    await db.execute(
        """
        INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value
        """,
        (user_id, key, value),
    )
    await db.commit()


async def get_all_for_user(
    db: aiosqlite.Connection, user_id: int
) -> dict[str, str]:
    cursor = await db.execute(
        "SELECT key, value FROM settings WHERE user_id=?", (user_id,)
    )
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def delete_for_user(
    db: aiosqlite.Connection, user_id: int, key: str
) -> None:
    await db.execute(
        "DELETE FROM settings WHERE user_id=? AND key=?",
        (user_id, key),
    )
    await db.commit()
