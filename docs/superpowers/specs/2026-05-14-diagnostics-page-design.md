# Diagnostics page (`/settings/diagnostics`) — design

**Status:** draft
**Date:** 2026-05-14
**Author:** Stefan + Claude

## Problem

The app currently has three independent background tasks:

- `app.worker.Worker` — drains the `jobs` queue (summary pipeline:
  download → transcribe → summarize).
- `app.tts_worker.TtsWorker` — drains the `tts_jobs` queue
  (translate → render → ffprobe).
- `app.scheduler.PlaylistScheduler` — periodically refreshes saved
  YouTube playlists.

Per-video status is observable (`/v/{id}/status` HTMX fragment), and
the settings page shows `scheduler_last_tick_at` plus per-playlist
`last_refreshed_at`. There is **no global view** of:

- whether each background task is alive,
- how many items sit in each queue, in which state,
- which job is running right now and which is up next,
- recent failures and their error messages,
- recent application log output.

The reported symptom: videos sit in `queued` for a long time and the
operator (Stefan) cannot tell whether the worker is wedged, slow, or
fine. The fix is one Settings subpage that answers
"is it running, and what's it doing?" in one screen.

## Goals

- Single page at `/settings/diagnostics` summarizing both queues, all
  three background tasks, recent failures, and the last ~200 log
  lines.
- Targeted recovery actions: retry/delete a failed job, force an
  immediate scheduler tick.
- No new persistent state where transient state will do
  (heartbeats live in `app.state`; the log tail lives in RAM).
- No JS framework, no WebSocket. Static snapshot + manual refresh,
  matching the rest of the Settings UX.

## Non-goals

- Authn/authz on action routes. The whole Settings surface is
  unauthenticated by design (single-user LAN tool, see
  `app/main.py`); diagnostics follows the same model. If that
  changes later it changes everywhere at once.
- Persisting heartbeats or the log buffer across container
  restarts. A restart wipes both — that's fine; a fresh process is
  the only thing the operator wants to know about anyway.
- Live updates (HTMX polling / WebSocket push). Page is a snapshot
  with a manual "Aktualisieren" link.
- Log filtering / search / download.
- A job-detail drilldown page. The "currently running" row links to
  the existing `/v/{video_id}` page; that's enough.
- Cleaning up MP3 files on disk when deleting a failed TTS job row.
  `failed` jobs generally have no audio output. If they do, the file
  becomes orphaned — a separate cleanup task can sweep these later.

## Surface

### New route

```
GET  /settings/diagnostics               → diagnostics.html
POST /settings/diagnostics/retry-job/{id}        → 303 → /settings/diagnostics
POST /settings/diagnostics/delete-job/{id}       → 303 → /settings/diagnostics
POST /settings/diagnostics/retry-tts/{id}        → 303 → /settings/diagnostics
POST /settings/diagnostics/delete-tts/{id}       → 303 → /settings/diagnostics
POST /settings/diagnostics/tick-scheduler        → 303 → /settings/diagnostics
```

All routes live in `app/routes/settings.py` alongside the existing
settings handlers — same auth model, same redirect pattern.

### UI layout

The template `app/templates/diagnostics.html` renders five sections,
top to bottom:

1. **Header bar** — page title, snapshot timestamp (`Stand: HH:MM:SS`),
   `← Zurück zu Settings` link, `Aktualisieren` link (re-GET of the
   page). No auto-poll.
2. **Worker heartbeat card** — 3 rows (Summary-Worker, TTS-Worker,
   Scheduler). Each row: name, status pill (`✅ alive` / `⚠ stale`
   / `⛔ never`), last-tick timestamp, current job id + step (or
   `idle` when nothing is in flight).
3. **Summary queue card** — header chips (`queued: N · running: N ·
   failed: N · done (24h): N`). Subsections:
   - "Aktuell laufend" — single row (video title linking to
     `/v/{id}`, step, seconds elapsed since `updated_at`). Hidden
     when nothing is running.
   - "Als Nächstes" — up to 10 pending rows, FIFO order (video
     title, wait time).
   - "Letzte Fehler" — up to 10 failed rows (video title, error
     snippet, `finished` timestamp, `[Retry]` `[Löschen]` forms).
4. **TTS queue card** — same shape as Summary card. The active state
   is `translating` or `rendering` (both count as "running" in the
   header chip). "Letzte Fehler" Retry preserves
   `tts_jobs.translated_text` so we don't pay the LLM again.
5. **Scheduler card** — last tick timestamp (in-memory + persisted
   `scheduler_last_tick_at`), per-playlist last-refresh table
   (already shown on the main settings page; we duplicate the
   compact view here so diagnostics is self-contained), and a
   `Jetzt prüfen` button (`POST /tick-scheduler`).
6. **Log tail** — `<pre>` block with the last 200 lines from the
   in-memory ring buffer. Default 200; `?lines=500` query param
   reads up to the buffer cap.

### Settings page entry point

`app/templates/settings.html` gets one new link in the footer / a
small card at the bottom: `Diagnose / Worker-Status → /settings/diagnostics`.
No icon shuffling, no nav-bar change.

## Data + module changes

### `app/services/heartbeat.py` (new)

Pure in-memory registry. Module-level singleton keyed by worker name.
Stored on `app.state.heartbeats` so tests can inject a fresh registry.

```python
@dataclass(frozen=True)
class Heartbeat:
    name: str                       # "summary_worker" | "tts_worker" | "scheduler"
    last_tick_at: datetime          # UTC, naive (matches DB convention)
    current_job_id: int | None
    current_step: str | None

class HeartbeatRegistry:
    def touch(
        self,
        name: str,
        *,
        current_job_id: int | None = None,
        current_step: str | None = None,
    ) -> None: ...
    def snapshot(self) -> dict[str, Heartbeat]: ...
```

`touch()` is synchronous, lock-free (single-writer per worker, dict
assignment is atomic in CPython). `snapshot()` returns a shallow
copy so the template iterates without racing further writes.

Worker call sites:

- `Worker.run()` — `touch("summary_worker", current_job_id=…, current_step=job.step)` immediately after `claim_next` returns, **and** when it returns `None` (touch with `job_id=None, step=None` so an idle worker still proves it's alive).
- `TtsWorker.run()` — same pattern, name `"tts_worker"`.
- `PlaylistScheduler.run()` — touch at the top of each iteration with `step=playlist_id` while syncing, then `step=None` when sleeping.

A worker is `alive` if `now - last_tick_at < 3× poll_interval` (so
the 1 s poller has a ~3 s budget; the scheduler with a 60-min
interval gets ~3 h — sensible default). `stale` if older;
`never` if no heartbeat was ever recorded.

`poll_interval` is read from each worker instance via
`request.app.state.worker._poll_interval` /
`...tts_worker._poll_interval` / a new
`scheduler.current_interval_seconds()` async helper that returns the
same value `_interval_seconds()` already computes. To keep the
diagnostics route from caring about these internals, the spec-level
contract is: each worker exposes a public read-only
`poll_interval_seconds` property, used only by the diagnostics view.

### `app/services/log_buffer.py` (new)

```python
class RingBufferHandler(logging.Handler):
    """Thread-safe ring buffer of formatted log lines.

    Installed once at app startup on the root logger. The diagnostics
    page reads snapshot() and renders it as a <pre> block.
    """
    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._buf: deque[str] = deque(maxlen=capacity)
        # collections.deque is thread-safe for append/popleft but
        # NOT for snapshot iteration; we use a lock for the read.
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        self._buf.append(line)

    def snapshot(self, limit: int | None = None) -> list[str]:
        with self._lock:
            data = list(self._buf)
        if limit is not None and len(data) > limit:
            return data[-limit:]
        return data
```

Installed in `app/main.py:lifespan` immediately after `Config.from_env()`:

```python
buf = RingBufferHandler(capacity=500)
buf.setLevel(logging.INFO)
buf.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s"
))
logging.getLogger().addHandler(buf)
app.state.log_buffer = buf
```

Existing stdout logging keeps working — we only **add** a handler.

### `app/repos/jobs.py` additions

```python
async def counts(db) -> dict[str, int]:
    """Returns {"pending": N, "running": N, "failed": N, "done_24h": N}."""
async def list_queue(db, limit: int = 10) -> list[tuple[Job, str]]:
    """Pending + running jobs FIFO, with the video title joined in."""
async def list_recent_failed(db, limit: int = 10) -> list[tuple[Job, str]]:
    """Failed jobs newest first, with video title."""
async def retry(db, job_id: int) -> None:
    """failed → pending, clear error_message, bump updated_at."""
async def delete(db, job_id: int) -> None:
    """DELETE FROM jobs WHERE id=?. Video row is untouched."""
```

`list_queue` and `list_recent_failed` return `(Job, title)` tuples;
the title is materialized via `LEFT JOIN videos ON videos.id = jobs.video_id`
so a deleted video still renders something useful (fall back to
`job.video_id` in the template).

### `app/repos/tts_jobs.py` additions

Same five functions, mirrored for the TTS queue. `counts` collapses
`translating + rendering` into a single "running" bucket. `retry`
clears `error`, `started_at`, `finished_at`, sets `status='queued'`,
**preserves `translated_text`** (so a render-stage failure doesn't
pay the LLM cost again — same logic the worker already uses for
crash-resume).

### `app/scheduler.py` change

Add a manual-tick wakeup channel. Today the loop sleeps via
`asyncio.wait_for(self._stopped.wait(), seconds)`; we add a second
event:

```python
def __init__(self, ...):
    ...
    self._stopped = asyncio.Event()
    self._tick_requested = asyncio.Event()

def request_tick(self) -> None:
    """Wake the scheduler so the next iteration runs immediately."""
    self._tick_requested.set()

async def _sleep_or_stop(self, seconds: float) -> None:
    """Wait up to `seconds`, but return early on stop OR tick request."""
    stop_task = asyncio.create_task(self._stopped.wait())
    tick_task = asyncio.create_task(self._tick_requested.wait())
    try:
        await asyncio.wait(
            {stop_task, tick_task},
            timeout=seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (stop_task, tick_task):
            if not t.done():
                t.cancel()
        self._tick_requested.clear()
```

`POST /settings/diagnostics/tick-scheduler` calls
`request.app.state.scheduler.request_tick()` and redirects.

### `app/routes/settings.py` additions

One GET handler (`diagnostics_page`) and five POST handlers. The
GET reads:

- `request.app.state.heartbeats.snapshot()`
- `jobs_repo.counts(db)`, `jobs_repo.list_queue(db)`, `jobs_repo.list_recent_failed(db)`
- `tts_jobs_repo.counts(db)`, `tts_jobs_repo.list_queue(db)`, `tts_jobs_repo.list_recent_failed(db)`
- `settings_repo.get(db, "scheduler_last_tick_at")`
- `playlists_repo.list_for_user(db, 1)` (same call as the main settings page)
- `request.app.state.log_buffer.snapshot(limit=lines)`

…and passes everything into `diagnostics.html`.

POST handlers are thin wrappers around the repo `retry` / `delete`
functions and the scheduler's `request_tick()`. All return
`RedirectResponse("/settings/diagnostics", status_code=303)`.

## Failure modes considered

- **Worker never registers a heartbeat.** Page shows `⛔ never`.
  The operator at least sees "the worker didn't even start" rather
  than a silent hang.
- **Worker is stuck inside `process_video`.** Heartbeat is updated
  after `claim_next` returns, *before* `process_video` runs, so a
  stuck worker still shows `alive` but with a stale `current_step`.
  The "Aktuell laufend" row shows the seconds-elapsed counter, so
  the operator can spot it (`step="downloading" · 3600s` is a clear
  red flag). We do not try to detect "stuck" automatically — that's
  a heuristic minefield (a long Whisper transcribe is legitimately
  slow on a Pi).
- **Retry on a non-failed job.** Repo functions assert
  `state='failed'` (resp. `status='failed'`) in the WHERE clause
  and `rowcount == 0` triggers a 404. Prevents accidentally
  resetting a running job from another tab.
- **Delete on a running job.** Same `rowcount` check — only failed
  rows are deletable. Operator must wait for the job to finish or
  the worker to crash.
- **Logger handler exception.** `RingBufferHandler.emit` wraps the
  `format()` call in a bare try/except (Python's logging contract:
  a handler must never raise; the parent `logging.Handler.emit`
  pattern is identical).
- **Snapshot during high log volume.** `snapshot()` takes a lock,
  copies under it (`list(self._buf)` runs in C), releases. Worst
  case: a few milliseconds of contention.
- **Container restart wipes heartbeats and the log buffer.** Acceptable
  — the page renders, the worker rows show `⛔ never` until each
  worker's first tick (typically 1 s), and the log tail is empty
  until the first INFO line is emitted by startup. Both populate
  within seconds.

## Testing strategy

Each new module gets its own focused test file. No integration
theater — the existing pipeline tests already cover the worker's
happy path.

- `tests/test_log_buffer.py`
  - emit appends, capacity caps at N, `snapshot()` returns a copy
    that survives further writes, `snapshot(limit=)` slices the tail.
  - emit swallows formatter exceptions (e.g. bad `%`-style record).
- `tests/test_heartbeat.py`
  - `touch` writes, `snapshot` returns a copy, multiple workers
    don't clobber each other.
- `tests/test_repos_jobs.py` (extend)
  - `counts` math (24h window respected against `updated_at`).
  - `list_queue` ordering = `created_at ASC, id ASC`, includes
    title via JOIN, missing video falls back to id.
  - `list_recent_failed` ordering = `updated_at DESC`.
  - `retry` flips state and clears error_message; running rows are
    rejected.
  - `delete` removes failed rows; running rows are rejected.
- `tests/test_repos_tts_jobs.py` (extend)
  - Same as above plus: `retry` preserves `translated_text`,
    clears `error/started_at/finished_at`.
- `tests/test_scheduler.py` (extend)
  - `request_tick()` makes the sleep return early; `_tick_requested`
    is cleared after the wakeup so back-to-back ticks each get a
    real cycle.
- `tests/test_routes_diagnostics.py` (new)
  - GET renders 200 and contains the three worker names + both
    queue headers.
  - GET handles empty queues / empty log buffer without crashing.
  - POST retry / delete / tick-scheduler return 303 and produce
    the expected DB / scheduler effect.

## Open questions / future work

- A future PR could add a `/settings/diagnostics.json` endpoint for
  scripted health checks (e.g. `curl | jq`). Out of scope here —
  the HTML page is what the request asks for.
- Heuristic "stuck worker" detection (long-running with no step
  change) is deliberately deferred. Whisper on a Pi can legitimately
  spend 20 min on a single chunk; we'd produce false positives.
- A retry button for the *scheduler* (if a playlist sync repeatedly
  fails) isn't surfaced. The scheduler's `list_for_user` /
  `_sync_fn` loop only logs and continues; it has no per-playlist
  "failed" state. Adding that would be a bigger change and isn't
  asked for.
