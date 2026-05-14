# Diagnostics Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single Settings subpage at `/settings/diagnostics` that shows worker heartbeats, queue contents (summary + TTS), recent failures, and a tail of in-memory logs, with retry/delete/tick-scheduler actions.

**Architecture:** Two new in-memory services (heartbeat registry, log ring-buffer handler) wired into `app.state` during `lifespan` startup. Six new repo functions split across `app/repos/jobs.py` and `app/repos/tts_jobs.py`. One scheduler change for a manual-tick wakeup channel. One new template `diagnostics.html` and six new routes in `app/routes/settings.py`. No DB migration, no new dependencies. Static page + manual refresh — no polling, no WebSocket.

**Tech Stack:** Python 3.11+, FastAPI, aiosqlite, Jinja2, pytest-asyncio (`asyncio_mode = "auto"`). All existing — no additions.

---

## File Structure

**New files:**
- `app/services/heartbeat.py` — `Heartbeat` dataclass + `HeartbeatRegistry` (touch/snapshot).
- `app/services/log_buffer.py` — `RingBufferHandler(logging.Handler)`.
- `app/templates/diagnostics.html` — extends `base.html`, renders the page.
- `tests/test_heartbeat.py`
- `tests/test_log_buffer.py`
- `tests/test_routes_diagnostics.py`

**Modified files:**
- `app/worker.py` — `Worker.__init__` takes optional `heartbeat` registry; `run()` calls `touch("summary_worker", ...)` per loop iteration. New `poll_interval_seconds` property.
- `app/tts_worker.py` — same pattern, name `"tts_worker"`. New `poll_interval_seconds` property.
- `app/scheduler.py` — `PlaylistScheduler.__init__` takes optional `heartbeat`; new `_tick_requested: asyncio.Event`, new `request_tick()`, new `current_interval_seconds()` public wrapper, `_sleep_or_stop` reworked to wake on tick request, `run()` touches the registry per iteration.
- `app/repos/jobs.py` — add `counts`, `list_queue`, `list_recent_failed`, `retry`, `delete`.
- `app/repos/tts_jobs.py` — add `counts`, `list_queue`, `list_recent_failed`, `retry` (note: `delete` already exists).
- `app/main.py` — install `RingBufferHandler` on root logger in `lifespan`; create `HeartbeatRegistry`; wire it into all three background tasks; expose `app.state.heartbeats` and `app.state.log_buffer`.
- `app/routes/settings.py` — add one GET + five POST handlers.
- `app/templates/settings.html` — add a footer link to `/settings/diagnostics`.
- `tests/test_repos_jobs.py` — extend with cases for the five new functions.
- `tests/test_repos_tts_jobs.py` — extend with cases for the four new functions.
- `tests/test_scheduler.py` — extend with `request_tick()` test.

---

## Task 1: HeartbeatRegistry service

**Files:**
- Create: `app/services/heartbeat.py`
- Test: `tests/test_heartbeat.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_heartbeat.py`:

```python
from datetime import datetime

from app.services.heartbeat import Heartbeat, HeartbeatRegistry


def test_touch_records_heartbeat_for_new_worker():
    reg = HeartbeatRegistry()
    reg.touch("summary_worker", current_job_id=42, current_step="downloading")
    snap = reg.snapshot()
    assert "summary_worker" in snap
    hb = snap["summary_worker"]
    assert isinstance(hb, Heartbeat)
    assert hb.name == "summary_worker"
    assert hb.current_job_id == 42
    assert hb.current_step == "downloading"
    assert isinstance(hb.last_tick_at, datetime)


def test_touch_updates_existing_worker():
    reg = HeartbeatRegistry()
    reg.touch("summary_worker", current_job_id=1, current_step="a")
    reg.touch("summary_worker", current_job_id=2, current_step="b")
    snap = reg.snapshot()
    assert snap["summary_worker"].current_job_id == 2
    assert snap["summary_worker"].current_step == "b"


def test_touch_with_no_job_marks_worker_idle():
    reg = HeartbeatRegistry()
    reg.touch("tts_worker")
    snap = reg.snapshot()
    assert snap["tts_worker"].current_job_id is None
    assert snap["tts_worker"].current_step is None


def test_multiple_workers_do_not_clobber_each_other():
    reg = HeartbeatRegistry()
    reg.touch("summary_worker", current_job_id=1, current_step="x")
    reg.touch("tts_worker", current_job_id=99, current_step="rendering")
    reg.touch("scheduler", current_step="p1")
    snap = reg.snapshot()
    assert snap["summary_worker"].current_job_id == 1
    assert snap["tts_worker"].current_step == "rendering"
    assert snap["scheduler"].current_step == "p1"


def test_snapshot_returns_a_copy():
    reg = HeartbeatRegistry()
    reg.touch("summary_worker", current_step="initial")
    snap = reg.snapshot()
    reg.touch("summary_worker", current_step="later")
    # The earlier snapshot must not have changed.
    assert snap["summary_worker"].current_step == "initial"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_heartbeat.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.heartbeat'`.

- [ ] **Step 3: Write the implementation**

Create `app/services/heartbeat.py`:

```python
"""In-memory heartbeat registry for background workers.

Each long-running background task (summary worker, TTS worker,
playlist scheduler) calls :meth:`HeartbeatRegistry.touch` once per
loop iteration. The diagnostics page reads :meth:`snapshot` to
render an "is the worker alive?" view.

State is intentionally process-local — it's reset on container
restart, which is the only restart the operator cares about. No
DB row, no migration, no IPC.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Heartbeat:
    """Snapshot of one worker's last observed state.

    ``last_tick_at`` is UTC-naive (matches SQLite ``datetime('now')``
    output used throughout the repos).
    """
    name: str
    last_tick_at: datetime
    current_job_id: int | None = None
    current_step: str | None = None


class HeartbeatRegistry:
    """Process-wide ``name -> Heartbeat`` map.

    Writes are single-producer per worker name; reads (snapshot)
    are O(workers) and return a shallow copy so the caller can
    iterate without racing further writes. No lock is needed —
    dict assignment is atomic in CPython.
    """

    def __init__(self) -> None:
        self._heartbeats: dict[str, Heartbeat] = {}

    def touch(
        self,
        name: str,
        *,
        current_job_id: int | None = None,
        current_step: str | None = None,
    ) -> None:
        self._heartbeats[name] = Heartbeat(
            name=name,
            last_tick_at=datetime.now(UTC).replace(tzinfo=None),
            current_job_id=current_job_id,
            current_step=current_step,
        )

    def snapshot(self) -> dict[str, Heartbeat]:
        """Return a shallow copy of the current registry."""
        return dict(self._heartbeats)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_heartbeat.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/heartbeat.py tests/test_heartbeat.py
git commit -m "feat(diagnostics): heartbeat registry for background workers"
```

---

## Task 2: RingBufferHandler log service

**Files:**
- Create: `app/services/log_buffer.py`
- Test: `tests/test_log_buffer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_log_buffer.py`:

```python
import logging

from app.services.log_buffer import RingBufferHandler


def _record(msg: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )


def test_emit_appends_formatted_line():
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.emit(_record("hello"))
    assert h.snapshot() == ["INFO hello"]


def test_capacity_caps_buffer_length():
    h = RingBufferHandler(capacity=3)
    h.setFormatter(logging.Formatter("%(message)s"))
    for i in range(5):
        h.emit(_record(f"line{i}"))
    # Oldest two dropped.
    assert h.snapshot() == ["line2", "line3", "line4"]


def test_snapshot_returns_copy_that_survives_further_writes():
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(_record("first"))
    snap = h.snapshot()
    h.emit(_record("second"))
    # The earlier snapshot must not see "second".
    assert snap == ["first"]


def test_snapshot_limit_slices_tail():
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    for i in range(6):
        h.emit(_record(f"l{i}"))
    assert h.snapshot(limit=2) == ["l4", "l5"]


def test_emit_swallows_formatter_exceptions():
    """A bad formatter must not crash the logger pipeline."""
    h = RingBufferHandler(capacity=10)
    # %(nonexistent_field) raises KeyError inside Formatter.format.
    h.setFormatter(logging.Formatter("%(nonexistent_field)s"))
    h.emit(_record("ignored"))
    # No exception → snapshot is empty (the bad line was dropped).
    assert h.snapshot() == []


def test_works_attached_to_root_logger():
    """Smoke test: attach to a logger and emit through .info()."""
    logger = logging.getLogger("yt_summary.test.logbuffer")
    logger.setLevel(logging.INFO)
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(h)
    try:
        logger.info("via-logger")
        assert "via-logger" in h.snapshot()
    finally:
        logger.removeHandler(h)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_log_buffer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.log_buffer'`.

- [ ] **Step 3: Write the implementation**

Create `app/services/log_buffer.py`:

```python
"""In-memory ring-buffer logging handler.

Installed once at app startup on the root logger. The diagnostics
page reads :meth:`snapshot` and renders it as a ``<pre>`` block so
the operator can see recent worker activity without shelling into
the container.

Lifetime is process-local — a restart wipes it. That's fine: the
operator only ever cares about *this* process's logs.
"""
from __future__ import annotations

import logging
import threading
from collections import deque


class RingBufferHandler(logging.Handler):
    """Thread-safe ring buffer of formatted log lines.

    :meth:`emit` is called by the logging framework (possibly from
    worker threads, e.g. ``faster-whisper`` or Piper synthesis); it
    appends to a bounded :class:`collections.deque`. :meth:`snapshot`
    takes a lock and copies the deque so the caller can iterate
    without racing further writes.

    Capacity defaults to 500 lines (~30–80 KB at typical log line
    lengths). The diagnostics view shows the last 200 by default.
    """

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._buf: deque[str] = deque(maxlen=capacity)
        # collections.deque append/popleft are thread-safe in CPython,
        # but iterating *while another thread appends* is not. The
        # lock protects only the snapshot read; writes stay lock-free.
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        # Python's logging contract: a handler must never raise. We
        # mirror the parent's try/except style here — a bad formatter
        # or unhashable arg drops the single line silently.
        try:
            line = self.format(record)
        except Exception:
            return
        self._buf.append(line)

    def snapshot(self, limit: int | None = None) -> list[str]:
        """Return the buffered lines, optionally trimmed to the last
        ``limit`` entries."""
        with self._lock:
            data = list(self._buf)
        if limit is not None and len(data) > limit:
            return data[-limit:]
        return data
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_log_buffer.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/log_buffer.py tests/test_log_buffer.py
git commit -m "feat(diagnostics): in-memory ring-buffer log handler"
```

---

## Task 3: `jobs` repo additions — counts + list helpers

**Files:**
- Modify: `app/repos/jobs.py` (append new functions at end)
- Test: `tests/test_repos_jobs.py` (append cases at end)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repos_jobs.py`:

```python
import asyncio
from datetime import UTC, datetime, timedelta


async def test_counts_buckets_match_state(db: aiosqlite.Connection):
    """One job in each of the four buckets, all reachable via the
    public repo API (no raw SQL fiddling)."""
    await _video(db, "a")
    await _video(db, "b")
    await _video(db, "c")
    await _video(db, "d")
    # 1 pending — enqueue and leave it alone.
    await jobs_repo.enqueue(db, "a")
    # 1 running — enqueue then claim_next promotes oldest pending to running.
    await jobs_repo.enqueue(db, "b")
    claimed = await jobs_repo.claim_next(db)
    assert claimed is not None and claimed.video_id == "a"
    # claim_next moved "a" → running, leaving "b" pending. Re-stage:
    # enqueue another pending so the "running" bucket holds exactly 1
    # and "pending" still holds 1 ("b").
    # 1 failed
    j_failed = await jobs_repo.enqueue(db, "c")
    await jobs_repo.fail(db, j_failed, "boom")
    # 1 done within 24h
    j_done = await jobs_repo.enqueue(db, "d")
    await jobs_repo.complete(db, j_done)

    counts = await jobs_repo.counts(db)
    assert counts["pending"] == 1   # "b"
    assert counts["running"] == 1   # "a"
    assert counts["failed"] == 1    # "c"
    assert counts["done_24h"] == 1  # "d"


async def test_counts_done_24h_excludes_old_rows(db: aiosqlite.Connection):
    await _video(db, "old")
    j = await jobs_repo.enqueue(db, "old")
    await jobs_repo.complete(db, j)
    # Backdate the row past the 24h window.
    await db.execute(
        "UPDATE jobs SET updated_at=datetime('now','-2 days') WHERE id=?",
        (j,),
    )
    await db.commit()
    counts = await jobs_repo.counts(db)
    assert counts["done_24h"] == 0


async def test_list_queue_returns_pending_and_running_with_title(
    db: aiosqlite.Connection,
):
    await videos_repo.upsert_metadata(
        db, video_id="a", url="u", title="Alpha", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="b", url="u", title="Beta", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await jobs_repo.enqueue(db, "a")
    await asyncio.sleep(0.01)  # ensure created_at differs
    await jobs_repo.enqueue(db, "b")
    rows = await jobs_repo.list_queue(db, limit=10)
    assert len(rows) == 2
    # FIFO: oldest first.
    assert rows[0][1] == "Alpha"
    assert rows[1][1] == "Beta"


async def test_list_queue_falls_back_to_video_id_when_video_missing(
    db: aiosqlite.Connection,
):
    """If the video row was deleted but a job lingers, the queue view
    must still render something."""
    await _video(db, "v1")
    await jobs_repo.enqueue(db, "v1")
    # Simulate the video being deleted while a job exists. We use raw
    # SQL to bypass any cascade — current schema has no FK cascade on
    # jobs.video_id, but if that changes the test will be the canary.
    await db.execute("DELETE FROM videos WHERE id='v1'")
    await db.commit()
    rows = await jobs_repo.list_queue(db, limit=10)
    assert len(rows) == 1
    job, title = rows[0]
    assert title == "v1"  # fell back to id


async def test_list_recent_failed_orders_newest_first(db: aiosqlite.Connection):
    await _video(db, "a")
    await _video(db, "b")
    ja = await jobs_repo.enqueue(db, "a")
    jb = await jobs_repo.enqueue(db, "b")
    await jobs_repo.fail(db, ja, "first")
    await asyncio.sleep(0.01)
    await jobs_repo.fail(db, jb, "second")
    rows = await jobs_repo.list_recent_failed(db, limit=10)
    assert len(rows) == 2
    assert rows[0][0].id == jb  # newest
    assert rows[1][0].id == ja


async def test_retry_flips_failed_to_pending_and_clears_error(
    db: aiosqlite.Connection,
):
    await _video(db)
    j = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.fail(db, j, "oops")
    await jobs_repo.retry(db, j)
    job = await jobs_repo.get(db, j)
    assert job is not None
    assert job.state is JobState.PENDING
    assert job.error_message is None


async def test_retry_refuses_non_failed_job(db: aiosqlite.Connection):
    """retry on a running/pending/done row must be a no-op (caller will
    see rowcount==0 and 404)."""
    await _video(db)
    j = await jobs_repo.enqueue(db, "v1")
    # Currently pending — retry should refuse.
    affected = await jobs_repo.retry(db, j)
    assert affected == 0
    job = await jobs_repo.get(db, j)
    assert job is not None
    assert job.state is JobState.PENDING  # unchanged


async def test_delete_removes_failed_row(db: aiosqlite.Connection):
    await _video(db)
    j = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.fail(db, j, "x")
    affected = await jobs_repo.delete(db, j)
    assert affected == 1
    assert await jobs_repo.get(db, j) is None


async def test_delete_refuses_running_row(db: aiosqlite.Connection):
    await _video(db)
    j = await jobs_repo.enqueue(db, "v1")
    claimed = await jobs_repo.claim_next(db)
    assert claimed is not None and claimed.id == j
    affected = await jobs_repo.delete(db, j)
    assert affected == 0
    assert await jobs_repo.get(db, j) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_repos_jobs.py -v -k "counts or list_queue or list_recent_failed or retry or delete"`
Expected: FAIL — `AttributeError: module 'app.repos.jobs' has no attribute 'counts'` (and similar for each).

- [ ] **Step 3: Write the implementations**

Append to `app/repos/jobs.py`:

```python
async def counts(db: aiosqlite.Connection) -> dict[str, int]:
    """Aggregate job state counts for the diagnostics page.

    Returns ``{"pending": N, "running": N, "failed": N, "done_24h": N}``.
    ``done_24h`` is bounded to the last 24 hours against ``updated_at``
    so a long-lived install doesn't show a 5-digit number that scrolls
    off-screen.
    """
    cursor = await db.execute(
        """
        SELECT
          SUM(state='pending') AS pending,
          SUM(state='running') AS running,
          SUM(state='failed')  AS failed,
          SUM(state='done' AND updated_at >= datetime('now','-1 day')) AS done_24h
        FROM jobs
        """
    )
    row = await cursor.fetchone()
    # COALESCE the NULLs that SUM returns on an empty table.
    return {
        "pending": row["pending"] or 0,
        "running": row["running"] or 0,
        "failed":  row["failed"]  or 0,
        "done_24h": row["done_24h"] or 0,
    }


async def list_queue(
    db: aiosqlite.Connection, limit: int = 10,
) -> list[tuple[Job, str]]:
    """Pending + running jobs in FIFO order, with the video title.

    ``LEFT JOIN`` so a deleted video still renders — the template
    falls back to the job's ``video_id``.
    """
    cursor = await db.execute(
        """
        SELECT j.*, v.title AS video_title
        FROM jobs j
        LEFT JOIN videos v ON v.id = j.video_id
        WHERE j.state IN ('pending','running')
        ORDER BY j.created_at ASC, j.id ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [(_row_to_job(r), r["video_title"] or r["video_id"]) for r in rows]


async def list_recent_failed(
    db: aiosqlite.Connection, limit: int = 10,
) -> list[tuple[Job, str]]:
    """Failed jobs, newest first, with video title joined in."""
    cursor = await db.execute(
        """
        SELECT j.*, v.title AS video_title
        FROM jobs j
        LEFT JOIN videos v ON v.id = j.video_id
        WHERE j.state = 'failed'
        ORDER BY j.updated_at DESC, j.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [(_row_to_job(r), r["video_title"] or r["video_id"]) for r in rows]


async def retry(db: aiosqlite.Connection, job_id: int) -> int:
    """Reset a failed job back to ``pending`` so the worker picks it
    up. Returns the number of rows changed (0 ⇒ caller should 404).
    """
    cursor = await db.execute(
        """
        UPDATE jobs
        SET state='pending', error_message=NULL, updated_at=datetime('now')
        WHERE id=? AND state='failed'
        """,
        (job_id,),
    )
    await db.commit()
    return cursor.rowcount or 0


async def delete(db: aiosqlite.Connection, job_id: int) -> int:
    """Delete a failed job row. Returns the number of rows deleted
    (0 ⇒ caller should 404). The video row is untouched.
    """
    cursor = await db.execute(
        "DELETE FROM jobs WHERE id=? AND state='failed'",
        (job_id,),
    )
    await db.commit()
    return cursor.rowcount or 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_repos_jobs.py -v`
Expected: PASS (all old tests + 9 new).

- [ ] **Step 5: Commit**

```bash
git add app/repos/jobs.py tests/test_repos_jobs.py
git commit -m "feat(diagnostics): jobs repo counts/list/retry/delete helpers"
```

---

## Task 4: `tts_jobs` repo additions — counts + list helpers + retry

**Files:**
- Modify: `app/repos/tts_jobs.py` (append new functions; `delete` already exists)
- Test: `tests/test_repos_tts_jobs.py` (append cases)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repos_tts_jobs.py`:

```python
import asyncio


async def test_counts_collapses_translating_and_rendering_into_running(
    db: aiosqlite.Connection,
):
    """Both 'translating' and 'rendering' are active states; the
    diagnostics chip shows a single 'running' bucket."""
    await _video(db, "a")
    await _video(db, "b")
    await _video(db, "c")
    j_translating = await r.enqueue(db, "a", "summary", "de", "v", "low")
    j_rendering = await r.enqueue(db, "b", "summary", "de", "v", "low")
    j_failed = await r.enqueue(db, "c", "summary", "de", "v", "low")
    await r.set_status(db, j_translating.id, "translating")
    await r.set_status(db, j_rendering.id, "rendering")
    await r.fail(db, j_failed.id, "boom")

    counts = await r.counts(db)
    assert counts["queued"] == 0  # all three are non-queued now
    assert counts["running"] == 2
    assert counts["failed"] == 1


async def test_counts_done_24h_excludes_old_rows(db: aiosqlite.Connection):
    await _video(db, "a")
    j = await r.enqueue(db, "a", "summary", "de", "v", "low")
    await r.complete(
        db, j.id, audio_path="x.mp3", duration_seconds=1.0, translated_text=None,
    )
    await db.execute(
        "UPDATE tts_jobs SET finished_at=datetime('now','-2 days') WHERE id=?",
        (j.id,),
    )
    await db.commit()
    counts = await r.counts(db)
    assert counts["done_24h"] == 0


async def test_list_queue_returns_queued_and_running_with_title(
    db: aiosqlite.Connection,
):
    await videos_repo.upsert_metadata(
        db, video_id="a", url="u", title="Alpha", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="b", url="u", title="Beta", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    j1 = await r.enqueue(db, "a", "summary", "de", "v", "low")
    await asyncio.sleep(0.01)
    j2 = await r.enqueue(db, "b", "summary", "de", "v", "low")
    rows = await r.list_queue(db, limit=10)
    # Both queued → FIFO by id (created_at not on the schema).
    assert [row[0].id for row in rows] == [j1.id, j2.id]
    assert [row[1] for row in rows] == ["Alpha", "Beta"]


async def test_list_recent_failed_orders_newest_first(db: aiosqlite.Connection):
    await _video(db, "a")
    await _video(db, "b")
    j1 = await r.enqueue(db, "a", "summary", "de", "v", "low")
    j2 = await r.enqueue(db, "b", "summary", "de", "v", "low")
    await r.fail(db, j1.id, "first")
    await asyncio.sleep(0.01)
    await r.fail(db, j2.id, "second")
    rows = await r.list_recent_failed(db, limit=10)
    assert [row[0].id for row in rows] == [j2.id, j1.id]


async def test_retry_resets_failed_back_to_queued_and_clears_error(
    db: aiosqlite.Connection,
):
    await _video(db, "a")
    j = await r.enqueue(db, "a", "summary", "de", "v", "low")
    await r.fail(db, j.id, "boom")
    affected = await r.retry(db, j.id)
    assert affected == 1
    fresh = await r.get(db, j.id)
    assert fresh is not None
    assert fresh.status == "queued"
    assert fresh.error is None
    assert fresh.started_at is None
    assert fresh.finished_at is None


async def test_retry_preserves_translated_text(db: aiosqlite.Connection):
    """A render-stage failure leaves translated_text populated. retry
    must NOT clear it — re-running translation costs the LLM call."""
    await _video(db, "a")
    j = await r.enqueue(db, "a", "summary", "de", "v", "low")
    await r.set_translated_text(db, j.id, "Hallo Welt")
    await r.fail(db, j.id, "render crashed")
    await r.retry(db, j.id)
    fresh = await r.get(db, j.id)
    assert fresh is not None
    assert fresh.translated_text == "Hallo Welt"


async def test_retry_refuses_non_failed_job(db: aiosqlite.Connection):
    await _video(db, "a")
    j = await r.enqueue(db, "a", "summary", "de", "v", "low")
    # status is 'queued' — retry should refuse.
    affected = await r.retry(db, j.id)
    assert affected == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_repos_tts_jobs.py -v -k "counts or list_queue or list_recent_failed or retry"`
Expected: FAIL — `AttributeError: module 'app.repos.tts_jobs' has no attribute 'counts'` (and similar).

- [ ] **Step 3: Write the implementations**

Append to `app/repos/tts_jobs.py`:

```python
async def counts(db: aiosqlite.Connection) -> dict[str, int]:
    """Aggregate TTS-job status counts for the diagnostics page.

    Returns ``{"queued": N, "running": N, "failed": N, "done_24h": N}``.
    Both ``translating`` and ``rendering`` are folded into ``running``
    so the chip stays readable. ``done_24h`` filters by ``finished_at``
    inside the last 24 h.
    """
    cursor = await db.execute(
        """
        SELECT
          SUM(status='queued') AS queued,
          SUM(status IN ('translating','rendering')) AS running,
          SUM(status='failed') AS failed,
          SUM(status='done' AND finished_at >= datetime('now','-1 day')) AS done_24h
        FROM tts_jobs
        """
    )
    row = await cursor.fetchone()
    return {
        "queued":  row["queued"]  or 0,
        "running": row["running"] or 0,
        "failed":  row["failed"]  or 0,
        "done_24h": row["done_24h"] or 0,
    }


async def list_queue(
    db: aiosqlite.Connection, limit: int = 10,
) -> list[tuple[TtsJob, str]]:
    """Queued + active TTS jobs in FIFO order with video title.

    Active = 'translating' or 'rendering' — both are pre-terminal.
    """
    cursor = await db.execute(
        """
        SELECT t.*, v.title AS video_title
        FROM tts_jobs t
        LEFT JOIN videos v ON v.id = t.video_id
        WHERE t.status IN ('queued','translating','rendering')
        ORDER BY t.id ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [(_row_to_tts_job(r), r["video_title"] or r["video_id"]) for r in rows]


async def list_recent_failed(
    db: aiosqlite.Connection, limit: int = 10,
) -> list[tuple[TtsJob, str]]:
    """Failed TTS jobs, newest first."""
    cursor = await db.execute(
        """
        SELECT t.*, v.title AS video_title
        FROM tts_jobs t
        LEFT JOIN videos v ON v.id = t.video_id
        WHERE t.status='failed'
        ORDER BY t.finished_at DESC NULLS LAST, t.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [(_row_to_tts_job(r), r["video_title"] or r["video_id"]) for r in rows]


async def retry(db: aiosqlite.Connection, job_id: int) -> int:
    """Reset a failed TTS job back to 'queued'. Preserves
    ``translated_text`` so a render-stage failure doesn't waste the
    LLM translation cost on re-run. Returns rows changed (0 ⇒ 404).
    """
    cursor = await db.execute(
        """
        UPDATE tts_jobs
        SET status='queued',
            error=NULL,
            started_at=NULL,
            finished_at=NULL
        WHERE id=? AND status='failed'
        """,
        (job_id,),
    )
    await db.commit()
    return cursor.rowcount or 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_repos_tts_jobs.py -v`
Expected: PASS (existing tests + 7 new).

- [ ] **Step 5: Commit**

```bash
git add app/repos/tts_jobs.py tests/test_repos_tts_jobs.py
git commit -m "feat(diagnostics): tts_jobs repo counts/list/retry helpers"
```

---

## Task 5: Scheduler `request_tick()` + `current_interval_seconds()`

**Files:**
- Modify: `app/scheduler.py`
- Test: `tests/test_scheduler.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scheduler.py`:

```python
async def test_request_tick_wakes_a_long_sleep(db: aiosqlite.Connection, tmp_path):
    """request_tick() must make the scheduler return from a long sleep
    so the next iteration fires immediately (the manual 'Jetzt prüfen'
    button on /settings/diagnostics relies on this)."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await _make_playlist(db, "p1")
    # Long interval so the natural wake-up doesn't muddy the test.
    await settings_repo.set(db, "playlist_refresh_interval_minutes", "60")

    sync_calls: list[str] = []

    async def fake_sync(db_, config_, playlist_id):
        sync_calls.append(playlist_id)

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=fake_sync, min_sleep_seconds=0.05
    )
    task = asyncio.create_task(scheduler.run())
    # Let the scheduler settle into its first long sleep.
    await asyncio.sleep(0.1)
    scheduler.request_tick()
    for _ in range(40):
        await asyncio.sleep(0.05)
        if sync_calls:
            break
    scheduler.stop()
    await task

    assert sync_calls == ["p1"]


async def test_request_tick_is_cleared_after_wakeup(
    db: aiosqlite.Connection, tmp_path,
):
    """After waking, the flag must auto-clear so the next iteration
    sleeps normally — otherwise we'd loop hot."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await settings_repo.set(db, "playlist_refresh_interval_minutes", "60")

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=AsyncMock(), min_sleep_seconds=0.05
    )
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.1)
    scheduler.request_tick()
    # Give it time to consume the tick.
    await asyncio.sleep(0.3)
    # If the flag weren't cleared, _tick_requested.is_set() would still
    # be True. We assert it was cleared.
    assert not scheduler._tick_requested.is_set()
    scheduler.stop()
    await task


async def test_current_interval_seconds_exposes_resolved_value(
    db: aiosqlite.Connection, tmp_path,
):
    """The diagnostics view reads this to compute 'alive vs stale'."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await settings_repo.set(db, "playlist_refresh_interval_minutes", "5")
    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=AsyncMock(), min_sleep_seconds=0.05
    )
    assert await scheduler.current_interval_seconds() == 5 * 60
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scheduler.py -v -k "request_tick or current_interval_seconds"`
Expected: FAIL — `AttributeError: 'PlaylistScheduler' object has no attribute 'request_tick'`.

- [ ] **Step 3: Modify `app/scheduler.py`**

Edit `app/scheduler.py`:

**3a.** In `PlaylistScheduler.__init__`, after `self._stopped = asyncio.Event()`, add:

```python
        self._tick_requested = asyncio.Event()
```

**3b.** Replace the `_sleep_or_stop` method body. Find:

```python
    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), seconds)
```

Replace with:

```python
    async def _sleep_or_stop(self, seconds: float) -> None:
        """Wait up to ``seconds``, but return early on stop OR tick request.

        The tick-request event lets the diagnostics page's 'Jetzt prüfen'
        button trigger a refresh without waiting out the rest of the
        interval. The flag is cleared on wakeup so the next iteration
        sleeps normally — otherwise the loop would hot-spin.
        """
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

**3c.** Add two new public methods at the end of the class (after `run()`):

```python
    def request_tick(self) -> None:
        """Wake the scheduler so the next iteration runs immediately.

        Idempotent — a second request before the loop wakes is a no-op
        (asyncio.Event is set-once-until-cleared).
        """
        self._tick_requested.set()

    async def current_interval_seconds(self) -> float:
        """Public wrapper around the resolved interval.

        The diagnostics page calls this to compute the 'alive vs stale'
        threshold for the scheduler heartbeat (3× this value).
        """
        return await self._interval_seconds()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS (all existing tests still pass + 3 new).

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_scheduler.py
git commit -m "feat(diagnostics): scheduler request_tick + interval accessor"
```

---

## Task 6: Wire heartbeat into Worker + TtsWorker

**Files:**
- Modify: `app/worker.py`, `app/tts_worker.py`
- Test: `tests/test_worker.py` (extend), `tests/test_tts_worker.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worker.py`:

```python
async def test_worker_touches_heartbeat_each_iteration(
    db: aiosqlite.Connection, tmp_path,
):
    """The diagnostics page needs proof that the loop is alive even
    when the queue is empty. touch() must fire on every iteration."""
    from app.services.heartbeat import HeartbeatRegistry

    config = Config(data_dir=tmp_path)
    hb = HeartbeatRegistry()
    worker = Worker(
        db=db, config=config, process_video=AsyncMock(return_value=None),
        poll_interval=0.05, heartbeat=hb,
    )
    task = asyncio.create_task(worker.run())
    # Give it enough time to loop at least once on an empty queue.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if "summary_worker" in hb.snapshot():
            break
    worker.stop()
    await task

    snap = hb.snapshot()
    assert "summary_worker" in snap
    assert snap["summary_worker"].current_job_id is None  # idle


async def test_worker_heartbeat_records_current_job(
    db: aiosqlite.Connection, tmp_path,
):
    from app.services.heartbeat import HeartbeatRegistry

    config = Config(data_dir=tmp_path)
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    job_id = await jobs_repo.enqueue(db, "v1")

    seen_job_ids: list[int | None] = []
    hb = HeartbeatRegistry()

    async def slow_process(db_, config_, video_id, set_step):
        # Capture the heartbeat as it would be observed by the page.
        await asyncio.sleep(0.05)
        snap = hb.snapshot()
        if "summary_worker" in snap:
            seen_job_ids.append(snap["summary_worker"].current_job_id)

    worker = Worker(
        db=db, config=config, process_video=slow_process,
        poll_interval=0.05, heartbeat=hb,
    )
    task = asyncio.create_task(worker.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        j = await jobs_repo.get(db, job_id)
        if j and j.state is JobState.DONE:
            break
    worker.stop()
    await task

    assert job_id in seen_job_ids


def test_worker_poll_interval_seconds_property():
    """Expose poll_interval as a public read-only attribute so the
    diagnostics view doesn't reach into _poll_interval."""
    from app.services.heartbeat import HeartbeatRegistry
    worker = Worker(
        db=None, config=None, process_video=AsyncMock(),
        poll_interval=0.5, heartbeat=HeartbeatRegistry(),
    )
    assert worker.poll_interval_seconds == 0.5
```

Append to `tests/test_tts_worker.py`:

```python
async def test_tts_worker_touches_heartbeat(
    db: aiosqlite.Connection, tmp_path: Path,
) -> None:
    """Same contract as Worker: the loop must heartbeat each iteration."""
    from app.services.heartbeat import HeartbeatRegistry

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    hb = HeartbeatRegistry()

    async def fake_translate(*a, **kw): return ""
    async def fake_render(*a, **kw): return None
    async def fake_voice(*a, **kw): return tmp_path / "voice.onnx"

    worker = TtsWorker(
        db=db, config=cfg,
        translate=fake_translate,
        render_chunks_to_mp3=fake_render,
        ensure_voice=fake_voice,
        poll_interval=0.05,
        heartbeat=hb,
    )
    task = asyncio.create_task(worker.run())
    for _ in range(20):
        await asyncio.sleep(0.05)
        if "tts_worker" in hb.snapshot():
            break
    worker.stop()
    await task

    assert "tts_worker" in hb.snapshot()


def test_tts_worker_poll_interval_seconds_property():
    from app.services.heartbeat import HeartbeatRegistry
    async def _noop(*a, **kw): return None
    worker = TtsWorker(
        db=None, config=None,
        translate=_noop, render_chunks_to_mp3=_noop, ensure_voice=_noop,
        poll_interval=0.25, heartbeat=HeartbeatRegistry(),
    )
    assert worker.poll_interval_seconds == 0.25
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker.py tests/test_tts_worker.py -v -k "heartbeat or poll_interval_seconds"`
Expected: FAIL — `TypeError: Worker.__init__() got an unexpected keyword argument 'heartbeat'`.

- [ ] **Step 3: Modify `app/worker.py`**

Replace the body of the `Worker` class. Find:

```python
class Worker:
    def __init__(
        self,
        db: aiosqlite.Connection,
        config: Config,
        process_video: ProcessVideo,
        poll_interval: float = 1.0,
    ):
        self._db = db
        self._config = config
        self._process_video = process_video
        self._poll_interval = poll_interval
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            job = await jobs_repo.claim_next(self._db)
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
                continue
            try:
                job_id_capture = job.id

                async def set_step(step: str, _job_id: int = job_id_capture) -> None:  # noqa: B023
                    await jobs_repo.set_step(self._db, _job_id, step)

                await self._process_video(self._db, self._config, job.video_id, set_step)
                await jobs_repo.complete(self._db, job.id)
            except Exception as e:
                log.exception("job %s failed", job.id)
                await jobs_repo.fail(self._db, job.id, str(e))
```

Replace with:

```python
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
        while not self._stopped.is_set():
            job = await jobs_repo.claim_next(self._db)
            if job is None:
                # Heartbeat the idle loop so 'is the worker alive?' can
                # be answered even when there's nothing to do.
                self._touch()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
                continue
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
```

And add the import at the top of `app/worker.py` (after the existing imports):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.heartbeat import HeartbeatRegistry
```

- [ ] **Step 4: Modify `app/tts_worker.py`**

In `TtsWorker.__init__`, add the `heartbeat` keyword parameter and stash it:

Find the existing `__init__` and add `heartbeat: "HeartbeatRegistry | None" = None,` as the last keyword arg, plus `self._heartbeat = heartbeat` in the body.

Add the `TYPE_CHECKING` import at the top:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.heartbeat import HeartbeatRegistry
```

(There's already a `from typing import Any, Protocol` line — extend it: `from typing import Any, Protocol, TYPE_CHECKING`.)

Add the property and helper at the top of the class (after `__init__`, before `stop`):

```python
    @property
    def poll_interval_seconds(self) -> float:
        return self._poll_interval

    def _touch(
        self, *, current_job_id: int | None = None, current_step: str | None = None,
    ) -> None:
        if self._heartbeat is not None:
            self._heartbeat.touch(
                "tts_worker",
                current_job_id=current_job_id,
                current_step=current_step,
            )
```

Modify `run()`. Find:

```python
    async def run(self) -> None:
        while not self._stopped.is_set():
            job = await tts_jobs_repo.claim_next(self._db)
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopped.wait(), self._poll_interval
                    )
                continue
            try:
                audio_rel, duration, translated = await self._process(job)
```

Replace with:

```python
    async def run(self) -> None:
        while not self._stopped.is_set():
            job = await tts_jobs_repo.claim_next(self._db)
            if job is None:
                self._touch()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopped.wait(), self._poll_interval
                    )
                continue
            try:
                self._touch(current_job_id=job.id, current_step=job.step or "starting")
                audio_rel, duration, translated = await self._process(job)
```

Also update `_process` so step changes mirror into the heartbeat. Find the inner `set_step`:

```python
        async def set_step(step: str) -> None:
            await tts_jobs_repo.set_step(self._db, job_id, step)
```

Replace with:

```python
        async def set_step(step: str) -> None:
            await tts_jobs_repo.set_step(self._db, job_id, step)
            self._touch(current_job_id=job_id, current_step=step)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_worker.py tests/test_tts_worker.py -v`
Expected: PASS (all existing tests + new heartbeat / poll_interval cases).

- [ ] **Step 6: Commit**

```bash
git add app/worker.py app/tts_worker.py tests/test_worker.py tests/test_tts_worker.py
git commit -m "feat(diagnostics): wire heartbeat into Worker and TtsWorker"
```

---

## Task 7: Wire heartbeat into PlaylistScheduler

**Files:**
- Modify: `app/scheduler.py`
- Test: `tests/test_scheduler.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler.py`:

```python
async def test_scheduler_touches_heartbeat_each_iteration(
    db: aiosqlite.Connection, tmp_path,
):
    """The scheduler must heartbeat so the diagnostics page can tell
    it's alive even when no playlists are configured."""
    from app.services.heartbeat import HeartbeatRegistry

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await settings_repo.set(db, "playlist_refresh_interval_minutes", "0")
    hb = HeartbeatRegistry()

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=AsyncMock(),
        min_sleep_seconds=0.05, heartbeat=hb,
    )
    task = asyncio.create_task(scheduler.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if "scheduler" in hb.snapshot():
            break
    scheduler.stop()
    await task

    assert "scheduler" in hb.snapshot()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scheduler.py::test_scheduler_touches_heartbeat_each_iteration -v`
Expected: FAIL — `TypeError: PlaylistScheduler.__init__() got an unexpected keyword argument 'heartbeat'`.

- [ ] **Step 3: Modify `app/scheduler.py`**

**3a.** Add to the top of the file, with the other imports:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.heartbeat import HeartbeatRegistry
```

**3b.** In `PlaylistScheduler.__init__`, add the new keyword param. Find:

```python
    def __init__(
        self,
        db: aiosqlite.Connection,
        config: Config,
        sync_fn: SyncFn,
        *,
        min_sleep_seconds: float = 1.0,
    ) -> None:
```

Replace with:

```python
    def __init__(
        self,
        db: aiosqlite.Connection,
        config: Config,
        sync_fn: SyncFn,
        *,
        min_sleep_seconds: float = 1.0,
        heartbeat: "HeartbeatRegistry | None" = None,
    ) -> None:
```

And in the body, after `self._stopped = asyncio.Event()`:

```python
        self._heartbeat = heartbeat
```

**3c.** Add the `_touch` helper as a class method (next to `request_tick`):

```python
    def _touch(self, *, current_step: str | None = None) -> None:
        if self._heartbeat is not None:
            self._heartbeat.touch("scheduler", current_step=current_step)
```

**3d.** Call `_touch` at the top of each iteration inside `run()`. Find:

```python
    async def run(self) -> None:
        while not self._stopped.is_set():
            await self._sleep_or_stop(await self._interval_seconds())
            if self._stopped.is_set():
                return
            try:
                playlists = await playlists_repo.list_for_user(self._db, 1)
            except Exception:
                log.exception("scheduler: list_for_user failed")
                await self._record_tick()
                continue
            for playlist in playlists:
                if self._stopped.is_set():
                    return
                try:
                    await self._sync_fn(self._db, self._config, playlist.id)
                except Exception:
                    log.exception(
                        "scheduler: sync failed for playlist %s", playlist.id
                    )
            await self._record_tick()
```

Replace with:

```python
    async def run(self) -> None:
        while not self._stopped.is_set():
            self._touch(current_step="sleeping")
            await self._sleep_or_stop(await self._interval_seconds())
            if self._stopped.is_set():
                return
            self._touch(current_step="scanning")
            try:
                playlists = await playlists_repo.list_for_user(self._db, 1)
            except Exception:
                log.exception("scheduler: list_for_user failed")
                await self._record_tick()
                continue
            for playlist in playlists:
                if self._stopped.is_set():
                    return
                self._touch(current_step=f"syncing {playlist.id}")
                try:
                    await self._sync_fn(self._db, self._config, playlist.id)
                except Exception:
                    log.exception(
                        "scheduler: sync failed for playlist %s", playlist.id
                    )
            await self._record_tick()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS (all existing tests + new heartbeat case).

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_scheduler.py
git commit -m "feat(diagnostics): wire heartbeat into PlaylistScheduler"
```

---

## Task 8: Lifespan wiring — install log buffer + heartbeat registry

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_routes_diagnostics.py` with one bootstrap test that proves the wiring exists. (We'll grow this file further in Task 10.)

```python
from fastapi.testclient import TestClient

from app.main import create_app


def test_app_state_exposes_heartbeats_and_log_buffer(tmp_path, monkeypatch):
    """The diagnostics page reads these off app.state; lifespan must set them."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        # TestClient enters lifespan on __enter__.
        assert hasattr(app.state, "heartbeats")
        assert hasattr(app.state, "log_buffer")
        # And the log buffer is wired to the root logger.
        import logging
        root = logging.getLogger()
        assert app.state.log_buffer in root.handlers


def test_log_buffer_captures_emitted_lines(tmp_path, monkeypatch):
    """Smoke: any logger.info() should land in the ring buffer."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        import logging
        logging.getLogger("yt_summary.test").info("hello-from-test")
        lines = app.state.log_buffer.snapshot()
        assert any("hello-from-test" in line for line in lines)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routes_diagnostics.py -v`
Expected: FAIL — `AssertionError: hasattr(app.state, "heartbeats") is False`.

- [ ] **Step 3: Modify `app/main.py`**

In the `lifespan` function, modify the body. Find this block:

```python
    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    await init_schema(db)
    await jobs_repo.reset_orphaned_running(db)
```

Replace with:

```python
    from app.services.heartbeat import HeartbeatRegistry
    from app.services.log_buffer import RingBufferHandler

    config = Config.from_env()
    config.ensure_dirs()

    # Install the in-memory log tail handler on the root logger so the
    # diagnostics page can render recent worker output. Capacity is
    # bounded; the handler is GC'd with the app on shutdown.
    log_buffer = RingBufferHandler(capacity=500)
    log_buffer.setLevel(logging.INFO)
    log_buffer.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logging.getLogger().addHandler(log_buffer)

    # Process-wide heartbeat registry — workers write to it, the
    # diagnostics page reads it. Wiped on container restart by design.
    heartbeats = HeartbeatRegistry()

    db = await connect(config)
    await init_schema(db)
    await jobs_repo.reset_orphaned_running(db)
```

Update the three worker constructions to pass `heartbeat=heartbeats`. Find:

```python
    worker = Worker(db=db, config=config, process_video=process_video)
```

Replace with:

```python
    worker = Worker(
        db=db, config=config, process_video=process_video, heartbeat=heartbeats,
    )
```

Find:

```python
    tts_worker = TtsWorker(
        db=db,
        config=config,
        translate=translate,
        render_chunks_to_mp3=render_chunks_to_mp3,
        ensure_voice=_ensure_voice,
    )
```

Replace with:

```python
    tts_worker = TtsWorker(
        db=db,
        config=config,
        translate=translate,
        render_chunks_to_mp3=render_chunks_to_mp3,
        ensure_voice=_ensure_voice,
        heartbeat=heartbeats,
    )
```

Find:

```python
    scheduler = PlaylistScheduler(db=db, config=config, sync_fn=sync_playlist)
```

Replace with:

```python
    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=sync_playlist, heartbeat=heartbeats,
    )
```

Add the `app.state` exposures. Find:

```python
    app.state.config = config
    app.state.db = db
    app.state.worker = worker
    app.state.tts_worker = tts_worker
    app.state.scheduler = scheduler
```

Replace with:

```python
    app.state.config = config
    app.state.db = db
    app.state.worker = worker
    app.state.tts_worker = tts_worker
    app.state.scheduler = scheduler
    app.state.heartbeats = heartbeats
    app.state.log_buffer = log_buffer
```

In the `finally` block of the lifespan, remove the log handler so a process that creates many apps (test suite) doesn't leak handlers. After `await db.close()`:

```python
        logging.getLogger().removeHandler(log_buffer)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routes_diagnostics.py -v`
Expected: PASS.

Also re-run the full suite to confirm nothing regressed:

Run: `pytest -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_routes_diagnostics.py
git commit -m "feat(diagnostics): install log buffer + heartbeat registry in lifespan"
```

---

## Task 9: Diagnostics route — GET handler

**Files:**
- Modify: `app/routes/settings.py`
- Create: `app/templates/diagnostics.html`
- Test: `tests/test_routes_diagnostics.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_routes_diagnostics.py`:

```python
def test_get_diagnostics_renders_page(tmp_path, monkeypatch):
    """GET /settings/diagnostics returns 200 with all the section
    headers a fresh install should show."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200
    text = resp.text
    # Three worker rows by name.
    assert "summary_worker" in text or "Summary-Worker" in text
    assert "tts_worker" in text or "TTS-Worker" in text
    assert "scheduler" in text or "Scheduler" in text
    # Both queue cards.
    assert "Summary-Queue" in text or "summary queue" in text.lower()
    assert "TTS-Queue" in text or "tts queue" in text.lower()
    # Log tail block.
    assert "Log" in text


def test_get_diagnostics_with_no_data_does_not_crash(tmp_path, monkeypatch):
    """Empty queues, empty log buffer, no heartbeats → still renders."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200


def test_get_diagnostics_shows_queued_video_title(tmp_path, monkeypatch):
    """A pending job should appear in the 'Als Nächstes' list."""
    import asyncio
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            from app.repos import jobs as jobs_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vQ", url="u", title="MyQueuedVideo",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await jobs_repo.enqueue(app.state.db, "vQ")
        asyncio.get_event_loop().run_until_complete(seed())
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200
    assert "MyQueuedVideo" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_routes_diagnostics.py::test_get_diagnostics_renders_page -v`
Expected: FAIL — `404 Not Found` for `/settings/diagnostics`.

- [ ] **Step 3: Add the GET route in `app/routes/settings.py`**

Append at the end of `app/routes/settings.py`:

```python
@router.get("/settings/diagnostics", response_class=HTMLResponse)
async def diagnostics_page(
    request: Request,
    lines: int = 200,
    db: aiosqlite.Connection = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Render the diagnostics dashboard.

    Reads from app.state (heartbeats, log_buffer, the three workers
    for poll-interval thresholds), the DB (queue counts + recent
    jobs), and the persisted scheduler tick stamp. No mutation.
    """
    from datetime import UTC, datetime

    from app.repos import jobs as jobs_repo
    from app.repos import tts_jobs as tts_jobs_repo

    heartbeats = request.app.state.heartbeats.snapshot()
    log_buffer = request.app.state.log_buffer
    worker = request.app.state.worker
    tts_worker = request.app.state.tts_worker
    scheduler = request.app.state.scheduler

    # Resolve the per-worker stale threshold = 3 × poll_interval.
    # 3× gives the 1s pollers a ~3s budget and the 60-min scheduler
    # a ~3h budget — matches what the spec calls for.
    worker_thresholds_seconds = {
        "summary_worker": worker.poll_interval_seconds * 3,
        "tts_worker":     tts_worker.poll_interval_seconds * 3,
        "scheduler":      (await scheduler.current_interval_seconds()) * 3,
    }

    summary_counts = await jobs_repo.counts(db)
    summary_queue = await jobs_repo.list_queue(db, limit=10)
    summary_failed = await jobs_repo.list_recent_failed(db, limit=10)

    tts_counts = await tts_jobs_repo.counts(db)
    tts_queue = await tts_jobs_repo.list_queue(db, limit=10)
    tts_failed = await tts_jobs_repo.list_recent_failed(db, limit=10)

    scheduler_last_tick_at = await settings_repo.get(
        db, "scheduler_last_tick_at",
    )
    scheduled_playlists = await playlists_repo.list_for_user(db, 1)

    # Bound the requested line count to the buffer capacity.
    log_lines = log_buffer.snapshot(limit=max(1, min(lines, 500)))

    now_naive = datetime.now(UTC).replace(tzinfo=None)

    return templates.TemplateResponse(
        request,
        "diagnostics.html",
        {
            "current_user": current_user,
            "now": now_naive,
            "heartbeats": heartbeats,
            "worker_thresholds_seconds": worker_thresholds_seconds,
            "summary_counts": summary_counts,
            "summary_queue": summary_queue,
            "summary_failed": summary_failed,
            "tts_counts": tts_counts,
            "tts_queue": tts_queue,
            "tts_failed": tts_failed,
            "scheduler_last_tick_at": scheduler_last_tick_at,
            "scheduled_playlists": scheduled_playlists,
            "log_lines": log_lines,
            "lines_requested": lines,
        },
    )
```

- [ ] **Step 4: Create `app/templates/diagnostics.html`**

```html
{% extends "base.html" %}
{% block title %}Diagnose — yt-summary{% endblock %}
{% block content %}
<div class="settings-page">
  <header class="diagnostics-header">
    <h1>Diagnose</h1>
    <p class="settings-card-sub">
      Worker-Status, Queue-Inhalt und Log-Tail. Snapshot vom Aufruf —
      kein Auto-Refresh.
      <strong>Stand:</strong>
      <code>{{ now.isoformat(timespec='seconds') }}</code>
    </p>
    <p>
      <a href="/settings">← Zurück zu Settings</a>
      &middot;
      <a href="/settings/diagnostics">Aktualisieren</a>
    </p>
  </header>

  {# ── Workers ─────────────────────────────────────────────── #}
  <section class="settings-card">
    <header class="settings-card-head">
      <span class="settings-card-icon" aria-hidden="true">🫀</span>
      <div class="settings-card-head-text">
        <h2>Worker-Status</h2>
        <p class="settings-card-sub">
          Pro Hintergrund-Task: zuletzt beobachtete Aktivität.
        </p>
      </div>
    </header>
    <table class="playlist-refresh-table">
      <thead>
        <tr>
          <th>Worker</th>
          <th>Status</th>
          <th>Letztes Lebenszeichen</th>
          <th>Aktueller Schritt</th>
        </tr>
      </thead>
      <tbody>
        {% for name in ['summary_worker', 'tts_worker', 'scheduler'] %}
          {% set hb = heartbeats.get(name) %}
          {% set threshold = worker_thresholds_seconds[name] %}
          <tr>
            <td><code>{{ name }}</code></td>
            <td>
              {% if hb is none %}
                ⛔ never
              {% else %}
                {% set age = (now - hb.last_tick_at).total_seconds() %}
                {% if age < threshold %}
                  ✅ alive
                {% else %}
                  ⚠ stale ({{ age | int }}s)
                {% endif %}
              {% endif %}
            </td>
            <td>
              {% if hb %}
                <code>{{ hb.last_tick_at.isoformat(timespec='seconds') }}</code>
              {% else %}
                <em>—</em>
              {% endif %}
            </td>
            <td>
              {% if hb and hb.current_step %}
                {{ hb.current_step }}
                {% if hb.current_job_id %}
                  (job {{ hb.current_job_id }})
                {% endif %}
              {% else %}
                <em>idle</em>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </section>

  {# ── Helper macro: queue card ───────────────────────────── #}
  {% macro queue_card(label, counts, queue_rows, failed_rows,
                      retry_url, delete_url,
                      video_link_prefix='/v/') %}
  <section class="settings-card">
    <header class="settings-card-head">
      <span class="settings-card-icon" aria-hidden="true">📋</span>
      <div class="settings-card-head-text">
        <h2>{{ label }}</h2>
        <p class="settings-card-sub">
          queued: <strong>{{ counts.get('queued', counts.get('pending', 0)) }}</strong>
          · running: <strong>{{ counts['running'] }}</strong>
          · failed: <strong>{{ counts['failed'] }}</strong>
          · done (24h): <strong>{{ counts['done_24h'] }}</strong>
        </p>
      </div>
    </header>

    <h3>Als Nächstes</h3>
    {% if queue_rows %}
      <table class="playlist-refresh-table">
        <thead><tr><th>Video</th><th>Status</th></tr></thead>
        <tbody>
          {% for job, title in queue_rows %}
            <tr>
              <td>
                <a href="{{ video_link_prefix }}{{ job.video_id }}">{{ title }}</a>
              </td>
              <td>
                {% if job.state is defined %}
                  {{ job.state.value }}
                {% else %}
                  {{ job.status }}
                {% endif %}
                {% if job.step %} · {{ job.step }}{% endif %}
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="settings-card-sub"><em>Nichts in der Warteschlange.</em></p>
    {% endif %}

    <h3>Letzte Fehler</h3>
    {% if failed_rows %}
      <table class="playlist-refresh-table">
        <thead>
          <tr><th>Video</th><th>Fehler</th><th>Aktion</th></tr>
        </thead>
        <tbody>
          {% for job, title in failed_rows %}
            <tr>
              <td>
                <a href="{{ video_link_prefix }}{{ job.video_id }}">{{ title }}</a>
              </td>
              <td>
                {% set msg = job.error_message if job.error_message is defined else job.error %}
                <code>{{ (msg or '')[:200] }}</code>
              </td>
              <td>
                <form method="post" action="{{ retry_url }}/{{ job.id }}" style="display:inline">
                  <button type="submit" class="btn btn-secondary">Retry</button>
                </form>
                <form method="post" action="{{ delete_url }}/{{ job.id }}" style="display:inline"
                      onsubmit="return confirm('Diesen Job-Eintrag löschen?')">
                  <button type="submit" class="btn btn-secondary">Löschen</button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="settings-card-sub"><em>Keine Fehler.</em></p>
    {% endif %}
  </section>
  {% endmacro %}

  {{ queue_card('Summary-Queue', summary_counts, summary_queue, summary_failed,
                '/settings/diagnostics/retry-job',
                '/settings/diagnostics/delete-job') }}

  {{ queue_card('TTS-Queue', tts_counts, tts_queue, tts_failed,
                '/settings/diagnostics/retry-tts',
                '/settings/diagnostics/delete-tts') }}

  {# ── Scheduler ────────────────────────────────────────────── #}
  <section class="settings-card">
    <header class="settings-card-head">
      <span class="settings-card-icon" aria-hidden="true">⏰</span>
      <div class="settings-card-head-text">
        <h2>Scheduler</h2>
        <p class="settings-card-sub">
          Letzter Tick:
          {% if scheduler_last_tick_at %}
            <code>{{ scheduler_last_tick_at }}</code>
          {% else %}
            <em>noch keiner</em>
          {% endif %}
        </p>
      </div>
    </header>

    {% if scheduled_playlists %}
      <table class="playlist-refresh-table">
        <thead>
          <tr><th>Playlist</th><th>Zuletzt aktualisiert</th></tr>
        </thead>
        <tbody>
          {% for pl in scheduled_playlists %}
            <tr>
              <td>{{ pl.title or pl.id }}</td>
              <td>
                {% if pl.last_refreshed_at %}
                  {{ pl.last_refreshed_at.isoformat(timespec='seconds') }}
                {% else %}
                  <em>nie</em>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="settings-card-sub"><em>Keine Playlists konfiguriert.</em></p>
    {% endif %}

    <form method="post" action="/settings/diagnostics/tick-scheduler"
          style="margin-top: 0.5rem">
      <button type="submit" class="btn btn-primary">Jetzt prüfen</button>
    </form>
  </section>

  {# ── Log tail ────────────────────────────────────────────── #}
  <section class="settings-card">
    <header class="settings-card-head">
      <span class="settings-card-icon" aria-hidden="true">📜</span>
      <div class="settings-card-head-text">
        <h2>Log</h2>
        <p class="settings-card-sub">
          Letzte {{ log_lines | length }} Zeilen (max 500 im RAM, weg
          nach Container-Restart). Default 200 — mehr via
          <code>?lines=500</code>.
        </p>
      </div>
    </header>
    {% if log_lines %}
      <pre class="diagnostics-log">{% for line in log_lines %}{{ line }}
{% endfor %}</pre>
    {% else %}
      <p class="settings-card-sub"><em>Buffer ist leer.</em></p>
    {% endif %}
  </section>
</div>
{% endblock %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_routes_diagnostics.py -v`
Expected: PASS (3 new tests).

- [ ] **Step 6: Commit**

```bash
git add app/routes/settings.py app/templates/diagnostics.html tests/test_routes_diagnostics.py
git commit -m "feat(diagnostics): GET /settings/diagnostics + template"
```

---

## Task 10: Diagnostics POST actions — retry / delete / tick-scheduler

**Files:**
- Modify: `app/routes/settings.py`
- Test: `tests/test_routes_diagnostics.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routes_diagnostics.py`:

```python
def test_post_retry_job_resets_failed_to_pending(tmp_path, monkeypatch):
    import asyncio
    from app.models import JobState
    from app.repos import jobs as jobs_repo
    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            jid = await jobs_repo.enqueue(app.state.db, "v1")
            await jobs_repo.fail(app.state.db, jid, "oops")
            return jid
        jid = asyncio.get_event_loop().run_until_complete(seed())

        resp = client.post(
            f"/settings/diagnostics/retry-job/{jid}", follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            j = await jobs_repo.get(app.state.db, jid)
            return j
        job = asyncio.get_event_loop().run_until_complete(check())
        assert job is not None
        assert job.state is JobState.PENDING


def test_post_retry_job_unknown_id_returns_404(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/diagnostics/retry-job/99999", follow_redirects=False,
        )
    assert resp.status_code == 404


def test_post_delete_job_removes_failed_row(tmp_path, monkeypatch):
    import asyncio
    from app.repos import jobs as jobs_repo
    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            jid = await jobs_repo.enqueue(app.state.db, "v1")
            await jobs_repo.fail(app.state.db, jid, "boom")
            return jid
        jid = asyncio.get_event_loop().run_until_complete(seed())

        resp = client.post(
            f"/settings/diagnostics/delete-job/{jid}", follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            return await jobs_repo.get(app.state.db, jid)
        assert asyncio.get_event_loop().run_until_complete(check()) is None


def test_post_retry_tts_preserves_translated_text(tmp_path, monkeypatch):
    import asyncio
    from app.repos import tts_jobs as r
    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            j = await r.enqueue(
                app.state.db, "v1", "summary", "de", "v", "low",
            )
            await r.set_translated_text(app.state.db, j.id, "Hallo Welt")
            await r.fail(app.state.db, j.id, "render crashed")
            return j.id
        jid = asyncio.get_event_loop().run_until_complete(seed())

        resp = client.post(
            f"/settings/diagnostics/retry-tts/{jid}", follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            return await r.get(app.state.db, jid)
        fresh = asyncio.get_event_loop().run_until_complete(check())
        assert fresh is not None
        assert fresh.status == "queued"
        assert fresh.translated_text == "Hallo Welt"


def test_post_delete_tts_removes_row(tmp_path, monkeypatch):
    import asyncio
    from app.repos import tts_jobs as r
    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            j = await r.enqueue(
                app.state.db, "v1", "summary", "de", "v", "low",
            )
            await r.fail(app.state.db, j.id, "x")
            return j.id
        jid = asyncio.get_event_loop().run_until_complete(seed())

        resp = client.post(
            f"/settings/diagnostics/delete-tts/{jid}", follow_redirects=False,
        )
        assert resp.status_code == 303

        async def check():
            return await r.get(app.state.db, jid)
        assert asyncio.get_event_loop().run_until_complete(check()) is None


def test_post_tick_scheduler_returns_303(tmp_path, monkeypatch):
    """POST /tick-scheduler wakes the PlaylistScheduler.

    We assert the 303 redirect, not the _tick_requested flag, because
    the running scheduler may already have woken and cleared the flag
    by the time the assertion runs. The wake-up behaviour itself is
    proven by tests/test_scheduler.py::test_request_tick_wakes_a_long_sleep.
    """
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/diagnostics/tick-scheduler", follow_redirects=False,
        )
        assert resp.status_code == 303
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routes_diagnostics.py -v -k "post_"`
Expected: FAIL — `405 Method Not Allowed` for the POST endpoints.

- [ ] **Step 3: Add the POST routes in `app/routes/settings.py`**

Append at the end of `app/routes/settings.py`:

```python
@router.post("/settings/diagnostics/retry-job/{job_id}")
async def diagnostics_retry_job(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Reset a failed summary job back to 'pending'. 404 if the job
    is missing or not in 'failed' state."""
    from app.repos import jobs as jobs_repo
    affected = await jobs_repo.retry(db, job_id)
    if affected == 0:
        raise HTTPException(404, detail=f"No failed job {job_id}")
    return RedirectResponse("/settings/diagnostics", status_code=303)


@router.post("/settings/diagnostics/delete-job/{job_id}")
async def diagnostics_delete_job(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Delete a failed summary job row. The video is untouched. 404
    if the job is missing or not in 'failed' state."""
    from app.repos import jobs as jobs_repo
    affected = await jobs_repo.delete(db, job_id)
    if affected == 0:
        raise HTTPException(404, detail=f"No failed job {job_id}")
    return RedirectResponse("/settings/diagnostics", status_code=303)


@router.post("/settings/diagnostics/retry-tts/{job_id}")
async def diagnostics_retry_tts(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Reset a failed TTS job back to 'queued'. Preserves
    translated_text so we don't pay the LLM cost again."""
    from app.repos import tts_jobs as tts_jobs_repo
    affected = await tts_jobs_repo.retry(db, job_id)
    if affected == 0:
        raise HTTPException(404, detail=f"No failed tts_job {job_id}")
    return RedirectResponse("/settings/diagnostics", status_code=303)


@router.post("/settings/diagnostics/delete-tts/{job_id}")
async def diagnostics_delete_tts(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Delete a TTS job row. We deliberately do NOT clean up any MP3
    on disk — failed jobs typically have none; orphan cleanup is a
    separate concern."""
    from app.repos import tts_jobs as tts_jobs_repo
    # Reuse the existing repo function (predates this PR).
    cursor = await db.execute(
        "SELECT id FROM tts_jobs WHERE id=? AND status='failed'",
        (job_id,),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(404, detail=f"No failed tts_job {job_id}")
    await tts_jobs_repo.delete(db, job_id)
    return RedirectResponse("/settings/diagnostics", status_code=303)


@router.post("/settings/diagnostics/tick-scheduler")
async def diagnostics_tick_scheduler(request: Request):
    """Wake the PlaylistScheduler so its next iteration runs
    immediately, instead of waiting out the rest of the interval."""
    request.app.state.scheduler.request_tick()
    return RedirectResponse("/settings/diagnostics", status_code=303)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routes_diagnostics.py -v`
Expected: PASS (all existing + new POST tests).

- [ ] **Step 5: Commit**

```bash
git add app/routes/settings.py tests/test_routes_diagnostics.py
git commit -m "feat(diagnostics): POST retry/delete/tick-scheduler actions"
```

---

## Task 11: Settings page footer link

**Files:**
- Modify: `app/templates/settings.html`
- Test: `tests/test_routes_settings.py` (one new case)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_routes_settings.py`:

```python
def test_settings_page_links_to_diagnostics(tmp_path, monkeypatch):
    """The diagnostics subpage must be reachable from /settings."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "/settings/diagnostics" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_routes_settings.py::test_settings_page_links_to_diagnostics -v`
Expected: FAIL — `assert "/settings/diagnostics" in resp.text` is False.

- [ ] **Step 3: Add the link in `app/templates/settings.html`**

Find the closing `</div>` of `<div class="settings-page">` near the end of the file:

```html
    <ul class="settings-endpoint-list">
      <li>REST API: <code>/api/v1/</code> &middot; <a href="/docs" target="_blank">OpenAPI docs</a></li>
      <li>MCP server: <code>/mcp/sse</code></li>
    </ul>
  </section>
</div>
```

Insert a new section before the final `</div>`:

```html
    <ul class="settings-endpoint-list">
      <li>REST API: <code>/api/v1/</code> &middot; <a href="/docs" target="_blank">OpenAPI docs</a></li>
      <li>MCP server: <code>/mcp/sse</code></li>
    </ul>
  </section>

  <!-- ── Diagnostics link ──────────────────────────────────── -->
  <section class="settings-card">
    <header class="settings-card-head">
      <span class="settings-card-icon" aria-hidden="true">🔬</span>
      <div class="settings-card-head-text">
        <h2>Diagnose</h2>
        <p class="settings-card-sub">
          Worker-Status, Queue-Inhalt, Log-Tail — nützlich, wenn
          Videos lang queued bleiben oder die TTS nicht weiterkommt.
        </p>
      </div>
    </header>
    <p><a href="/settings/diagnostics">→ Zur Diagnose-Seite</a></p>
  </section>
</div>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_routes_settings.py::test_settings_page_links_to_diagnostics -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/settings.html tests/test_routes_settings.py
git commit -m "feat(diagnostics): link from settings page to diagnostics subpage"
```

---

## Task 12: Final verification

- [ ] **Step 1: Run the entire test suite**

Run: `pytest -q`
Expected: ALL PASS. (No skips that weren't skipping before.)

- [ ] **Step 2: Run ruff**

Run: `ruff check app tests`
Expected: clean, or only pre-existing warnings unrelated to this PR.

- [ ] **Step 3: Manual smoke (optional)**

```bash
python -m uvicorn app.main:app --port 8000
```

Visit `http://localhost:8000/settings/diagnostics`. Expected to see:
- 3 worker rows, with `summary_worker` / `tts_worker` showing `✅ alive` within a few seconds.
- `scheduler` may show `⛔ never` until its first cycle (depends on configured interval).
- Empty queue / log lists rendered cleanly.
- `← Zurück zu Settings`, `Aktualisieren`, and `Jetzt prüfen` links work.

Stop with Ctrl-C.

- [ ] **Step 4: Final commit / nothing-to-commit check**

```bash
git status
```

Expected: clean. If anything is staged but uncommitted, commit it with a meaningful message before declaring the feature done.

---

## Self-review notes (post-write)

- **Spec coverage:** Every Goal item in the spec maps to a Task (heartbeat → 1+6+7, log buffer → 2+8, queue counts/lists → 3+4, scheduler tick wakeup → 5, GET page → 9, POST actions → 10, settings link → 11). Non-goals are honoured: no auth, no DB schema change, no live update, no log filter.
- **Type consistency:** `Heartbeat.last_tick_at` is UTC-naive (`datetime.now(UTC).replace(tzinfo=None)`) everywhere it's compared to `datetime.now(UTC).replace(tzinfo=None)` in the route. The `_record_tick` helper in the scheduler writes ISO timestamps to settings with `.replace(microsecond=0).isoformat()`; the diagnostics view treats it as a string. No mismatch.
- **Naming:** `poll_interval_seconds` (property, both workers) vs `current_interval_seconds` (async method, scheduler) — different names because the scheduler's value is dynamic (reads settings each call) while the workers' is fixed at construction. Documented in Task 5 and Task 6.
- **Templates:** the `queue_card` macro handles both `Job` (has `.state`) and `TtsJob` (has `.status`) via `is defined`. The `error_message` (Job) vs `error` (TtsJob) split is handled the same way.
- **Tests:** the POST-tick-scheduler test sidesteps the racy event-cleared-after-wake check and asserts only the redirect, since the unit test for `request_tick()` itself is in `tests/test_scheduler.py` and already proves the wake-up works.
