from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from app.models import Job, JobState


def _row_to_job(row: aiosqlite.Row) -> Job:
    return Job(
        id=row["id"],
        video_id=row["video_id"],
        state=JobState(row["state"]),
        step=row["step"],
        error_message=row["error_message"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        llm_model_id=row["llm_model_id"],
        additional_prompt=row["additional_prompt"],
        attempts=row["attempts"],
        started_at=(
            datetime.fromisoformat(row["started_at"])
            if row["started_at"] else None
        ),
    )


async def enqueue(
    db: aiosqlite.Connection,
    video_id: str,
    *,
    llm_model_id: int | None = None,
    additional_prompt: str | None = None,
) -> int:
    cursor = await db.execute(
        """
        INSERT INTO jobs (video_id, state, llm_model_id, additional_prompt)
        VALUES (?, 'pending', ?, ?)
        """,
        (video_id, llm_model_id, additional_prompt),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def claim_next(db: aiosqlite.Connection) -> Job | None:
    """Atomically transition the oldest pending job to 'running' and
    return it. Single statement (no manual BEGIN/COMMIT) so we can't
    leave a transaction open if the call is interrupted mid-flight —
    the previous BEGIN IMMEDIATE / SELECT / UPDATE / COMMIT version
    could leave the connection in a half-open transaction if any step
    raised, which then crashed every subsequent call with
    'cannot start a transaction within a transaction'.

    Mirrors the pattern in app.repos.tts_jobs.claim_next.
    """
    cursor = await db.execute(
        """
        UPDATE jobs
        SET state='running', attempts=attempts+1,
            started_at=datetime('now'), updated_at=datetime('now')
        WHERE id = (
            SELECT id FROM jobs
            WHERE state='pending'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
        )
        RETURNING *
        """
    )
    row = await cursor.fetchone()
    await db.commit()
    return _row_to_job(row) if row else None


async def get(db: aiosqlite.Connection, job_id: int) -> Job | None:
    cursor = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def latest_for_video(db: aiosqlite.Connection, video_id: str) -> Job | None:
    cursor = await db.execute(
        "SELECT * FROM jobs WHERE video_id=? ORDER BY id DESC LIMIT 1",
        (video_id,),
    )
    row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def set_step(db: aiosqlite.Connection, job_id: int, step: str) -> None:
    await db.execute(
        "UPDATE jobs SET step=?, updated_at=datetime('now') WHERE id=?",
        (step, job_id),
    )
    await db.commit()


async def complete(db: aiosqlite.Connection, job_id: int) -> None:
    await db.execute(
        "UPDATE jobs SET state='done', updated_at=datetime('now') WHERE id=?",
        (job_id,),
    )
    await db.commit()


async def fail(db: aiosqlite.Connection, job_id: int, message: str) -> None:
    await db.execute(
        "UPDATE jobs SET state='failed', error_message=?, updated_at=datetime('now') WHERE id=?",
        (message, job_id),
    )
    await db.commit()


async def reset_orphaned_running(
    db: aiosqlite.Connection, max_attempts: int = 3
) -> int:
    """Called at startup. Jobs left running across a restart go back
    to pending — unless they have already been claimed `max_attempts`
    times, in which case they are marked failed.

    Without the cap a job that kills the container (Whisper on a Pi
    running out of RAM, a watchdog reboot) is requeued on every boot
    and takes the box down again. Returns the number of jobs that
    were given up on. A manual Retry resets the counter.
    """
    cursor = await db.execute(
        """
        UPDATE jobs
        SET state='failed',
            error_message='interrupted ' || attempts || ' times (worker '
                || 'crashed or container restarted mid-run); not requeued '
                || 'automatically — retry manually from Diagnostics',
            updated_at=datetime('now')
        WHERE state='running' AND attempts >= ?
        """,
        (max_attempts,),
    )
    n_failed = cursor.rowcount or 0
    await db.execute(
        "UPDATE jobs SET state='pending', updated_at=datetime('now') WHERE state='running'"
    )
    await db.commit()
    return n_failed


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
    # `SELECT SUM(...) FROM jobs` always returns exactly one row
    # (NULLs on an empty table). The `if row is None` branch is
    # unreachable in practice but guards against -O builds stripping
    # an `assert` and against future driver quirks.
    if row is None:
        return {"pending": 0, "running": 0, "failed": 0, "done_24h": 0}
    return {
        "pending": row["pending"] or 0,
        "running": row["running"] or 0,
        "failed": row["failed"] or 0,
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
) -> list[tuple[Job, str, bool]]:
    """Failed jobs, newest first, with video title and `video_done`.

    `video_done` is True when the video already has a summary — a
    common case where the failure is a stale leftover from an earlier
    attempt and a retry would just re-do work that already succeeded.
    The diagnostics page uses this to disable the Retry button.
    """
    cursor = await db.execute(
        """
        SELECT j.*,
               v.title AS video_title,
               (v.summary IS NOT NULL) AS video_done
        FROM jobs j
        LEFT JOIN videos v ON v.id = j.video_id
        WHERE j.state = 'failed'
        ORDER BY j.updated_at DESC, j.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        (
            _row_to_job(r),
            r["video_title"] or r["video_id"],
            bool(r["video_done"]),
        )
        for r in rows
    ]


async def retry(db: aiosqlite.Connection, job_id: int) -> int:
    """Reset a failed job back to ``pending`` so the worker picks it
    up. Returns the number of rows changed (0 => caller should 404).

    ``llm_model_id`` and ``additional_prompt`` are intentionally
    preserved — a retry should re-use the same per-run override the
    caller originally supplied (e.g. a Re-summarize with Claude +
    "be terse" should still hit Claude with "be terse" after retry).
    """
    cursor = await db.execute(
        """
        UPDATE jobs
        SET state='pending', error_message=NULL, attempts=0,
            updated_at=datetime('now')
        WHERE id=? AND state='failed'
        """,
        (job_id,),
    )
    await db.commit()
    return cursor.rowcount or 0


async def delete(db: aiosqlite.Connection, job_id: int) -> int:
    """Delete a failed job row. Returns the number of rows deleted
    (0 => caller should 404). The video row is untouched.
    """
    cursor = await db.execute(
        "DELETE FROM jobs WHERE id=? AND state='failed'",
        (job_id,),
    )
    await db.commit()
    return cursor.rowcount or 0


# ── Per-profile view (the /processing page and home strip) ───────
#
# Every helper below proves ownership inside the query itself via
# ``JOIN videos v ON v.id = j.video_id AND v.user_id = ?``. Routers
# never pre-check ownership and then mutate; the mutating statement
# carries the owner predicate so a stale read can't widen access.


@dataclass(frozen=True)
class QueueEntry:
    """One of the caller's active jobs plus its place in the global
    FIFO. ``position`` is 1-based across *all* profiles' active jobs
    (running first, then pending in claim order), so a user can see
    "you are #4" even when the three ahead belong to someone else.
    """
    job: Job
    title: str
    position: int


# Ordering shared by the position window and the row listing. Running
# first (there is at most one), then the exact order claim_next uses.
_ACTIVE_ORDER = "(state = 'running') DESC, created_at ASC, id ASC"


async def overview_for_user(
    db: aiosqlite.Connection, user_id: int,
) -> dict[str, int]:
    """Counts for the home strip.

    ``running`` / ``pending`` / ``failed`` are the caller's own jobs.
    ``others_ahead`` is the number of *other* profiles' active jobs
    that will be worked before the caller's first pending job — the
    honest answer to "why hasn't mine started?" without exposing
    their titles. 0 when the caller has nothing pending.
    """
    cursor = await db.execute(
        """
        WITH mine AS (
            SELECT j.id, j.state, j.created_at
            FROM jobs j
            JOIN videos v ON v.id = j.video_id AND v.user_id = ?
        ),
        first_pending AS (
            SELECT created_at, id FROM mine
            WHERE state = 'pending'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
        )
        SELECT
          (SELECT COUNT(*) FROM mine WHERE state = 'running') AS running,
          (SELECT COUNT(*) FROM mine WHERE state = 'pending') AS pending,
          (SELECT COUNT(*) FROM mine WHERE state = 'failed')  AS failed,
          (
            SELECT COUNT(*)
            FROM jobs j
            JOIN videos v ON v.id = j.video_id AND v.user_id != ?
            CROSS JOIN first_pending fp
            WHERE j.state = 'running'
               OR (j.state = 'pending'
                   AND (j.created_at, j.id) < (fp.created_at, fp.id))
          ) AS others_ahead
        """,
        (user_id, user_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return {"running": 0, "pending": 0, "failed": 0, "others_ahead": 0}
    return {
        "running": row["running"] or 0,
        "pending": row["pending"] or 0,
        "failed": row["failed"] or 0,
        "others_ahead": row["others_ahead"] or 0,
    }


async def list_active_for_user(
    db: aiosqlite.Connection, user_id: int, limit: int = 50,
) -> list[QueueEntry]:
    """The caller's running + pending jobs with their global queue
    position, in the order the worker will reach them."""
    cursor = await db.execute(
        f"""
        WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (ORDER BY {_ACTIVE_ORDER}) AS position
            FROM jobs
            WHERE state IN ('pending', 'running')
        )
        SELECT r.*, v.title AS video_title
        FROM ranked r
        JOIN videos v ON v.id = r.video_id AND v.user_id = ?
        ORDER BY r.position ASC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = await cursor.fetchall()
    return [
        QueueEntry(
            job=_row_to_job(r),
            title=r["video_title"] or r["video_id"],
            position=r["position"],
        )
        for r in rows
    ]


async def list_failed_for_user(
    db: aiosqlite.Connection, user_id: int, limit: int = 20,
) -> list[tuple[Job, str, bool]]:
    """Same shape as :func:`list_recent_failed`, scoped to one profile."""
    cursor = await db.execute(
        """
        SELECT j.*,
               v.title AS video_title,
               (v.summary IS NOT NULL) AS video_done
        FROM jobs j
        JOIN videos v ON v.id = j.video_id AND v.user_id = ?
        WHERE j.state = 'failed'
        ORDER BY j.updated_at DESC, j.id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = await cursor.fetchall()
    return [
        (_row_to_job(r), r["video_title"] or r["video_id"], bool(r["video_done"]))
        for r in rows
    ]


async def get_for_user(
    db: aiosqlite.Connection, job_id: int, user_id: int,
) -> Job | None:
    """Read one job only if its video belongs to ``user_id``. Used by
    routers to pick 404 vs 409 after an owner-scoped mutation touched
    zero rows — never as a pre-check that gates the mutation."""
    cursor = await db.execute(
        """
        SELECT j.* FROM jobs j
        JOIN videos v ON v.id = j.video_id AND v.user_id = ?
        WHERE j.id = ?
        """,
        (user_id, job_id),
    )
    row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def cancel_pending_for_user(
    db: aiosqlite.Connection, job_id: int, *, user_id: int,
) -> int:
    """Drop one of the caller's *pending* jobs. Running jobs are not
    cancellable (the pipeline has no cooperative abort point) and
    return 0, as do foreign or missing ids.

    Cancelling deletes the row rather than adding a 'cancelled' state:
    the ``jobs.state`` CHECK constraint would need a table rebuild for
    a new value, and a job-less video already renders as "no summary
    yet" with a Summarize button.
    """
    cursor = await db.execute(
        """
        DELETE FROM jobs
        WHERE id = ? AND state = 'pending'
          AND video_id IN (SELECT id FROM videos WHERE user_id = ?)
        """,
        (job_id, user_id),
    )
    await db.commit()
    return cursor.rowcount or 0


async def retry_for_user(
    db: aiosqlite.Connection, job_id: int, *, user_id: int,
) -> int:
    """Owner-scoped :func:`retry`. Same override-preserving semantics."""
    cursor = await db.execute(
        """
        UPDATE jobs
        SET state='pending', error_message=NULL, attempts=0,
            updated_at=datetime('now')
        WHERE id = ? AND state = 'failed'
          AND video_id IN (SELECT id FROM videos WHERE user_id = ?)
        """,
        (job_id, user_id),
    )
    await db.commit()
    return cursor.rowcount or 0


async def dismiss_failed_for_user(
    db: aiosqlite.Connection, job_id: int, *, user_id: int,
) -> int:
    """Owner-scoped :func:`delete` of a failed job row."""
    cursor = await db.execute(
        """
        DELETE FROM jobs
        WHERE id = ? AND state = 'failed'
          AND video_id IN (SELECT id FROM videos WHERE user_id = ?)
        """,
        (job_id, user_id),
    )
    await db.commit()
    return cursor.rowcount or 0
