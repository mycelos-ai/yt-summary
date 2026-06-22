from datetime import datetime

import aiosqlite

from app.models import SpeakerSourceCandidate

_VALID_STATES = {"pending", "confirmed", "dismissed"}


def _row(r: aiosqlite.Row) -> SpeakerSourceCandidate:
    return SpeakerSourceCandidate(
        id=r["id"], user_id=r["user_id"], speaker_id=r["speaker_id"],
        source_id=r["source_id"], signal=r["signal"], score=r["score"],
        state=r["state"], created_at=datetime.fromisoformat(r["created_at"]),
    )


async def upsert_pending(
    db: aiosqlite.Connection, *, user_id: int = 1, speaker_id: int,
    source_id: str, signal: str, score: float | None,
) -> int:
    """Insert a pending candidate, or update its signal/score if one
    already exists for (speaker_id, source_id). NEVER touches
    source_speakers — a candidate is a suggestion only.

    IMPORTANT: the update path only touches signal+score, never state.
    A dismissed candidate stays dismissed even if re-discovered."""
    cur = await db.execute(
        "SELECT id FROM speaker_source_candidates WHERE speaker_id=? AND source_id=?",
        (speaker_id, source_id),
    )
    row = await cur.fetchone()
    if row is not None:
        await db.execute(
            "UPDATE speaker_source_candidates SET signal=?, score=? WHERE id=?",
            (signal, score, row["id"]),
        )
        await db.commit()
        return row["id"]
    cur = await db.execute(
        "INSERT INTO speaker_source_candidates "
        "(user_id, speaker_id, source_id, signal, score, state) "
        "VALUES (?,?,?,?,?, 'pending')",
        (user_id, speaker_id, source_id, signal, score),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def list_for_speaker(
    db: aiosqlite.Connection, speaker_id: int, *, state: str = "pending",
) -> list[SpeakerSourceCandidate]:
    cur = await db.execute(
        "SELECT * FROM speaker_source_candidates WHERE speaker_id=? AND state=? "
        "ORDER BY score DESC NULLS LAST, id ASC",
        (speaker_id, state),
    )
    return [_row(r) for r in await cur.fetchall()]


async def get(
    db: aiosqlite.Connection, candidate_id: int,
) -> SpeakerSourceCandidate | None:
    cur = await db.execute(
        "SELECT * FROM speaker_source_candidates WHERE id=?", (candidate_id,)
    )
    row = await cur.fetchone()
    return _row(row) if row else None


async def set_state(db: aiosqlite.Connection, candidate_id: int, state: str) -> None:
    if state not in _VALID_STATES:
        raise ValueError(f"bad state: {state!r}; must be one of {_VALID_STATES}")
    await db.execute(
        "UPDATE speaker_source_candidates SET state=? WHERE id=?",
        (state, candidate_id),
    )
    await db.commit()
