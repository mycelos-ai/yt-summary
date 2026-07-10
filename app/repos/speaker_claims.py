from datetime import datetime
from typing import Literal, overload

import aiosqlite

from app.models import SpeakerClaim

_DEFAULT_USER = 1
# Columns the user may correct from the review UI. Anything else is ignored.
_EDITABLE = {"claim", "topic", "evidence_text", "confidence"}


def _row(r: aiosqlite.Row) -> SpeakerClaim:
    return SpeakerClaim(
        id=r["id"], user_id=r["user_id"], speaker_id=r["speaker_id"],
        source_id=r["source_id"], source_speaker_id=r["source_speaker_id"],
        claim=r["claim"], topic=r["topic"], evidence_text=r["evidence_text"],
        evidence_start_s=r["evidence_start_s"], evidence_end_s=r["evidence_end_s"],
        text_start_offset=r["text_start_offset"], text_end_offset=r["text_end_offset"],
        confidence=r["confidence"], extraction_method=r["extraction_method"],
        attribution_method=r["attribution_method"],
        attribution_confidence=r["attribution_confidence"],
        attribution_reason=r["attribution_reason"],
        review_status=r["review_status"],
        created_at=datetime.fromisoformat(r["created_at"]),
    )


async def insert_claim(
    db: aiosqlite.Connection, *, user_id: int = _DEFAULT_USER,
    speaker_id: int, source_id: str, source_speaker_id: int | None = None,
    claim: str, topic: str | None = None, evidence_text: str | None = None,
    evidence_start_s: int | None = None, evidence_end_s: int | None = None,
    text_start_offset: int | None = None, text_end_offset: int | None = None,
    confidence: float | None = None, extraction_method: str = "llm",
    attribution_method: str | None = None, attribution_confidence: float | None = None,
    attribution_reason: str | None = None,
    commit: bool = True,
) -> int:
    cur = await db.execute(
        "INSERT INTO speaker_claims ("
        "user_id, speaker_id, source_id, source_speaker_id, claim, topic, "
        "evidence_text, evidence_start_s, evidence_end_s, text_start_offset, "
        "text_end_offset, confidence, extraction_method, attribution_method, "
        "attribution_confidence, attribution_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, speaker_id, source_id, source_speaker_id, claim, topic,
         evidence_text, evidence_start_s, evidence_end_s, text_start_offset,
         text_end_offset, confidence, extraction_method, attribution_method,
         attribution_confidence, attribution_reason),
    )
    assert cur.lastrowid is not None
    if commit:
        await db.commit()
    return cur.lastrowid


async def get(db: aiosqlite.Connection, claim_id: int) -> "SpeakerClaim | None":
    cur = await db.execute("SELECT * FROM speaker_claims WHERE id=?", (claim_id,))
    row = await cur.fetchone()
    return _row(row) if row is not None else None


@overload
async def list_for_speaker(
    db: aiosqlite.Connection, speaker_id: int, *, grouped_by_topic: Literal[False] = False,
) -> list[SpeakerClaim]: ...


@overload
async def list_for_speaker(
    db: aiosqlite.Connection, speaker_id: int, *, grouped_by_topic: Literal[True],
) -> dict[str, list[SpeakerClaim]]: ...


async def list_for_speaker(
    db: aiosqlite.Connection, speaker_id: int, *, grouped_by_topic: bool = False,
) -> list[SpeakerClaim] | dict[str, list[SpeakerClaim]]:
    cur = await db.execute(
        "SELECT * FROM speaker_claims WHERE speaker_id=? "
        "ORDER BY topic IS NULL, topic COLLATE NOCASE, created_at DESC, id DESC",
        (speaker_id,),
    )
    rows = [_row(r) for r in await cur.fetchall()]
    if not grouped_by_topic:
        return rows
    grouped: dict[str, list[SpeakerClaim]] = {}
    for c in rows:
        grouped.setdefault(c.topic or "Other", []).append(c)
    return grouped


async def list_for_source_speakers(
    db: aiosqlite.Connection, source_id: str, speaker_ids: list[int],
) -> list[SpeakerClaim]:
    if not speaker_ids:
        return []
    marks = ",".join("?" for _ in speaker_ids)
    cur = await db.execute(
        f"SELECT * FROM speaker_claims WHERE source_id=? AND speaker_id IN ({marks}) "
        "ORDER BY created_at DESC, id DESC",
        (source_id, *speaker_ids),
    )
    return [_row(r) for r in await cur.fetchall()]


async def set_review_status(db: aiosqlite.Connection, claim_id: int, status: str) -> None:
    if status not in ("unreviewed", "accepted", "rejected"):
        raise ValueError(f"bad review_status: {status}")
    await db.execute(
        "UPDATE speaker_claims SET review_status=? WHERE id=?", (status, claim_id)
    )
    await db.commit()


async def edit_claim(db: aiosqlite.Connection, claim_id: int, **fields) -> None:
    cols = {k: v for k, v in fields.items() if k in _EDITABLE}
    if not cols:
        return
    sets = ", ".join(f"{k}=?" for k in cols)
    await db.execute(
        f"UPDATE speaker_claims SET {sets} WHERE id=?",
        (*cols.values(), claim_id),
    )
    await db.commit()


async def replace_for_source_speakers(
    db: aiosqlite.Connection, source_id: str, speaker_ids: list[int],
    commit: bool = True,
) -> None:
    """Delete THIS source's claims for the given speakers (forward-only
    re-derivation: the extractor re-inserts immediately after). No-op on
    an empty speaker list.

    When commit=False the DELETE is executed but NOT committed, allowing the
    caller to batch the delete + re-inserts into a single atomic transaction.
    """
    if not speaker_ids:
        return
    marks = ",".join("?" for _ in speaker_ids)
    await db.execute(
        f"DELETE FROM speaker_claims WHERE source_id=? AND speaker_id IN ({marks})",
        (source_id, *speaker_ids),
    )
    if commit:
        await db.commit()
