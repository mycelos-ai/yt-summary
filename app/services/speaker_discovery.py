"""Suggest possible sources for a speaker as CANDIDATES the user confirms.

Kept strictly separate from speaker_backfill: a weak signal here can
never auto-populate the dossier. Every row written is a
speaker_source_candidates row with state='pending'. This module NEVER
writes source_speakers. YouTube is excluded -- show_match handles it as
a confirmed link.
"""
import logging

import aiosqlite

from app.repos import speaker_source_candidates as cand
from app.repos import speakers as speakers_repo

log = logging.getLogger(__name__)

# Signal strengths (also the candidate score). email_from is the only
# reasonably trustworthy signal; the rest are weak and false-positive-prone.
_SCORE = {"email_from": 0.85, "title_match": 0.4, "fulltext": 0.3, "embedding": 0.5}


async def discover_candidates(db: aiosqlite.Connection, speaker_id: int) -> list[int]:
    """Return ids of upserted speaker_source_candidates rows.

    Signals checked:
      - email_from  (0.85): speaker name found in email-kind video description
        (real ingest stores "From {name} <{addr}>" in the description column).
      - title_match (0.40): speaker name in title of web/text video.
      - fulltext    (0.30): speaker name in description of web/text video.

    YouTube is intentionally excluded -- confirmed links come from show_match.
    The embedding signal is deferred; not implemented here.
    """
    speaker = await speakers_repo.get_speaker(db, speaker_id)
    if speaker is None:
        return []

    name_lower = speaker.name.lower()
    if not name_lower:
        return []

    out: list[int] = []

    # email_from: match speaker name against the description column of
    # email-kind videos. Real ingest (mail_sync.py) stores the sender as
    # "From {name} <{addr}>" in description; url is usually empty.
    cur = await db.execute(
        "SELECT id, title, description, url FROM videos "
        "WHERE user_id=? AND kind='email'",
        (speaker.user_id,),
    )
    for r in await cur.fetchall():
        sender_blob = f"{r['description'] or ''} {r['url'] or ''}".lower()
        if name_lower in sender_blob:
            out.append(await cand.upsert_pending(
                db, user_id=speaker.user_id, speaker_id=speaker_id,
                source_id=r["id"], signal="email_from", score=_SCORE["email_from"],
            ))

    # title_match / fulltext: web and text items mentioning the name. Weak.
    # YouTube is deliberately excluded from this query.
    cur = await db.execute(
        "SELECT id, title, description FROM videos "
        "WHERE user_id=? AND kind IN ('web','text')",
        (speaker.user_id,),
    )
    for r in await cur.fetchall():
        title = (r["title"] or "").lower()
        body = (r["description"] or "").lower()
        if name_lower in title:
            out.append(await cand.upsert_pending(
                db, user_id=speaker.user_id, speaker_id=speaker_id,
                source_id=r["id"], signal="title_match", score=_SCORE["title_match"],
            ))
        elif name_lower in body:
            out.append(await cand.upsert_pending(
                db, user_id=speaker.user_id, speaker_id=speaker_id,
                source_id=r["id"], signal="fulltext", score=_SCORE["fulltext"],
            ))

    return out
