import aiosqlite
from app.models import KnownShow


def _row(r) -> KnownShow:
    return KnownShow(
        id=r["id"], user_id=r["user_id"], name=r["name"],
        channel_id=r["channel_id"], title_pattern=r["title_pattern"],
        description_pattern=r["description_pattern"], hosts_json=r["hosts_json"],
        guest_rule=r["guest_rule"], enabled=bool(r["enabled"]),
    )


async def list_enabled(db: aiosqlite.Connection, *, user_id: int = 1) -> list[KnownShow]:
    # Shipped rows (user_id IS NULL) + this profile's own rows.
    # ORDER BY (user_id IS NULL), id  →  user rows first (0 < 1), then seed rows,
    # stable secondary sort by id within each tier.  This makes first-match-wins
    # in show_match.identify_from_metadata deterministic: user overrides beat seeds.
    cur = await db.execute(
        "SELECT * FROM known_shows"
        " WHERE enabled=1 AND (user_id IS NULL OR user_id=?)"
        " ORDER BY (user_id IS NULL), id",
        (user_id,),
    )
    return [_row(r) for r in await cur.fetchall()]
