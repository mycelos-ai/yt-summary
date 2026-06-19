# Playlist Detail Ordering by YouTube Position Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** List videos on the playlist detail page in YouTube playlist order by persisting each link's yt-dlp position and sorting by it (NULLs last, legacy fallback).

**Architecture:** A nullable `position` column on `playlist_videos`, written for every entry on every sync via an upsert (new + existing links). `PlaylistEntry` gains a `position`; `_process_entries` passes it to `link_video`; `videos_for_playlist` sorts by position with an `added_at` fallback. Pre-existing links start NULL and self-heal on the next refresh.

**Tech Stack:** Python 3.11+, aiosqlite (SQLite), yt-dlp, pytest (asyncio_mode=auto).

## Global Constraints

- `playlist_videos` has `PRIMARY KEY (playlist_id, video_id)` → `ON CONFLICT(playlist_id, video_id)` upsert is valid.
- `link_video`'s contract stays "return True iff the link was NEWLY created". Because `ON CONFLICT DO UPDATE` makes `cursor.rowcount` positive on BOTH insert and update, newness MUST be determined by an explicit `SELECT` before the upsert — not by `rowcount`.
- `link_video` gains `position: int | None = None` as an OPTIONAL trailing param so existing 3-arg callers and tests keep working unchanged.
- A position-only update (existing link) must NOT increment `newly_linked`/`newly_enqueued` — no re-enqueue of summary jobs on refresh.
- Migration is additive + idempotent via the existing `_ensure_column` helper in `app/db.py`. Pre-existing rows get `position = NULL`.
- Sort: positioned rows first (ascending position), NULLs last, then `added_at DESC, video_id DESC`.
- Tests use `asyncio_mode = "auto"` (no `@pytest.mark.asyncio`). Run `pytest` from repo root.
- Scope: ONLY the playlist detail ordering. Do NOT touch home/library ordering or the yt-dlp capping issue.

---

### Task 1: DB column `playlist_videos.position`

**Files:**
- Modify: `app/db.py` — add `position INTEGER` to the `playlist_videos` CREATE TABLE in `SCHEMA` (lines ~98-103) and an `_ensure_column` call in `_run_migrations`
- Test: `tests/test_db_migration_playlist_position.py` (create)

**Interfaces:**
- Produces: `playlist_videos.position INTEGER` (nullable) column.

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_playlist_position.py` (mirrors `tests/test_db_migration_image_query.py`):

```python
import asyncio

import aiosqlite

from app.config import Config
from app.db import connect, init_schema


def test_playlist_videos_gains_position_column(tmp_path):
    async def scenario():
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        conn = await aiosqlite.connect(cfg.db_path)
        await conn.execute(
            """
            CREATE TABLE playlist_videos (
                playlist_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (playlist_id, video_id)
            )
            """
        )
        await conn.commit()
        await conn.close()
        conn = await connect(cfg)
        await init_schema(conn)
        cur = await conn.execute("PRAGMA table_info(playlist_videos)")
        cols = {row[1] for row in await cur.fetchall()}
        await conn.close()
        return cols

    cols = asyncio.get_event_loop().run_until_complete(scenario())
    assert "position" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db_migration_playlist_position.py -v`
Expected: FAIL — `assert 'position' in cols` is False.

- [ ] **Step 3: Add the column to SCHEMA and the migration**

In `app/db.py`, in the `playlist_videos` CREATE TABLE inside `SCHEMA`, add a `position` column (after `added_at`):

```python
CREATE TABLE IF NOT EXISTS playlist_videos (
    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(id),
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- 1-based position in the source YouTube playlist (yt-dlp order),
    -- written on every sync. NULL on pre-feature links until the next
    -- refresh backfills them. Drives the detail-page ordering.
    position INTEGER,
    PRIMARY KEY (playlist_id, video_id)
);
```

In `_run_migrations`, add a guarded `_ensure_column` (place it near the other table migrations, e.g. after the `digests` block around line 515):

```python
    if await _table_exists(conn, "playlist_videos"):
        await _ensure_column(conn, "playlist_videos", "position", "INTEGER")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db_migration_playlist_position.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db_migration_playlist_position.py
git commit -m "feat(playlist-order): add playlist_videos.position column"
```

---

### Task 2: `link_video` writes position via upsert

**Files:**
- Modify: `app/repos/playlists.py` — `link_video` (lines ~124-135)
- Test: `tests/test_repos_playlists.py` (extend)

**Interfaces:**
- Consumes: `playlist_videos.position` (Task 1).
- Produces: `link_video(db, playlist_id, video_id, position: int | None = None) -> bool` — returns True iff newly linked; on an existing link returns False BUT updates `position`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_repos_playlists.py` (it has `_make_playlist` / `_make_video` helpers):

```python
async def test_link_video_stores_position(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    assert await playlists_repo.link_video(db, "p1", "v1", position=3) is True
    cur = await db.execute(
        "SELECT position FROM playlist_videos WHERE playlist_id='p1' AND video_id='v1'"
    )
    assert (await cur.fetchone())[0] == 3


async def test_relink_updates_position_without_new_link(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    await playlists_repo.link_video(db, "p1", "v1", position=5)
    # Re-link with a new position: not "new", but position must update.
    assert await playlists_repo.link_video(db, "p1", "v1", position=2) is False
    cur = await db.execute(
        "SELECT position, COUNT(*) FROM playlist_videos "
        "WHERE playlist_id='p1' AND video_id='v1'"
    )
    row = await cur.fetchone()
    assert row[0] == 2          # position updated
    cur = await db.execute(
        "SELECT COUNT(*) FROM playlist_videos WHERE playlist_id='p1' AND video_id='v1'"
    )
    assert (await cur.fetchone())[0] == 1   # still exactly one row
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_repos_playlists.py -k "position or relink" -v`
Expected: FAIL — `link_video()` has no `position` kwarg / position not stored.

- [ ] **Step 3: Rewrite `link_video` as a position-aware upsert**

In `app/repos/playlists.py`, replace `link_video`:

```python
async def link_video(
    db: aiosqlite.Connection,
    playlist_id: str,
    video_id: str,
    position: int | None = None,
) -> bool:
    """Link a video to a playlist, storing its playlist position.

    Returns True if the link was newly created, False if it already
    existed. In BOTH cases `position` is written (insert or update), so a
    refresh re-numbers existing links. Newness is determined by an
    explicit existence check BEFORE the upsert, because ON CONFLICT DO
    UPDATE makes rowcount positive on updates too.
    """
    cur = await db.execute(
        "SELECT 1 FROM playlist_videos WHERE playlist_id=? AND video_id=?",
        (playlist_id, video_id),
    )
    is_new = await cur.fetchone() is None
    await db.execute(
        """
        INSERT INTO playlist_videos (playlist_id, video_id, position)
        VALUES (?, ?, ?)
        ON CONFLICT(playlist_id, video_id)
        DO UPDATE SET position = excluded.position
        """,
        (playlist_id, video_id, position),
    )
    await db.commit()
    return is_new
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_repos_playlists.py -k "position or relink" -v`
Expected: PASS.

- [ ] **Step 5: Run the full repo test file (no regression — existing 3-arg callers)**

Run: `pytest tests/test_repos_playlists.py -v`
Expected: PASS (all, including `test_link_video_returns_true_when_new` / `_false_when_already_linked` which call the 3-arg form).

- [ ] **Step 6: Commit**

```bash
git add app/repos/playlists.py tests/test_repos_playlists.py
git commit -m "feat(playlist-order): link_video upserts playlist position"
```

---

### Task 3: `videos_for_playlist` sorts by position

**Files:**
- Modify: `app/repos/playlists.py` — `videos_for_playlist` ORDER BY (line ~156)
- Test: `tests/test_repos_playlists.py` (extend)

**Interfaces:**
- Consumes: `link_video(..., position=...)` (Task 2).
- Produces: `videos_for_playlist` returns videos ordered by `position ASC` (NULLs last, then `added_at DESC, video_id DESC`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repos_playlists.py`:

```python
async def test_videos_for_playlist_orders_by_position(db: aiosqlite.Connection):
    await _make_playlist(db)
    for vid in ("a", "b", "c"):
        await _make_video(db, vid)
    # Link out of id-order, with explicit positions (1=top).
    await playlists_repo.link_video(db, "p1", "a", position=3)
    await playlists_repo.link_video(db, "p1", "b", position=1)
    await playlists_repo.link_video(db, "p1", "c", position=2)
    rows = await playlists_repo.videos_for_playlist(db, "p1")
    assert [v.id for v in rows] == ["b", "c", "a"]   # position order


async def test_videos_for_playlist_null_position_sorts_last(db: aiosqlite.Connection):
    await _make_playlist(db)
    for vid in ("pos", "nul"):
        await _make_video(db, vid)
    await playlists_repo.link_video(db, "p1", "nul")            # position NULL
    await playlists_repo.link_video(db, "p1", "pos", position=1)
    rows = await playlists_repo.videos_for_playlist(db, "p1")
    assert [v.id for v in rows] == ["pos", "nul"]   # positioned first, NULL last
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_repos_playlists.py -k "orders_by_position or null_position" -v`
Expected: FAIL — current ORDER BY is `added_at DESC, video_id DESC`, so ids come back in the wrong order.

(Note: `test_videos_for_playlist_orders_recent_first` at line ~91 links without positions, so both rows have NULL position and fall through to the `added_at DESC, video_id DESC` fallback — its existing assertion must still hold. Verify in Step 5.)

- [ ] **Step 3: Update the ORDER BY**

In `app/repos/playlists.py`, `videos_for_playlist`, change the ORDER BY (line ~156) to:

```sql
        ORDER BY pv.position IS NULL, pv.position ASC,
                 pv.added_at DESC, pv.video_id DESC
```

(Leave the SELECT / JOIN unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_repos_playlists.py -k "orders_by_position or null_position" -v`
Expected: PASS.

- [ ] **Step 5: Run the full repo test file (confirm legacy ordering test still holds)**

Run: `pytest tests/test_repos_playlists.py -v`
Expected: PASS (all, including `test_videos_for_playlist_orders_recent_first`).

- [ ] **Step 6: Commit**

```bash
git add app/repos/playlists.py tests/test_repos_playlists.py
git commit -m "feat(playlist-order): sort playlist detail by position, NULLs last"
```

---

### Task 4: `PlaylistEntry.position` from yt-dlp order

**Files:**
- Modify: `app/services/playlist.py` — `PlaylistEntry` dataclass (lines 9-15), `_entry_from_dict` (lines 59-66), `fetch_playlist` entry build (line 74)
- Test: `tests/test_services_playlist.py` (extend or create)

**Interfaces:**
- Produces: `PlaylistEntry.position: int` — 1-based position in the playlist, taken from yt-dlp's `playlist_index` when present, else the 1-based index over the filtered entry list.

- [ ] **Step 1: Write the failing test**

Check whether `tests/test_services_playlist.py` exists (`ls tests/ | grep playlist`). If it does, append; if not, create it. The fetch path calls yt-dlp, so test the pure parse helpers directly. Add:

```python
from app.services.playlist import PlaylistEntry, _entry_from_dict


def test_entry_from_dict_uses_playlist_index_when_present():
    raw = {"id": "vid1", "title": "T", "playlist_index": 7}
    entry = _entry_from_dict(raw, fallback_position=2)
    assert entry.position == 7


def test_entry_from_dict_falls_back_to_enumeration_index():
    raw = {"id": "vid2", "title": "T"}   # no playlist_index
    entry = _entry_from_dict(raw, fallback_position=4)
    assert entry.position == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_services_playlist.py -k position -v`
Expected: FAIL — `_entry_from_dict()` takes no `fallback_position` / `PlaylistEntry` has no `position`.

- [ ] **Step 3: Add `position` to the dataclass and parse it**

In `app/services/playlist.py`:

Add `position` to `PlaylistEntry`:

```python
@dataclass(frozen=True)
class PlaylistEntry:
    id: str
    title: str
    description: str
    thumbnail_url: str | None
    duration_seconds: int | None
    position: int
```

Update `_entry_from_dict` to accept a fallback and prefer yt-dlp's index:

```python
def _entry_from_dict(raw: dict[str, Any], *, fallback_position: int) -> PlaylistEntry:
    idx = raw.get("playlist_index")
    position = idx if isinstance(idx, int) else fallback_position
    return PlaylistEntry(
        id=raw["id"],
        title=raw.get("title") or "",
        description=raw.get("description") or "",
        thumbnail_url=_pick_thumbnail(raw),
        duration_seconds=raw.get("duration"),
        position=position,
    )
```

Update `fetch_playlist`'s entry build (line 74) to enumerate the FILTERED list 1-based:

```python
    entries = [
        _entry_from_dict(e, fallback_position=i)
        for i, e in enumerate((x for x in raw_entries if x), start=1)
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_services_playlist.py -k position -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/playlist.py tests/test_services_playlist.py
git commit -m "feat(playlist-order): capture yt-dlp position on PlaylistEntry"
```

---

### Task 5: `_process_entries` passes position through

**Files:**
- Modify: `app/services/playlist_sync.py` — `_process_entries` loop (lines ~40-63)
- Test: `tests/test_services_playlist_sync.py` (extend or create)

**Interfaces:**
- Consumes: `PlaylistEntry.position` (Task 4), `link_video(..., position=...)` (Task 2).
- Produces: after a sync, each processed link carries its yt-dlp position; `newly_linked`/`newly_enqueued` count only genuinely new links.

- [ ] **Step 1: Write the failing test**

Check `tests/` for an existing `test_services_playlist_sync.py` / `test_playlist_sync.py` to mirror its fixtures. The simplest robust test calls `_process_entries` directly with a fake entry list and asserts positions land in the DB. Add (in the existing sync-test file, or a new one):

```python
import aiosqlite

from app.config import Config
from app.repos import playlists as playlists_repo
from app.services.playlist import PlaylistEntry
from app.services.playlist_sync import _process_entries


def _entry(vid, pos):
    return PlaylistEntry(
        id=vid, title=vid, description="", thumbnail_url=None,
        duration_seconds=None, position=pos,
    )


async def test_process_entries_writes_positions(db: aiosqlite.Connection, tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="T",
        description="", thumbnail_path=None,
    )
    entries = [_entry("a", 1), _entry("b", 2)]
    result = await _process_entries(db, cfg, "p1", 1, entries)
    assert result.newly_linked == 2
    cur = await db.execute(
        "SELECT video_id, position FROM playlist_videos "
        "WHERE playlist_id='p1' ORDER BY position"
    )
    assert [tuple(r) async for r in cur] == [("a", 1), ("b", 2)]


async def test_process_entries_reprocess_updates_position_no_reenqueue(
    db: aiosqlite.Connection, tmp_path,
):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="T",
        description="", thumbnail_path=None,
    )
    await _process_entries(db, cfg, "p1", 1, [_entry("a", 1)])
    # Re-process the same video at a new position: no new link, no re-enqueue.
    result = await _process_entries(db, cfg, "p1", 1, [_entry("a", 5)])
    assert result.newly_linked == 0
    assert result.newly_enqueued == 0
    cur = await db.execute(
        "SELECT position FROM playlist_videos WHERE playlist_id='p1' AND video_id='a'"
    )
    assert (await cur.fetchone())[0] == 5
```

Note: `_process_entries` downloads a thumbnail for NEW videos (`download_thumbnail`). If that needs network/mocking in this test environment, mirror however the existing sync tests handle it (they likely monkeypatch `download_thumbnail`); match that pattern. If no sync test exists to mirror, monkeypatch `app.services.playlist_sync.download_thumbnail` to a no-op AsyncMock in these tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_services_playlist_sync.py -k "writes_positions or reprocess" -v`
Expected: FAIL — `_process_entries` calls `link_video` without `position`, so positions are NULL.

- [ ] **Step 3: Pass `entry.position` to `link_video`**

In `app/services/playlist_sync.py`, `_process_entries`, change the link call (line ~58):

```python
        if await playlists_repo.link_video(
            db, playlist_id, entry.id, position=entry.position
        ):
            newly_linked += 1
            assert existing is not None
            if existing.summary is None:
                await jobs_repo.enqueue(db, entry.id)
                newly_enqueued += 1
```

(The rest of the loop — existence check, thumbnail download, `upsert_metadata` — is unchanged. The `if link_video(...)` guard already ensures position-only updates don't increment the counters or enqueue.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_services_playlist_sync.py -k "writes_positions or reprocess" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/playlist_sync.py tests/test_services_playlist_sync.py
git commit -m "feat(playlist-order): sync writes yt-dlp position for every entry"
```

---

### Task 6: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: all pass. (If `test_services_model_info.py` / `test_services_embeddings_local.py` fail ONLY due to HuggingFace-cache/SOCKS sandbox restrictions, re-run those outside the sandbox to confirm — those failures are environmental, not feature regressions.)

- [ ] **Step 2: Confirm the end-to-end behavior (optional, user-driven)**

After deploy, refresh the playlist once (the refresh processes all entries, backfilling positions), then open the playlist detail page and confirm the newest YouTube videos now appear at the top.

---

## Self-Review

**Spec coverage:**
- `position` column, additive idempotent migration → Task 1. ✓
- `PlaylistEntry.position` from yt-dlp order → Task 4. ✓
- `link_video` upsert + position + newness-by-explicit-SELECT → Task 2. ✓
- `_process_entries` passes position; no re-enqueue on position-only update → Task 5. ✓
- `videos_for_playlist` ORDER BY position, NULLs last, fallback → Task 3. ✓
- Pre-existing links NULL, self-heal on next refresh → Tasks 1+2+5 together (NULL default + upsert refresh). ✓
- Testing: migration (1), link_video new/relink (2), ordering + NULL-last (3), parse position (4), sync writes + no-reenqueue (5). ✓

**Type/name consistency:** `position: int | None` on `link_video` (Tasks 2, 5); `PlaylistEntry.position: int` (Tasks 4, 5); `_entry_from_dict(raw, *, fallback_position: int)` consistent (Task 4). ORDER BY clause identical to the spec.

**Placeholder scan:** No TBD/TODO. Task 4 Step 1 and Task 5 Step 1 contain "check whether the test file exists / mirror the thumbnail-mock pattern" — these are concrete discovery instructions with a fallback spelled out (create the file; monkeypatch `download_thumbnail`), not deferred work.

**Ordering note:** Tasks 2 and 3 both edit `app/repos/playlists.py` but different functions (`link_video` vs `videos_for_playlist`); Tasks 4 and 5 both touch the sync path but different files. No conflict; sequential execution is clean.
