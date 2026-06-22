"""Speaker activation service. Flips is_active AND enqueues the library-wide
claim backfill (PR 4). Kept as a service so activation logic is testable
without a TestClient and reusable by the route."""
import logging

import aiosqlite

from app.repos import speakers as speakers_repo

log = logging.getLogger(__name__)


async def activate(db: aiosqlite.Connection, speaker_id: int) -> None:
    """Activate a speaker and enqueue its backfill. Best-effort on the
    enqueue — activation must succeed even if the queue write fails."""
    await speakers_repo.set_active(db, speaker_id, True)
    try:
        from app.services import speaker_backfill
        await speaker_backfill.enqueue_backfill(db, speaker_id)
    except Exception:  # noqa: BLE001 — activation must not fail on enqueue trouble
        log.warning(
            "backfill enqueue failed for speaker %s", speaker_id, exc_info=True
        )
