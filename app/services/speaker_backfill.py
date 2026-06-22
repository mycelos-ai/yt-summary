"""Activation-triggered, library-wide claim backfill for one speaker.

Reads CONFIRMED sources only — the union of existing source_speakers
links and show-match hits over existing YouTube videos (which it first
writes as source_speakers rows). It NEVER reads
speaker_source_candidates: unconfirmed guesses are out of the dossier
by construction (spec rule #3, "attribution beats style").

Runs as a durable speaker_jobs job (decision A) so progress survives a
restart and is inspectable, mirroring the pipeline job posture.
"""
import logging

import aiosqlite

from app.repos import known_shows as known_shows_repo
from app.repos import source_speakers as source_speakers_repo
from app.repos import speaker_jobs as jobs_repo
from app.repos import speakers as speakers_repo
from app.repos import videos as videos_repo
from app.services import show_match
from app.services.speaker_claims import extract_claims_for_source

log = logging.getLogger(__name__)


async def enqueue_backfill(db: aiosqlite.Connection, speaker_id: int) -> int:
    return await jobs_repo.enqueue(db, speaker_id)


def _same_person(a: str, b: str) -> bool:
    from app.repos.speakers import normalize_name_key
    return normalize_name_key(a) == normalize_name_key(b)


async def _confirmed_source_ids(db: aiosqlite.Connection, speaker_id: int) -> list[str]:
    speaker = await speakers_repo.get_speaker(db, speaker_id)
    if speaker is None:
        return []
    # 1) Show-match over existing YouTube videos -> CONFIRM as source_speakers.
    #    Archived videos are excluded so we never write dead-weight links.
    cur = await db.execute(
        "SELECT id FROM videos WHERE user_id=? AND kind='youtube' AND archived_at IS NULL",
        (speaker.user_id,),
    )
    yt_rows = await cur.fetchall()
    # Preload the enabled known-shows ONCE and reuse across every video — the
    # list is identical for all of this user's videos, so re-querying per video
    # would be O(videos) redundant SELECTs on a large library.
    known_shows = await known_shows_repo.list_enabled(db, user_id=speaker.user_id)
    for r in yt_rows:
        video = await videos_repo.get(db, r["id"])
        if video is None:
            continue
        detected = await show_match.identify_from_metadata(
            db, video, known_shows=known_shows)
        if any(_same_person(d.name, speaker.name) for d in detected):
            # Confirmed link (show rule). Idempotent via source_speakers UNIQUE.
            await source_speakers_repo.link_speaker(
                db, video.id, speaker_id,
                detection_source="show_rule",
            )
    # 2) All existing confirmed links (reflects just-written show_rule links too).
    rows = await source_speakers_repo.list_sources_for_speaker(db, speaker_id)
    return list(dict.fromkeys(r["id"] for r in rows))  # de-dupe, preserve order


async def run_backfill(
    db: aiosqlite.Connection,
    speaker_id: int,
    *,
    model: str,
    api_key: str,
    base_url: str | None,
) -> int:
    """Extract claims for every CONFIRMED source. Returns #sources processed."""
    source_ids = await _confirmed_source_ids(db, speaker_id)
    processed = 0
    for sid in source_ids:
        source = await videos_repo.get(db, sid)
        if source is None:
            continue
        try:
            await extract_claims_for_source(
                db, source, [speaker_id],
                model=model, api_key=api_key, base_url=base_url,
            )
        except Exception as e:  # noqa: BLE001 — one bad source must not abort the rest
            log.warning(
                "backfill extract failed for %s: %s: %s",
                sid, type(e).__name__, e,
            )
        processed += 1
    return processed


async def run_pending_backfills(
    db: aiosqlite.Connection,
    *,
    model: str,
    api_key: str,
    base_url: str | None,
    limit: int = 1,
) -> int:
    """Drain up to `limit` pending speaker_jobs (scheduler glue)."""
    done = 0
    for _ in range(limit):
        job = await jobs_repo.claim_next(db)
        if job is None:
            break
        try:
            await jobs_repo.set_step(db, job.id, "extracting claims")
            await run_backfill(
                db, job.speaker_id,
                model=model, api_key=api_key, base_url=base_url,
            )
            await jobs_repo.complete(db, job.id)
        except Exception as e:  # noqa: BLE001
            await jobs_repo.fail(db, job.id, f"{type(e).__name__}: {e}")
        done += 1
    return done
