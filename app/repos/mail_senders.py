"""Per-profile newsletter sender subscriptions.

Distinct From-addresses discovered by scanning a mailbox. Only
`subscribed=1` senders are ingested by the sync — newsletters are
strictly opt-in, so a freshly discovered sender sits at subscribed=0
until the user ticks it on the "Add a source" page.
"""

from collections.abc import Sequence

import aiosqlite

from app.models import MailSender


def _row_to_sender(row: aiosqlite.Row) -> MailSender:
    return MailSender(
        user_id=row["user_id"],
        sender_addr=row["sender_addr"],
        sender_name=row["sender_name"],
        subscribed=bool(row["subscribed"]),
        last_seen_at=row["last_seen_at"],
        last_subject=row["last_subject"],
    )


async def upsert_discovered(
    db: aiosqlite.Connection,
    user_id: int,
    senders: Sequence[tuple[str, str, str | None, str | None]],
) -> None:
    """Merge discovered senders in.

    `senders` is a list of (addr, name, last_seen_at, last_subject).
    Existing rows keep their `subscribed` flag — discovery only refreshes
    the display metadata; it never silently changes what's subscribed.
    """
    for addr, name, last_seen_at, last_subject in senders:
        addr = addr.strip().lower()
        if not addr:
            continue
        await db.execute(
            """
            INSERT INTO mail_senders (
                user_id, sender_addr, sender_name, subscribed,
                last_seen_at, last_subject
            )
            VALUES (?, ?, ?, 0, ?, ?)
            ON CONFLICT(user_id, sender_addr) DO UPDATE SET
                sender_name=excluded.sender_name,
                last_seen_at=excluded.last_seen_at,
                last_subject=excluded.last_subject
            """,
            (user_id, addr, name or addr, last_seen_at, last_subject),
        )
    await db.commit()


async def list_for_user(
    db: aiosqlite.Connection, user_id: int
) -> list[MailSender]:
    """All known senders for a profile, subscribed first, then most
    recently seen."""
    cursor = await db.execute(
        """
        SELECT * FROM mail_senders WHERE user_id=?
        ORDER BY subscribed DESC, last_seen_at DESC, sender_name COLLATE NOCASE
        """,
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_sender(r) for r in rows]


async def set_subscriptions(
    db: aiosqlite.Connection, user_id: int, subscribed_addrs: list[str]
) -> None:
    """Replace the subscribed set: addresses in the list become
    subscribed, everything else for this profile becomes unsubscribed."""
    wanted = {a.strip().lower() for a in subscribed_addrs if a.strip()}
    cursor = await db.execute(
        "SELECT sender_addr FROM mail_senders WHERE user_id=?", (user_id,)
    )
    known = {row[0] for row in await cursor.fetchall()}
    for addr in known:
        await db.execute(
            "UPDATE mail_senders SET subscribed=? WHERE user_id=? AND sender_addr=?",
            (1 if addr in wanted else 0, user_id, addr),
        )
    await db.commit()


async def subscribed_addrs(
    db: aiosqlite.Connection, user_id: int
) -> set[str]:
    cursor = await db.execute(
        "SELECT sender_addr FROM mail_senders WHERE user_id=? AND subscribed=1",
        (user_id,),
    )
    return {row[0] for row in await cursor.fetchall()}
