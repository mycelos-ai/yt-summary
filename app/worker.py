import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import aiosqlite

from app.config import Config
from app.repos import jobs as jobs_repo

if TYPE_CHECKING:
    from app.services.heartbeat import HeartbeatRegistry

log = logging.getLogger(__name__)

ProcessVideo = Callable[
    [aiosqlite.Connection, Config, str, Callable[[str], Awaitable[None]]],
    Awaitable[None],
]


class Worker:
    def __init__(
        self,
        db: aiosqlite.Connection,
        config: Config,
        process_video: ProcessVideo,
        poll_interval: float = 1.0,
        heartbeat: "HeartbeatRegistry | None" = None,
    ):
        self._db = db
        self._config = config
        self._process_video = process_video
        self._poll_interval = poll_interval
        self._heartbeat = heartbeat
        self._stopped = asyncio.Event()

    @property
    def poll_interval_seconds(self) -> float:
        """Public read-only accessor — the diagnostics page uses this
        to compute the alive/stale threshold (3 × poll_interval)."""
        return self._poll_interval

    def stop(self) -> None:
        self._stopped.set()

    def _touch(
        self, *, current_job_id: int | None = None, current_step: str | None = None,
    ) -> None:
        if self._heartbeat is not None:
            self._heartbeat.touch(
                "summary_worker",
                current_job_id=current_job_id,
                current_step=current_step,
            )

    async def run(self) -> None:
        """Top-level loop. Crash-resistant: any error from a single
        iteration (including BaseException subclasses other than
        CancelledError, and OperationalError from claim_next) is logged
        and survived. The worker only ever exits when stop() is called
        or the task is cancelled by the lifespan on shutdown."""
        while not self._stopped.is_set():
            try:
                await self._run_iteration()
            except asyncio.CancelledError:
                # Shutdown signal — propagate so the lifespan's
                # `await worker_task` returns cleanly.
                raise
            except BaseException:
                # Anything else: log with traceback, stamp the heartbeat
                # so the diagnostics page surfaces the crash, then sleep
                # 5s (interruptible by stop()) before resuming.
                log.exception(
                    "summary_worker: unexpected crash, restarting in 5s"
                )
                self._touch(current_step="restarting after crash")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), 5.0)

    async def _run_iteration(self) -> None:
        """One pass of the loop: claim or sleep, then handle the job.

        All pre-existing inner exception handling is preserved — pipeline
        failures still mark the job `failed` via the inner try/except.
        The outer crash-survival wrapper in `run()` only catches things
        that escape this method entirely.
        """
        job = await jobs_repo.claim_next(self._db)
        if job is None:
            # Heartbeat the idle loop so 'is the worker alive?' can
            # be answered even when there's nothing to do.
            self._touch()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
            return
        try:
            job_id_capture = job.id
            self._touch(current_job_id=job.id, current_step=job.step or "starting")

            async def set_step(step: str, _job_id: int = job_id_capture) -> None:  # noqa: B023
                await jobs_repo.set_step(self._db, _job_id, step)
                # Mirror step changes into the heartbeat so the page
                # shows the most recent step without a DB read.
                self._touch(current_job_id=_job_id, current_step=step)

            await self._process_video(self._db, self._config, job.video_id, set_step)
            await jobs_repo.complete(self._db, job.id)
        except Exception as e:
            log.exception("job %s failed", job.id)
            await jobs_repo.fail(self._db, job.id, str(e))
