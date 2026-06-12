# Digest Video Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Manual digests get a candidate-selection page (`/digest/new`) showing all highlight-bearing videos since the last digest (capped at 96 h), all pre-checked with select-all; the cron digest uses the same window automatically.

**Architecture:** A shared window helper (`compute_window`) replaces the fixed `period_hours` everywhere. The selection is persisted on the digest row (`selected_video_ids_json`, NULL = automatic) so the background job and history are self-describing. `_gather_pool` gains an optional ID restriction. New route `GET /digest/new` renders the checkbox list; `POST /digest/generate` now takes `video_ids[]` instead of `period_hours`.

**Tech Stack:** FastAPI + aiosqlite + Jinja2 + HTMX/Alpine. Tests: pytest + pytest-asyncio (auto mode) + FastAPI TestClient.

**Spec:** `docs/superpowers/specs/2026-06-12-digest-video-selection-design.md`

**Conventions used by this codebase (read before starting):**
- Repo tests (`tests/test_repos_*.py`) are `async def test_*(db)` using the `db` fixture from `tests/conftest.py`.
- Route tests build the app per-test: `monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path)); app = create_app()` and drive async setup via `asyncio.get_event_loop().run_until_complete(...)`.
- All timestamp comparisons in SQL go through `datetime(col) >= datetime(?)` to normalize the space-vs-T separator (SQLite default vs Python isoformat).
- Run tests with: `python -m pytest tests/<file>.py -v` from the repo root (activate `.venv` first if not already: `source .venv/bin/activate`).

---

### Task 1: Schema + model + repo: `selected_video_ids_json` column

**Files:**
- Modify: `app/db.py` (SCHEMA digests table ~line 242; `_run_migrations` — add near the feedback migration ~line 459)
- Modify: `app/models.py` (Digest dataclass ~line 219)
- Modify: `app/repos/digests.py` (`_row_to_digest`, `create_pending`)
- Test: `tests/test_db_migration_digest.py`, `tests/test_repos_digests.py`

- [ ] **Step 1: Write the failing migration test**

Append to `tests/test_db_migration_digest.py`:

```python
def test_digests_gains_selected_video_ids_column(tmp_path):
    """Legacy digests table (pre-selection) gains selected_video_ids_json
    via _ensure_column on init_schema."""
    import asyncio

    import aiosqlite

    from app.config import Config
    from app.db import connect, init_schema

    async def scenario():
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        conn = await aiosqlite.connect(cfg.db_path)
        await conn.execute(
            """
            CREATE TABLE digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                tldr TEXT,
                top_items_json TEXT,
                item_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL
                    CHECK(status IN ('pending','rendering','ready','failed')),
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.commit()
        await conn.close()
        conn = await connect(cfg)
        await init_schema(conn)
        cur = await conn.execute("PRAGMA table_info(digests)")
        cols = {row[1] for row in await cur.fetchall()}
        await conn.close()
        return cols

    cols = asyncio.get_event_loop().run_until_complete(scenario())
    assert "selected_video_ids_json" in cols
```

- [ ] **Step 2: Write the failing repo test**

Append to `tests/test_repos_digests.py` (the file already imports `digests_repo` and datetime helpers; add any missing `from datetime import UTC, datetime, timedelta` import):

```python
async def test_create_pending_persists_selection(db: aiosqlite.Connection):
    end = datetime.now(UTC).replace(microsecond=0)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=end - timedelta(hours=4), period_end=end,
        selected_video_ids_json='["a", "b"]',
    )
    assert d.selected_video_ids_json == '["a", "b"]'


async def test_create_pending_selection_defaults_to_null(
    db: aiosqlite.Connection,
):
    end = datetime.now(UTC).replace(microsecond=0)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=end - timedelta(hours=4), period_end=end,
    )
    assert d.selected_video_ids_json is None
```

- [ ] **Step 3: Run both tests to verify they fail**

Run: `python -m pytest tests/test_db_migration_digest.py::test_digests_gains_selected_video_ids_column tests/test_repos_digests.py::test_create_pending_persists_selection tests/test_repos_digests.py::test_create_pending_selection_defaults_to_null -v`
Expected: FAIL (`no such column: selected_video_ids_json` / `unexpected keyword argument` / `Digest has no attribute`)

- [ ] **Step 4: Implement schema + migration**

In `app/db.py`, SCHEMA digests table — add the column after `error TEXT,`:

```sql
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    tldr TEXT,
    top_items_json TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('pending','rendering','ready','failed')),
    error TEXT,
    -- JSON list of video ids the user hand-picked on /digest/new.
    -- NULL = automatic digest (cron or pre-feature rows): pool is
    -- everything in the window.
    selected_video_ids_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

In `_run_migrations`, after the feedback rebuild block (before the V7 call):

```python
    # Digest video selection: manual digests store the hand-picked
    # video ids; NULL = automatic (cron / legacy rows).
    if await _table_exists(conn, "digests"):
        await _ensure_column(
            conn, "digests", "selected_video_ids_json", "TEXT",
        )
```

- [ ] **Step 5: Implement model + repo**

In `app/models.py`, Digest dataclass — add after `error: str | None`:

```python
    # JSON-encoded list of hand-picked video ids (manual digests).
    # None = automatic digest: pool is everything in the window.
    selected_video_ids_json: str | None
```

NOTE: the field has no default, so it must be passed positionally-correct everywhere `Digest(...)` is constructed — `_row_to_digest` is the only constructor; `tests/test_models.py` may construct Digest directly, update it there too if so.

In `app/repos/digests.py`:

```python
def _row_to_digest(row: aiosqlite.Row) -> Digest:
    return Digest(
        id=row["id"],
        user_id=row["user_id"],
        period_start=datetime.fromisoformat(row["period_start"]),
        period_end=datetime.fromisoformat(row["period_end"]),
        tldr=row["tldr"],
        top_items_json=row["top_items_json"],
        item_count=row["item_count"],
        status=DigestStatus(row["status"]),
        error=row["error"],
        selected_video_ids_json=row["selected_video_ids_json"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def create_pending(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    period_start: datetime,
    period_end: datetime,
    selected_video_ids_json: str | None = None,
) -> Digest:
    cur = await db.execute(
        """
        INSERT INTO digests (
            user_id, period_start, period_end,
            selected_video_ids_json, status
        ) VALUES (?, ?, ?, ?, 'pending')
        """,
        (
            user_id, period_start.isoformat(), period_end.isoformat(),
            selected_video_ids_json,
        ),
    )
    await db.commit()
    digest_id = cur.lastrowid
    assert digest_id is not None
    fetched = await get(db, digest_id)
    assert fetched is not None
    return fetched
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_db_migration_digest.py tests/test_repos_digests.py tests/test_models.py -v`
Expected: PASS (all, including pre-existing tests in those files)

- [ ] **Step 7: Commit**

```bash
git add app/db.py app/models.py app/repos/digests.py tests/test_db_migration_digest.py tests/test_repos_digests.py tests/test_models.py
git commit -m "feat(digest): selected_video_ids_json column on digests"
```

---

### Task 2: Repo: `latest_period_end`

**Files:**
- Modify: `app/repos/digests.py`
- Test: `tests/test_repos_digests.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repos_digests.py`:

```python
async def test_latest_period_end_none_without_digests(
    db: aiosqlite.Connection,
):
    assert await digests_repo.latest_period_end(db, user_id=1) is None


async def test_latest_period_end_returns_newest(db: aiosqlite.Connection):
    now = datetime.now(UTC).replace(microsecond=0)
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=48),
        period_end=now - timedelta(hours=24),
    )
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=24),
        period_end=now - timedelta(hours=2),
    )
    got = await digests_repo.latest_period_end(db, user_id=1)
    assert got == now - timedelta(hours=2)


async def test_latest_period_end_ignores_failed(db: aiosqlite.Connection):
    now = datetime.now(UTC).replace(microsecond=0)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=24), period_end=now,
    )
    await digests_repo.mark_failed(db, digest_id=d.id, error="boom")
    assert await digests_repo.latest_period_end(db, user_id=1) is None


async def test_latest_period_end_scoped_by_user(db: aiosqlite.Connection):
    now = datetime.now(UTC).replace(microsecond=0)
    await db.execute("INSERT INTO users (id, name) VALUES (2, 'other')")
    await db.commit()
    await digests_repo.create_pending(
        db, user_id=2,
        period_start=now - timedelta(hours=24), period_end=now,
    )
    assert await digests_repo.latest_period_end(db, user_id=1) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_repos_digests.py -v -k latest_period_end`
Expected: FAIL with `AttributeError: module 'app.repos.digests' has no attribute 'latest_period_end'`

- [ ] **Step 3: Implement**

In `app/repos/digests.py`, change the datetime import at the top to `from datetime import UTC, datetime` and add after `list_for_user`:

```python
async def latest_period_end(
    db: aiosqlite.Connection, *, user_id: int,
) -> datetime | None:
    """period_end of the user's most recent non-failed digest.

    Failed digests are skipped — they summarized nothing, so their
    window must be retried by the next digest. Returned aware (UTC):
    rows written by this app store isoformat() of aware datetimes,
    but be defensive about naive legacy values.
    """
    cur = await db.execute(
        """
        SELECT period_end FROM digests
        WHERE user_id=? AND status IN ('pending','rendering','ready')
        ORDER BY datetime(period_end) DESC, id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    dt = datetime.fromisoformat(row["period_end"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_repos_digests.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/repos/digests.py tests/test_repos_digests.py
git commit -m "feat(digest): latest_period_end repo helper"
```

---

### Task 3: Service: `compute_window` (since last digest, capped 96 h)

**Files:**
- Modify: `app/services/digest.py`
- Test: `tests/test_services_digest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_services_digest.py` (ensure `from datetime import UTC, datetime, timedelta` and `from app.repos import digests as digests_repo` are imported at the top):

```python
async def test_compute_window_no_previous_digest_caps_96h(db):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    start, end = await digest_service.compute_window(db, user_id=1, now=now)
    assert end == now
    assert start == now - timedelta(hours=96)


async def test_compute_window_resumes_after_last_digest(db):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=30),
        period_end=now - timedelta(hours=6),
    )
    start, end = await digest_service.compute_window(db, user_id=1, now=now)
    assert start == now - timedelta(hours=6)
    assert end == now


async def test_compute_window_caps_stale_last_digest(db):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=300),
        period_end=now - timedelta(hours=200),
    )
    start, end = await digest_service.compute_window(db, user_id=1, now=now)
    assert start == now - timedelta(hours=96)


async def test_compute_window_ignores_failed_digests(db):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=10),
        period_end=now - timedelta(hours=2),
    )
    await digests_repo.mark_failed(db, digest_id=d.id, error="boom")
    start, end = await digest_service.compute_window(db, user_id=1, now=now)
    assert start == now - timedelta(hours=96)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_services_digest.py -v -k compute_window`
Expected: FAIL with `AttributeError: ... has no attribute 'compute_window'`

- [ ] **Step 3: Implement**

In `app/services/digest.py`, add below the module constants (`_TOP_N` etc.):

```python
# Hard cap on how far back a digest window may reach when the last
# digest is old or missing (spec: fixed 4 days, no setting).
WINDOW_CAP_HOURS = 96


async def compute_window(
    db: aiosqlite.Connection, *, user_id: int, now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Candidate window for the next digest of this Profile.

    Starts where the last non-failed digest ended, but never more than
    WINDOW_CAP_HOURS back. `now` is injectable for tests.
    """
    period_end = (now or datetime.now(UTC)).replace(microsecond=0)
    floor = period_end - timedelta(hours=WINDOW_CAP_HOURS)
    last_end = await digests_repo.latest_period_end(db, user_id=user_id)
    period_start = floor if last_end is None else max(last_end, floor)
    return period_start, period_end
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_services_digest.py -v -k compute_window`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/digest.py tests/test_services_digest.py
git commit -m "feat(digest): compute_window — since last digest, capped at 96h"
```

---

### Task 4: Service: pool ID restriction + candidate listing

**Files:**
- Modify: `app/services/digest.py` (`_gather_pool`; new `list_candidates`)
- Test: `tests/test_services_digest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_services_digest.py`. The seed helper inserts directly; the videos FTS triggers tolerate plain INSERTs:

```python
async def _seed_video(
    db, video_id, *, hours_ago=1, highlights='[{"text": "x", "rank": 1}]',
    user_id=1,
):
    created = (
        datetime.now(UTC) - timedelta(hours=hours_ago)
    ).replace(microsecond=0)
    await db.execute(
        "INSERT INTO videos (id, user_id, url, title, highlights_json,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (video_id, user_id, "u", f"Title {video_id}", highlights,
         created.isoformat()),
    )
    await db.commit()


async def test_gather_pool_restricts_to_video_ids(db):
    await _seed_video(db, "v1")
    await _seed_video(db, "v2")
    start = datetime.now(UTC) - timedelta(hours=96)
    pool = await digest_service._gather_pool(
        db, user_id=1, period_start=start, video_ids=["v1"],
    )
    assert [i["video_id"] for i in pool] == ["v1"]


async def test_gather_pool_empty_selection_returns_empty(db):
    await _seed_video(db, "v1")
    start = datetime.now(UTC) - timedelta(hours=96)
    pool = await digest_service._gather_pool(
        db, user_id=1, period_start=start, video_ids=[],
    )
    assert pool == []


async def test_gather_pool_without_ids_keeps_old_behavior(db):
    await _seed_video(db, "v1")
    await _seed_video(db, "v2")
    start = datetime.now(UTC) - timedelta(hours=96)
    pool = await digest_service._gather_pool(
        db, user_id=1, period_start=start,
    )
    assert {i["video_id"] for i in pool} == {"v1", "v2"}


async def test_list_candidates_splits_eligible_and_missing(db):
    await _seed_video(db, "v1", hours_ago=1)
    await _seed_video(db, "v2", hours_ago=2, highlights=None)
    await _seed_video(db, "v3", hours_ago=200)  # outside window
    start = datetime.now(UTC) - timedelta(hours=96)
    candidates, missing = await digest_service.list_candidates(
        db, user_id=1, period_start=start,
    )
    assert [c["id"] for c in candidates] == ["v1"]
    assert candidates[0]["title"] == "Title v1"
    assert candidates[0]["kind"] == "youtube"
    assert missing == 1


async def test_list_candidates_scoped_by_user(db):
    await db.execute("INSERT INTO users (id, name) VALUES (2, 'other')")
    await db.commit()
    await _seed_video(db, "v9", user_id=2)
    start = datetime.now(UTC) - timedelta(hours=96)
    candidates, missing = await digest_service.list_candidates(
        db, user_id=1, period_start=start,
    )
    assert candidates == []
    assert missing == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_services_digest.py -v -k "gather_pool or list_candidates"`
Expected: FAIL (`unexpected keyword argument 'video_ids'`, `no attribute 'list_candidates'`)

- [ ] **Step 3: Implement**

In `app/services/digest.py`, replace `_gather_pool` with:

```python
async def _gather_pool(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    period_start: datetime,
    video_ids: list[str] | None = None,
) -> list[dict]:
    """Return JSON-ready item dicts for the digest prompt.

    `video_ids` restricts the pool to a hand-picked selection (manual
    digests); None means everything in the window (automatic digests).
    The highlights gate applies either way.

    Uses SQLite's datetime() on both sides of the timestamp comparison
    to normalize the space-vs-T separator mismatch between SQLite's
    column-default datetime('now') and Python's datetime.isoformat().
    """
    if video_ids is not None and not video_ids:
        return []
    params: list = [user_id, period_start.isoformat()]
    id_clause = ""
    if video_ids is not None:
        placeholders = ",".join("?" for _ in video_ids)
        id_clause = f" AND id IN ({placeholders})"
        params.extend(video_ids)
    cur = await db.execute(
        f"""
        SELECT id, title, kind, url, highlights_json
        FROM videos
        WHERE user_id = ?
          AND datetime(created_at) >= datetime(?)
          AND highlights_json IS NOT NULL
          AND highlights_json != '[]'
          {id_clause}
        """,
        params,
    )
    rows = await cur.fetchall()
    items: list[dict] = []
    for r in rows:
        try:
            highlights = json.loads(r["highlights_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(highlights, list) or not highlights:
            continue
        items.append({
            "video_id": r["id"],
            "title": r["title"],
            "source_type": r["kind"],
            "url": r["url"],
            "highlights": highlights,
        })
    return items
```

Add `list_candidates` right after it:

```python
async def list_candidates(
    db: aiosqlite.Connection, *, user_id: int, period_start: datetime,
) -> tuple[list[dict], int]:
    """Candidates for the /digest/new selection page.

    Returns (eligible, missing_highlights_count): eligible items have
    highlights and can be picked; the count covers in-window videos
    without highlights, surfaced as a footnote so the user understands
    why they are absent.
    """
    cur = await db.execute(
        """
        SELECT id, title, kind, created_at,
               (highlights_json IS NOT NULL AND highlights_json != '[]')
                   AS has_highlights
        FROM videos
        WHERE user_id = ?
          AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) DESC
        """,
        (user_id, period_start.isoformat()),
    )
    rows = await cur.fetchall()
    eligible: list[dict] = []
    missing = 0
    for r in rows:
        if not r["has_highlights"]:
            missing += 1
            continue
        eligible.append({
            "id": r["id"],
            "title": r["title"],
            "kind": r["kind"],
            "created_at": r["created_at"],
        })
    return eligible, missing
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_services_digest.py -v`
Expected: PASS (including pre-existing tests — `_gather_pool`'s default behavior is unchanged)

- [ ] **Step 5: Commit**

```bash
git add app/services/digest.py tests/test_services_digest.py
git commit -m "feat(digest): pool ID restriction + candidate listing"
```

---

### Task 5: Service: `generate` / `run_for_existing_digest` use window + stored selection

**Files:**
- Modify: `app/services/digest.py` (`generate`, `run_for_existing_digest`, `_run`)
- Modify: `tests/test_services_digest.py`, `tests/test_e2e_digest.py` (drop `period_hours` at call sites)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_services_digest.py`:

```python
async def test_run_for_existing_digest_uses_stored_selection(
    db, monkeypatch,
):
    """The background job must honour the selection persisted on the
    digest row — not re-derive the pool from the whole window."""
    await _seed_video(db, "v1")
    await _seed_video(db, "v2")
    now = datetime.now(UTC).replace(microsecond=0)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=96), period_end=now,
        selected_video_ids_json='["v1"]',
    )

    seen_pools = []

    async def fake_llm(**kwargs):
        seen_pools.append(kwargs["payload"])
        return (
            '{"tldr": "ok", "top_items": '
            '[{"video_id": "v1", "rank": 1, "hook": "h", "reason": "r"}]}'
        )

    monkeypatch.setattr(digest_service, "_call_digest_llm", fake_llm)
    await _default_llm(db)  # helper already defined at the top of this file

    got = await digest_service.run_for_existing_digest(
        db, digest_id=d.id, user_id=1,
    )
    assert got.status.value == "ready"
    assert "v1" in seen_pools[0]
    assert "v2" not in seen_pools[0]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_services_digest.py::test_run_for_existing_digest_uses_stored_selection -v`
Expected: FAIL (`run_for_existing_digest() missing ... 'period_hours'` — the new call shape doesn't exist yet)

- [ ] **Step 3: Implement**

In `app/services/digest.py`, replace `generate`, `run_for_existing_digest`, and `_run`'s signature/pool call:

```python
async def generate(
    db: aiosqlite.Connection, *, user_id: int,
) -> Digest:
    """Build an automatic digest for the Profile.

    Window = since the last non-failed digest, capped at
    WINDOW_CAP_HOURS. Takes every candidate (no selection). Used by
    the cron sweep. The on-demand HTTP path creates the row in the
    handler so it can redirect to `/digest/<id>` immediately, then
    calls `run_for_existing_digest` to finish in the background.
    """
    period_start, period_end = await compute_window(db, user_id=user_id)

    d = await digests_repo.create_pending(
        db, user_id=user_id, period_start=period_start, period_end=period_end,
    )
    return await _run(
        db, digest_id=d.id, user_id=user_id,
        period_start=period_start, period_end=period_end,
        selected_video_ids=None,
    )


async def run_for_existing_digest(
    db: aiosqlite.Connection,
    *,
    digest_id: int,
    user_id: int,
) -> Digest:
    """Run the job for a digest row that's already been inserted as
    pending. Window and (optional) hand-picked selection come from the
    row itself — the route handler persisted both."""
    d = await digests_repo.get(db, digest_id)
    assert d is not None
    selected: list[str] | None = None
    if d.selected_video_ids_json:
        selected = json.loads(d.selected_video_ids_json)
    return await _run(
        db, digest_id=digest_id, user_id=user_id,
        period_start=d.period_start, period_end=d.period_end,
        selected_video_ids=selected,
    )


async def _run(
    db: aiosqlite.Connection,
    *,
    digest_id: int,
    user_id: int,
    period_start: datetime,
    period_end: datetime,
    selected_video_ids: list[str] | None,
) -> Digest:
    """Shared work loop. The row at `digest_id` must already exist."""
    await digests_repo.mark_rendering(db, digest_id=digest_id)

    period_hours = max(1, int((period_end - period_start).total_seconds() // 3600))
    pool = await _gather_pool(
        db, user_id=user_id, period_start=period_start,
        video_ids=selected_video_ids,
    )
    # ... rest of _run unchanged ...
```

(Only the signature, the docstring, and the `_gather_pool` call change in `_run`; everything from `if not pool:` down stays as-is.)

- [ ] **Step 4: Update existing call sites in tests**

In `tests/test_services_digest.py`: every `digest_service.generate(db, user_id=1, period_hours=24)` becomes `digest_service.generate(db, user_id=1)` (lines ~33-34, 63, 84, 101, 114, 137).
In `tests/test_e2e_digest.py` line ~76: same change.
CAUTION: any test that asserts on the empty-pool TL;DR text ("last 24 hours") must be relaxed to match the new 96 h window (e.g. assert on "quiet" instead of the hour count).

- [ ] **Step 5: Run the service + e2e digest tests**

Run: `python -m pytest tests/test_services_digest.py tests/test_e2e_digest.py -v`
Expected: PASS. (`tests/test_routes_digest.py` will FAIL now — routes still pass `period_hours`; that's Task 6/7.)

- [ ] **Step 6: Commit**

```bash
git add app/services/digest.py tests/test_services_digest.py tests/test_e2e_digest.py
git commit -m "feat(digest): generate/run use shared window + stored selection"
```

---

### Task 6: Route + template: `GET /digest/new`

**Files:**
- Modify: `app/routes/digest.py`
- Create: `app/templates/digest/new.html`
- Test: `tests/test_routes_digest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routes_digest.py`:

```python
def _seed_route_video(app, video_id, *, highlights=True, hours_ago=1):
    async def setup():
        created = (
            datetime.now(UTC) - timedelta(hours=hours_ago)
        ).replace(microsecond=0)
        await app.state.db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title,"
            " highlights_json, created_at) VALUES (?, 1, 'youtube', 'u',"
            " ?, ?, ?)",
            (
                video_id, f"Title {video_id}",
                '[{"text": "t", "rank": 1}]' if highlights else None,
                created.isoformat(),
            ),
        )
        await app.state.db.commit()
    asyncio.get_event_loop().run_until_complete(setup())


def test_get_digest_new_lists_candidates_prechecked(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_route_video(app, "v1")
        resp = client.get("/digest/new")
    assert resp.status_code == 200
    assert "Title v1" in resp.text
    assert 'name="video_ids"' in resp.text
    assert "checked" in resp.text
    assert 'action="/digest/generate"' in resp.text


def test_get_digest_new_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/digest/new")
    assert resp.status_code == 200
    assert 'name="video_ids"' not in resp.text
    assert "No new items" in resp.text


def test_get_digest_new_footnotes_missing_highlights(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_route_video(app, "v1")
        _seed_route_video(app, "v2", highlights=False)
        resp = client.get("/digest/new")
    assert resp.status_code == 200
    assert "Title v1" in resp.text
    assert "Title v2" not in resp.text
    assert "1 more item" in resp.text
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_routes_digest.py -v -k digest_new`
Expected: FAIL with 404 (route doesn't exist)

- [ ] **Step 3: Implement the route**

In `app/routes/digest.py`, add `from datetime import datetime` to the imports, extend the module docstring with the new endpoint, and add the handler ABOVE `digest_show` (FastAPI matches `/digest/{digest_id}` otherwise — `/digest/new` would 422 on int coercion):

```python
@router.get("/digest/new", response_class=HTMLResponse)
async def digest_new(
    request: Request,
    error: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    """Candidate-selection page for an on-demand digest. Window =
    since the last non-failed digest, capped at 96 h."""
    period_start, period_end = await digest_service.compute_window(
        db, user_id=user_id,
    )
    candidates, missing = await digest_service.list_candidates(
        db, user_id=user_id, period_start=period_start,
    )
    last_end = await digests_repo.latest_period_end(db, user_id=user_id)
    since_last = last_end is not None and last_end > (
        period_end - timedelta(hours=digest_service.WINDOW_CAP_HOURS)
    )
    return templates.TemplateResponse(
        request,
        "digest/new.html",
        {
            "candidates": candidates,
            "missing_highlights_count": missing,
            "period_start": period_start,
            "since_last_digest": since_last,
            "error": error,
        },
    )
```

(Also add `timedelta` to the datetime import: `from datetime import datetime, timedelta`.)

- [ ] **Step 4: Create the template**

Create `app/templates/digest/new.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>New digest</h1>
<p class="caption">
  {% if since_last_digest %}
    Covering items since your last digest
    ({{ period_start.strftime('%Y-%m-%d %H:%M') }} UTC).
  {% else %}
    Covering items from the last 4 days.
  {% endif %}
</p>

{% if error == 'no-selection' %}
  <p class="digest-new-error" role="alert">
    Pick at least one item to generate a digest.
  </p>
{% endif %}

{% if candidates %}
  <form method="post" action="/digest/generate"
        x-data="{ toggleAll(on) {
          this.$root.querySelectorAll('input[name=video_ids]')
            .forEach(c => c.checked = on)
        } }">
    <p class="digest-new-toolbar">
      <button type="button" class="btn btn-secondary"
              @click="toggleAll(true)">Select all</button>
      <button type="button" class="btn btn-secondary"
              @click="toggleAll(false)">Deselect all</button>
    </p>
    <ul class="digest-candidates">
      {% for c in candidates %}
        <li>
          <label>
            <input type="checkbox" name="video_ids"
                   value="{{ c.id }}" checked>
            <strong>{{ c.title }}</strong>
            <span class="caption">{{ c.kind }} · {{ c.created_at[:16] }}</span>
          </label>
        </li>
      {% endfor %}
    </ul>
    <button type="submit" class="btn btn-primary">Generate digest</button>
  </form>
{% else %}
  <p class="empty">
    No new items with highlights in this window — your queue is quiet.
  </p>
{% endif %}

{% if missing_highlights_count %}
  <p class="caption digest-new-footnote">
    {{ missing_highlights_count }} more item{{ '' if missing_highlights_count == 1 else 's' }}
    in this window {{ 'has' if missing_highlights_count == 1 else 'have' }} no highlights yet and can't be included.
  </p>
{% endif %}

<style>
  .digest-candidates { list-style: none; padding: 0; }
  .digest-candidates li { padding: 6px 0; border-bottom: 1px solid var(--hairline); }
  .digest-candidates label { display: flex; gap: 10px; align-items: baseline; cursor: pointer; }
  .digest-new-toolbar { display: flex; gap: 8px; }
  .digest-new-error { color: var(--danger, #b00020); }
  .digest-new-footnote { margin-top: 16px; }
</style>
{% endblock %}
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_routes_digest.py -v -k digest_new`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/routes/digest.py app/templates/digest/new.html tests/test_routes_digest.py
git commit -m "feat(digest): /digest/new candidate-selection page"
```

---

### Task 7: Route: `POST /digest/generate` takes the selection; update entry points

**Files:**
- Modify: `app/routes/digest.py` (`_enqueue_digest_job`, `digest_generate`)
- Modify: `app/templates/digest/list.html`, `app/templates/home.html`, `app/templates/digest/_body.html`
- Test: `tests/test_routes_digest.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_routes_digest.py`, REPLACE `test_post_digest_generate_redirects_to_new_digest` and `test_post_digest_generate_rejects_invalid_period_hours` with:

```python
def test_post_digest_generate_persists_selection_and_redirects(
    tmp_path, monkeypatch,
):
    """The handler validates the picked ids against the current
    candidate window, persists them on the digest row, and redirects
    (303) to /digest/<id>. Generation runs in background — neutralize
    it so no LLM call fires."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    from app.services import digest as digest_service

    async def noop(db, *, digest_id, user_id):
        return None
    monkeypatch.setattr(digest_service, "run_for_existing_digest", noop)

    with TestClient(app) as client:
        _seed_route_video(app, "v1")
        _seed_route_video(app, "v2")
        resp = client.post(
            "/digest/generate", data={"video_ids": ["v1"]},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/digest/")
    digest_id = int(location.rsplit("/", 1)[1])

    async def fetch():
        return await digests_repo.get(app.state.db, digest_id)
    d = asyncio.get_event_loop().run_until_complete(fetch())
    assert d is not None
    assert d.selected_video_ids_json == '["v1"]'


def test_post_digest_generate_filters_foreign_ids(tmp_path, monkeypatch):
    """Ids outside the candidate window (unknown, other profile, no
    highlights) are dropped server-side."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    from app.services import digest as digest_service

    async def noop(db, *, digest_id, user_id):
        return None
    monkeypatch.setattr(digest_service, "run_for_existing_digest", noop)

    with TestClient(app) as client:
        _seed_route_video(app, "v1")
        resp = client.post(
            "/digest/generate", data={"video_ids": ["v1", "evil"]},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    digest_id = int(resp.headers["location"].rsplit("/", 1)[1])

    async def fetch():
        return await digests_repo.get(app.state.db, digest_id)
    d = asyncio.get_event_loop().run_until_complete(fetch())
    assert d is not None
    assert d.selected_video_ids_json == '["v1"]'


def test_post_digest_generate_empty_selection_redirects_back(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/digest/generate", data={}, follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/digest/new?error=no-selection"
```

Also UPDATE `test_enqueue_marks_digest_failed_when_run_crashes` to the new signatures:

```python
def test_enqueue_marks_digest_failed_when_run_crashes(tmp_path, monkeypatch):
    """Safety net (mirrors the ask flow): a crashing background digest job
    must leave the row 'failed', not stuck 'pending'/'rendering'."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()

    from app.routes import digest as digest_route
    from app.services import digest as digest_service

    async def boom(db, *, digest_id, user_id):
        raise RuntimeError("simulated digest crash")
    monkeypatch.setattr(digest_service, "run_for_existing_digest", boom)

    with TestClient(app):
        async def scenario():
            end = datetime.now(UTC).replace(microsecond=0)
            d = await digest_route._enqueue_digest_job(
                app.state.db, user_id=1,
                period_start=end - timedelta(hours=96), period_end=end,
                video_ids=["v1"],
            )
            for t in list(digest_route._PENDING_JOBS):
                await t
            return await digests_repo.get(app.state.db, d.id)
        got = asyncio.get_event_loop().run_until_complete(scenario())
    assert got is not None
    assert got.status.value == "failed"
    assert "simulated digest crash" in (got.error or "")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_routes_digest.py -v`
Expected: the new/updated tests FAIL (old route still expects `period_hours`)

- [ ] **Step 3: Implement route changes**

In `app/routes/digest.py`, add `import json` if not present (it is, line 10), and replace `_enqueue_digest_job` and `digest_generate`:

```python
async def _enqueue_digest_job(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    period_start: datetime,
    period_end: datetime,
    video_ids: list[str] | None,
) -> Digest:
    """Spawn the digest job. Pre-creates the pending row in the
    foreground so the redirect to `/digest/<id>` has a valid target
    before the background generation finishes. `video_ids` is the
    hand-picked selection (None = automatic). Tests monkeypatch this
    whole function.
    """
    d = await digests_repo.create_pending(
        db, user_id=user_id, period_start=period_start,
        period_end=period_end,
        selected_video_ids_json=(
            json.dumps(video_ids) if video_ids is not None else None
        ),
    )

    async def _run(digest_id: int) -> None:
        try:
            await digest_service.run_for_existing_digest(
                db, digest_id=digest_id, user_id=user_id,
            )
        except Exception as e:
            log.exception(
                "on-demand digest job crashed for user %s", user_id,
            )
            # Safety net: don't leave the row stuck pending/rendering if
            # the job raised before marking its own failure.
            try:
                await digests_repo.mark_failed(
                    db, digest_id=digest_id,
                    error=f"{type(e).__name__}: {e}",
                )
            except Exception:
                log.exception(
                    "digest job: could not mark digest %s failed", digest_id,
                )

    task = asyncio.create_task(_run(d.id))
    _PENDING_JOBS.add(task)
    task.add_done_callback(_PENDING_JOBS.discard)
    return d
```

```python
@router.post("/digest/generate")
async def digest_generate(
    video_ids: list[str] = Form(default=[]),
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    period_start, period_end = await digest_service.compute_window(
        db, user_id=user_id,
    )
    candidates, _ = await digest_service.list_candidates(
        db, user_id=user_id, period_start=period_start,
    )
    allowed = {c["id"] for c in candidates}
    chosen = [v for v in video_ids if v in allowed]
    if not chosen:
        return RedirectResponse(
            url="/digest/new?error=no-selection", status_code=303,
        )
    d = await _enqueue_digest_job(
        db, user_id=user_id,
        period_start=period_start, period_end=period_end,
        video_ids=chosen,
    )
    return RedirectResponse(url=f"/digest/{d.id}", status_code=303)
```

Update the module docstring at the top of the file:

```python
"""Digest endpoints.

GET    /digest                  list view (latest + archive)
GET    /digest/new              candidate-selection page
GET    /digest/<id>             single digest view, HTMX-pollable
POST   /digest/generate         enqueue an on-demand digest job
"""
```

- [ ] **Step 4: Update the templates (entry points)**

`app/templates/digest/list.html` — replace the generate form and the empty-state button:

```html
{% extends "base.html" %}
{% block content %}
<h1>Daily digest</h1>
<p><a href="/digest/new" class="btn btn-primary">New digest</a></p>
<ul>
  {% for d in digests %}
    <li>
      <a href="/digest/{{ d.id }}">{{ d.created_at.strftime('%Y-%m-%d %H:%M') }}</a>
      — status: {{ d.status.value }}
      ({{ d.item_count }} items)
    </li>
  {% else %}
    <li>No digests yet. <a href="/digest/new">Generate the first one</a></li>
  {% endfor %}
</ul>
{% endblock %}
```

`app/templates/home.html` — replace the digest form (lines 65-79) with a link card, mirroring the playlist add card:

```html
  <a href="/digest/new" class="playlist-card playlist-card-add"
     title="Pick the items for a fresh digest">
    <div class="playlist-card-placeholder">+</div>
    <div class="playlist-card-body">
      <h4>
        {% if recent_digests %}New digest{% else %}Generate your first digest{% endif %}
      </h4>
      <p class="caption">
        A TL;DR + Top-10 of new items, shaped by your interest profile
      </p>
    </div>
  </a>
```

(The surrounding `<form ... class="digest-card-add-wrap">`, the hidden `period_hours` input, and the `<button>` wrapper are removed entirely.)

`app/templates/digest/_body.html` — the failed-state retry form (lines 17-20) becomes a link:

```html
{% elif digest.status.value == 'failed' %}
  <section class="digest-failed">
    <p>Digest failed: {{ digest.error or 'unknown error' }}</p>
    <a href="/digest/new" class="btn btn-primary">Retry</a>
  </section>
```

- [ ] **Step 5: Run the digest route tests + home/feedback tests**

Run: `python -m pytest tests/test_routes_digest.py tests/test_routes_home.py tests/test_routes_feedback.py -v`
Expected: PASS. If a home-route test asserted on the old digest `<form>`, update it to expect the `/digest/new` link instead.

- [ ] **Step 6: Commit**

```bash
git add app/routes/digest.py app/templates/digest/list.html app/templates/home.html app/templates/digest/_body.html tests/test_routes_digest.py tests/test_routes_home.py
git commit -m "feat(digest): generate takes hand-picked video_ids; entry points link /digest/new"
```

---

### Task 8: Scheduler uses the shared window

**Files:**
- Modify: `app/scheduler.py` (~line 328)
- Test: `tests/test_scheduler_digest.py`

- [ ] **Step 1: Update the test expectation first**

In `tests/test_scheduler_digest.py` line ~27, change:

```python
    fake_generate.assert_awaited_once_with(db, user_id=1, period_hours=24)
```

to:

```python
    fake_generate.assert_awaited_once_with(db, user_id=1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_scheduler_digest.py -v`
Expected: FAIL on the assertion (scheduler still passes `period_hours=24`)

- [ ] **Step 3: Implement**

In `app/scheduler.py` (~line 328):

```python
            await digest_service.generate(
                self._db, user_id=user_id,
            )
```

Also update the DigestScheduler class docstring's last line to: `call digest_service.generate (window = since the last digest, capped at 96 h).`

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_scheduler_digest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_scheduler_digest.py
git commit -m "feat(digest): cron digest uses since-last-digest window"
```

---

### Task 9: Full-suite verification

- [ ] **Step 1: Grep for leftovers**

Run: `grep -rn "period_hours" app/ tests/ --include="*.py" --include="*.html"`
Expected: NO matches in `app/` (the concept is gone from production code). Matches in tests must only be inside tests you already updated — if any remain, fix them.

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: PASS. Likely stragglers if not: `tests/test_e2e_digest.py` (call shape), `tests/test_routes_home.py` (home template), `tests/test_models.py` (Digest constructor). Fix forward.

- [ ] **Step 3: Commit any straggler fixes**

```bash
git add -A tests/
git commit -m "test(digest): align remaining tests with selection flow"
```

(Skip the commit if Step 2 needed no changes.)
