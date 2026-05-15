# Worker resurrection + realistic heartbeat threshold — design

**Status:** draft
**Date:** 2026-05-15
**Author:** Stefan + Claude

## Problem

On Stefan's Raspberry Pi deployment, the diagnostics page shows the
`summary_worker` as `⚠ stale (57121s)` (≈ 16 hours) while three videos
sit in `pending` state. The `tts_worker` and `scheduler` in the same
process are alive, so the asyncio event loop itself is healthy — only
the summary worker's coroutine has died.

Reading the code confirms three independent defects that conspire
toward this outcome:

1. **Worker.run()'s exception handling only catches `Exception`.**
   `BaseException`-subclass exceptions (`asyncio.CancelledError`,
   `SystemExit`, `KeyboardInterrupt`, or anything else raised from
   deep inside Whisper/LiteLLM that doesn't inherit from `Exception`)
   propagate out of the loop, the task ends, and nothing restarts it.
   The lifespan's `await worker_task` later silently reaps the
   already-dead task on shutdown.

2. **`claim_next()` is called outside the inner try/except.**
   A `aiosqlite.OperationalError: database is locked` from heavy SD
   card contention on a Pi, or any other DB-layer crash, kills the
   task even though it IS an `Exception` subclass — because the
   only `except Exception` in `run()` wraps the `process_video`
   call, not the `claim_next` call before it.

3. **The "alive vs stale" threshold is 3× the poll interval (3 s).**
   That's appropriate for "the loop is alive" but useless for "the
   worker is doing something useful." A Whisper transcribe on a Pi
   can run for tens of minutes inside a single `process_video` call,
   firing `set_step` only at chunk boundaries (every few seconds to
   a minute). During Whisper's long initial model-load or any
   between-chunk gap, the heartbeat ages past 3 s and the page
   wrongly reports `⚠ stale` even though the worker is working.

The fix is a single PR that addresses all three.

## Goals

- The summary and TTS workers must survive any single-iteration
  exception (including `BaseException` subclasses other than
  `asyncio.CancelledError`) and resume the loop after a brief delay.
- DB-level failures during `claim_next` (or any other line in the
  loop body, not just `process_video`) must be caught and survived.
- The diagnostics page's "alive vs stale" threshold must reflect
  the actual heartbeat cadence the workers can sustain during real
  work, not the loop's idle poll interval.
- The fix must not regress shutdown semantics — `CancelledError`
  raised by the lifespan to terminate the task must propagate.

## Non-goals

- Watchdog for in-flight `running` jobs that have stalled without
  the worker crashing. The existing `jobs_repo.reset_orphaned_running`
  call on startup handles the after-restart case; a heuristic
  "this job has been running too long" detector is a separate, much
  riskier feature (Whisper on a Pi can legitimately run 20+ minutes).
- Per-worker configurable thresholds in the Settings UI. A fixed
  5-minute window for both pollers is good enough; tuning is a
  code change.
- Additional `_touch()` calls in pipeline hot loops (Whisper inner
  chunks, summarizer streaming). The 5-minute window absorbs the
  natural cadence of existing `set_step` callbacks.
- Exponential backoff or max-retry counters in the loop. Constant
  5-second sleep after a crash is fine for a home-LAN tool; if the
  underlying problem persists, the log tail will show the operator
  what to fix.
- Diagnostics-page "crashed N times" counter or restart button.
  The log tail already surfaces crashes; YAGNI for a UI knob.

## Surface

### `app/worker.py`

The `Worker.run()` method is split into two layers:

1. An outer `while not self._stopped.is_set()` loop that wraps each
   iteration in a try/except. It catches all `BaseException` except
   `asyncio.CancelledError`, logs with traceback, stamps the
   heartbeat with `current_step="restarting after crash"`, and
   sleeps 5 seconds (interruptible by the stop event) before
   continuing.

2. An inner `_run_iteration()` method holding the existing loop
   body verbatim: `claim_next`, idle-tick heartbeat, the inner
   try/except around `process_video`, and the `set_step`-mirroring
   closure. The inner try/except is unchanged — pipeline failures
   still mark the job `failed` and write the error message to the
   DB, exactly as today.

The `_touch` helper gets one new call site: at the start of the
"restarting after crash" path, so the diagnostics page reflects
the crash for at least one snapshot before the next iteration
overwrites it. The call deliberately passes no `current_job_id`
(default `None`) — by the time control reaches the outer except,
the closure that captured the in-flight job id may be in an
inconsistent state, and the operator's correct mental model is
"the worker just blew up, all bets are off" anyway.

```python
async def run(self) -> None:
    while not self._stopped.is_set():
        try:
            await self._run_iteration()
        except asyncio.CancelledError:
            # Shutdown — propagate so the lifespan can await us.
            raise
        except BaseException:
            log.exception(
                "summary_worker: unexpected crash, restarting in 5s",
            )
            self._touch(current_step="restarting after crash")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), 5.0)

async def _run_iteration(self) -> None:
    job = await jobs_repo.claim_next(self._db)
    if job is None:
        self._touch()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
        return
    try:
        job_id_capture = job.id
        self._touch(current_job_id=job.id, current_step=job.step or "starting")
        # ... existing set_step closure + process_video call unchanged ...
        await self._process_video(self._db, self._config, job.video_id, set_step)
        await jobs_repo.complete(self._db, job.id)
    except Exception as e:
        log.exception("job %s failed", job.id)
        await jobs_repo.fail(self._db, job.id, str(e))
```

### `app/tts_worker.py`

The same two-layer pattern. The inner `_run_iteration()` holds the
existing body (`claim_next`, idle-tick `_touch`, the inner try/except
calling `self._process(job)` and either `tts_jobs_repo.complete(...)`
or `tts_jobs_repo.fail(...)`).

Heartbeat name `"tts_worker"`; everything else identical to
`Worker`'s structure.

### `app/routes/settings.py:diagnostics_page`

The `worker_thresholds_seconds` dictionary changes from a
poll-interval multiple to a fixed 5-minute constant for the two
polling workers. The scheduler keeps its interval-derived threshold
(its expected idle period is hours, not seconds).

```python
# Polling workers (Worker, TtsWorker) can spend minutes in a single
# Whisper or render step without firing a heartbeat update. A
# pessimistic 3× poll_interval threshold (≈ 3 s) would mark them
# stale every long-running job, which is the opposite of useful.
# Stick with a generous fixed window: if no tick in 5 minutes, the
# worker is genuinely stuck or dead. The scheduler keeps its
# interval-derived threshold because its expected idle is hours.
_POLLER_STALE_SECONDS = 300.0

worker_thresholds_seconds = {
    "summary_worker": _POLLER_STALE_SECONDS,
    "tts_worker":     _POLLER_STALE_SECONDS,
    "scheduler":      (await scheduler.current_interval_seconds()) * 3,
}
```

The `worker.poll_interval_seconds` / `tts_worker.poll_interval_seconds`
properties stay on the worker classes — tests use them, and they
remain meaningful as the actual poll interval; we just stop using
them as the staleness threshold.

## Failure modes considered

- **`claim_next` raises `OperationalError: database is locked`.**
  Outer try/except catches it, logs the full traceback, sleeps 5 s.
  By then SQLite's lock should have cleared (typical contention is
  sub-second; a checkpoint-induced hold is at most a few seconds).
  Loop continues.

- **`process_video` raises a non-`Exception` `BaseException`.** The
  current inner `except Exception` doesn't catch it; without this
  PR the task dies. With this PR the outer except catches it, logs,
  and resumes. The half-claimed job stays in `running` state —
  fine, the existing `jobs_repo.reset_orphaned_running` on the
  next process restart picks it up. We deliberately don't try to
  flip the job to `failed` here because we'd need to know its id,
  and the crash path may have already corrupted the closure.

- **Container/systemd kills the process mid-iteration with SIGTERM.**
  uvicorn translates SIGTERM into a `CancelledError` on each running
  task, which the outer except re-raises by name. The lifespan's
  `await worker_task` then completes normally and the lifespan
  shutdown proceeds.

- **Persistent crash loop.** If something keeps raising on every
  iteration, the worker logs the same traceback every 5 seconds.
  The operator sees this in the diagnostics log tail and on the
  Worker-Status row (where `current_step` will alternate between
  `restarting after crash` and the next-iteration step). No
  automatic exponential-backoff — a home tool's operator can
  intervene faster than a backoff strategy can stabilize a Pi.

- **Whisper transcribe takes 8 minutes on a single video.** Inside
  that 8 minutes, `set_step` fires per chunk (every ~1 minute for
  faster-whisper's default chunk-length), which calls `_touch` via
  the closure. Heartbeat stays well under the 300 s threshold.
  Worker shows `✅ alive`.

- **Whisper transcribe stalls inside a single chunk for 6 minutes.**
  No `set_step` calls during that window. Heartbeat ages past 300 s,
  page shows `⚠ stale (320s)`. False alarm in the sense that the
  worker isn't dead, but acceptable in the sense that the operator
  has a real reason to investigate (something in Whisper is hung).
  The status flips back to `✅ alive` as soon as the chunk
  completes and `set_step` runs again.

- **Heartbeat threshold for a fresh-start worker.** The first
  iteration of `run()` either calls `_touch()` (idle path) or
  `_touch(current_job_id=...)` (active path) within ~1 s. Before
  that very first tick, `heartbeats.get("summary_worker")` returns
  `None` and the page shows `⛔ never` — same behavior as today,
  unchanged by this PR.

## Testing strategy

All new tests live in the existing `tests/test_worker.py`,
`tests/test_tts_worker.py`, and `tests/test_routes_diagnostics.py`
files. No new test files needed.

- `tests/test_worker.py` (extend)
  - `test_worker_survives_runtime_error_in_process_video`: the
    fake `process_video` raises `RuntimeError` on the first call,
    returns `None` on the second. After running long enough for
    two iterations, the worker is still alive and the second job
    was processed.
  - `test_worker_survives_baseexception_in_process_video`: fake
    raises a `BaseException` subclass (e.g., a custom
    `class SneakyError(BaseException): ...`) on the first call.
    Loop survives, second iteration runs.
  - `test_worker_survives_claim_next_failure`: monkeypatch
    `jobs_repo.claim_next` to raise `aiosqlite.OperationalError`
    on first call, normal return on second. Loop survives, second
    iteration claims the seeded pending job and completes it.
  - `test_worker_propagates_cancelled_error_for_shutdown`: start
    the worker, send `task.cancel()`, await `task` — expect
    `CancelledError` to surface. Without this assertion, a bug
    that accidentally caught `CancelledError` in the outer except
    would mask shutdown failures.
  - `test_worker_heartbeat_marks_restart`: induce a crash, snapshot
    the heartbeat within the 5 s recovery window, assert
    `current_step == "restarting after crash"`.

- `tests/test_tts_worker.py` (extend) — same five tests, adapted to
  the TtsWorker's `_process(job)` interface.

- `tests/test_routes_diagnostics.py` (extend)
  - `test_summary_worker_alive_within_300s`: set the heartbeat
    timestamp to 250 s ago, render the page, assert `✅ alive`
    appears in the summary_worker row.
  - `test_summary_worker_stale_past_300s`: set the heartbeat
    timestamp to 350 s ago, assert `⚠ stale` appears.
  - `test_scheduler_threshold_still_uses_interval`: with
    `playlist_refresh_interval_minutes=60`, a scheduler heartbeat
    from 2 hours ago is still `✅ alive`; one from 4 hours ago is
    `⚠ stale`.

Test plumbing: the existing scheduler-test pattern of patching the
`HeartbeatRegistry` and asserting on rendered HTML carries over.
For the crash-survival tests, the existing `process_video=AsyncMock(...)`
pattern in `test_worker.py` extends naturally — just have the mock
raise on its first invocation.

## Open questions / future work

- If the worker crashes in a tight loop (a poisoned video that
  reliably triggers the crash before the job can be marked `failed`),
  the user sees log spam and `restarting after crash` heartbeat
  flapping. A future improvement could track a per-video crash
  count and skip-list the offender after N crashes. Out of scope
  here — Stefan's actual problem is a one-time crash, not a poison
  pill, and a heuristic skip would risk dropping legitimate work.

- The 5-minute threshold is a guess based on observed Whisper
  chunk cadence on a Pi5. If real deployments show false-positive
  staleness more than once a week, the threshold can be raised
  (constant change in one file). If false-negative misses
  ("worker dead but page says alive") become a problem because
  it takes 5 minutes to detect, additional `_touch` calls inside
  Whisper/Summarizer hot loops would lower the realistic floor —
  follow-up.

- The diagnostics page could differentiate `⚠ stale` (heartbeat
  older than threshold but `current_step != "restarting after crash"`)
  from `⚠ crashed, retrying` (when the step IS that string). A
  small visual nicety; deferred.
