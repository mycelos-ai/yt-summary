import asyncio
import contextlib
import logging

import aiosqlite

from app.config import Config
from app.repos import llm_models as llm_models_repo
from app.services import speaker_backfill

log = logging.getLogger(__name__)


class SpeakerBackfillWorker:
    """Drains the speaker_jobs backfill queue. Dedicated background task
    (like app/worker.py), NOT a scheduler tick — speaker_jobs is a durable
    claim_next queue. Lower urgency than video jobs, so it polls slower.
    Best-effort + crash-resistant: a failure logs and the loop survives."""

    def __init__(self, db: aiosqlite.Connection, config: Config, poll_interval: float = 5.0):
        self._db = db
        self._config = config
        self._poll_interval = poll_interval
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._run_iteration()
            except asyncio.CancelledError:
                raise
            except BaseException:
                log.exception("speaker_backfill_worker: unexpected crash, restarting in 5s")
                with contextlib.suppress(Exception):
                    await self._db.rollback()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), 5.0)

    async def _run_iteration(self) -> None:
        # Resolve the default model's creds for extraction. No model → nothing to do.
        model_row = await llm_models_repo.get_default(self._db)
        if model_row is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
            return
        n = await speaker_backfill.run_pending_backfills(
            self._db,
            model=model_row.model,
            api_key=model_row.api_key or "",
            base_url=model_row.base_url or None,
            limit=1,
        )
        # If nothing was drained, sleep before polling again (avoid a busy loop).
        if n == 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
