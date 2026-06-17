# Archive Items (instead of delete) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users archive a library item from its detail page; archived items vanish from Library, search, Ask, Digest, related, and tag views, but remain fully intact and restorable on a dedicated `/archive` page.

**Architecture:** Soft-delete via a new nullable `videos.archived_at` column (NULL = active). A single `archived_at IS NULL` filter is threaded through every "active list" query path. The detail page (`/v/{id}`) keeps loading archived items so they can be restored and so old digest/synthesis links keep working. New routes `POST /v/{id}/archive`, `POST /v/{id}/unarchive`, and `GET /archive`.

**Tech Stack:** FastAPI + aiosqlite + Jinja2/HTMX/Alpine. Tests: pytest + pytest-asyncio (auto mode) + FastAPI TestClient.

**Spec:** `docs/superpowers/specs/2026-06-12-archive-items-design.md`

**Conventions (read before starting):**
- Run tests with `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest <paths> -v` from the worktree root.
- **After every task, run `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .` — CI fails on lint (E501 line length 100). Wrap long lines.**
- pytest-asyncio auto mode: repo/service tests are `async def test_*(db)` using the `db` fixture (tests/conftest.py). Route tests build the app per-test: `monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path)); app = create_app()` and drive async setup via `asyncio.get_event_loop().run_until_complete(...)`.
- SQLite timestamp comparisons use `datetime(col)` to normalize the space-vs-T separator. `archived_at` is written with `datetime('now')` (space separator) and only ever tested for `IS NULL` / `IS NOT NULL`, so no normalization is needed for it.
- Seed a video in tests: `videos_repo.upsert_metadata(db, video_id=..., url=..., title=..., description=..., thumbnail_path=None, duration_seconds=None[, kind=VideoKind.EMAIL])`.

---

### Task 1: Schema + migration + model for `archived_at`

**Files:**
- Modify: `app/db.py` (SCHEMA videos block ~line 43; `_run_migrations` ~line 359)
- Modify: `app/models.py` (Video dataclass ~line 69)
- Modify: `app/repos/videos.py` (`_row_to_video` ~line 42)
- Test: `tests/test_db_migration_archive.py` (new)

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_archive.py`:

```python
import asyncio

import aiosqlite

from app.config import Config
from app.db import connect, init_schema


def test_videos_gains_archived_at_column(tmp_path):
    """A legacy videos table (pre-archive) gains archived_at on init."""

    async def scenario():
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        conn = await aiosqlite.connect(cfg.db_path)
        await conn.execute(
            """
            CREATE TABLE videos (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL DEFAULT 1,
                kind TEXT NOT NULL DEFAULT 'youtube',
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                thumbnail_path TEXT,
                duration_seconds INTEGER,
                transcript TEXT,
                transcript_source TEXT,
                summary TEXT,
                summary_model TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.commit()
        await conn.close()
        conn = await connect(cfg)
        await init_schema(conn)
        cur = await conn.execute("PRAGMA table_info(videos)")
        cols = {row[1] for row in await cur.fetchall()}
        await conn.close()
        return cols

    cols = asyncio.get_event_loop().run_until_complete(scenario())
    assert "archived_at" in cols
```

- [ ] **Step 2: Run it; verify it fails**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_db_migration_archive.py -v`
Expected: FAIL — `assert "archived_at" in cols` fails.

- [ ] **Step 3: Add the column to SCHEMA and the migration**

In `app/db.py`, the `videos` CREATE TABLE in SCHEMA — add the column right before `highlights_json TEXT`:

```sql
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Soft-delete timestamp. NULL = active; set = archived (hidden from
    -- all active views, restorable from /archive).
    archived_at TEXT,
    highlights_json TEXT
```

In `_run_migrations`, in the `if await _table_exists(conn, "videos"):` block, right after the existing `await _ensure_column(conn, "videos", "highlights_json", "TEXT")` line:

```python
        await _ensure_column(conn, "videos", "archived_at", "TEXT")
```

- [ ] **Step 4: Add the field to the model + row mapping**

In `app/models.py`, Video dataclass — add after `highlights_json: str | None = None`:

```python
    # Soft-delete timestamp (ISO string). None = active; set = archived.
    archived_at: str | None = None
```

In `app/repos/videos.py`, `_row_to_video` — the function uses `row["col"]` with try/except fallbacks for newer columns. Add a fallback read before the `return Video(...)` (alongside the other `try/except` blocks, e.g. after the `transcript_language` block ~line 41):

```python
    try:
        archived_at = row["archived_at"]
    except (IndexError, KeyError):
        archived_at = None
```

Then add to the `Video(...)` constructor call (after `highlights_json=...` if present, else at the end of the kwargs):

```python
        archived_at=archived_at,
```

NOTE: check whether `_row_to_video` currently passes `highlights_json` — at the time of writing it does NOT (the dataclass defaults it). Just append `archived_at=archived_at,` as the last kwarg; ordering among keyword args is irrelevant.

- [ ] **Step 5: Run tests**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_db_migration_archive.py tests/test_repos_videos.py tests/test_models.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 6: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/db.py app/models.py app/repos/videos.py tests/test_db_migration_archive.py
git commit -m "feat(archive): archived_at column on videos"
```

---

### Task 2: Repo — `set_archived`, `list_archived`, `count_archived`, and active-list filter

**Files:**
- Modify: `app/repos/videos.py` (`list_recent`, `search_fts`, `search` user-intersection; new functions)
- Test: `tests/test_repos_videos.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repos_videos.py` (it already imports `videos_repo`; add `from app.models import VideoKind` if missing). Seed helper inline:

```python
async def _seed(db, vid, *, user_id=1):
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title=f"T {vid}", description="",
        thumbnail_path=None, duration_seconds=None, user_id=user_id,
    )


async def test_set_archived_roundtrip(db):
    await _seed(db, "v1")
    ok = await videos_repo.set_archived(db, "v1", user_id=1, archived=True)
    assert ok is True
    v = await videos_repo.get(db, "v1")
    assert v.archived_at is not None
    ok = await videos_repo.set_archived(db, "v1", user_id=1, archived=False)
    assert ok is True
    v = await videos_repo.get(db, "v1")
    assert v.archived_at is None


async def test_set_archived_foreign_profile_returns_false(db):
    await db.execute("INSERT INTO users (id, name) VALUES (2, 'o')")
    await db.commit()
    await _seed(db, "v1", user_id=2)
    ok = await videos_repo.set_archived(db, "v1", user_id=1, archived=True)
    assert ok is False
    v = await videos_repo.get(db, "v1")
    assert v.archived_at is None


async def test_list_recent_excludes_archived(db):
    await _seed(db, "v1")
    await _seed(db, "v2")
    await videos_repo.set_archived(db, "v2", user_id=1, archived=True)
    rows = await videos_repo.list_recent(db, user_id=1)
    assert [v.id for v in rows] == ["v1"]


async def test_list_archived_returns_only_archived(db):
    await _seed(db, "v1")
    await _seed(db, "v2")
    await videos_repo.set_archived(db, "v2", user_id=1, archived=True)
    rows = await videos_repo.list_archived(db, user_id=1)
    assert [v.id for v in rows] == ["v2"]


async def test_count_archived(db):
    await _seed(db, "v1")
    await _seed(db, "v2")
    await videos_repo.set_archived(db, "v2", user_id=1, archived=True)
    assert await videos_repo.count_archived(db, user_id=1) == 1


async def test_search_fts_excludes_archived(db):
    await _seed(db, "v1")
    await videos_repo.set_archived(db, "v1", user_id=1, archived=True)
    ids = await videos_repo.search_fts(db, "T", user_id=1)
    assert "v1" not in ids
```

- [ ] **Step 2: Run; verify failures**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_repos_videos.py -v -k "archived"`
Expected: FAIL (`no attribute 'set_archived'` etc., and the list/search tests fail because archived rows still appear).

- [ ] **Step 3: Implement the new functions**

In `app/repos/videos.py`, add after `get` (~line 234):

```python
async def set_archived(
    db: aiosqlite.Connection, video_id: str, *, user_id: int, archived: bool,
) -> bool:
    """Archive or restore a video. Returns False when the video does
    not exist or belongs to another profile (caller answers 404)."""
    value = "datetime('now')" if archived else "NULL"
    cur = await db.execute(
        f"UPDATE videos SET archived_at={value}, updated_at=datetime('now') "
        "WHERE id=? AND user_id=?",
        (video_id, user_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def list_archived(
    db: aiosqlite.Connection, *, user_id: int, limit: int = 100, offset: int = 0,
) -> list[Video]:
    cur = await db.execute(
        "SELECT * FROM videos WHERE user_id=? AND archived_at IS NOT NULL "
        "ORDER BY datetime(archived_at) DESC, id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    )
    return [_row_to_video(r) for r in await cur.fetchall()]


async def count_archived(db: aiosqlite.Connection, *, user_id: int) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) FROM videos WHERE user_id=? AND archived_at IS NOT NULL",
        (user_id,),
    )
    row = await cur.fetchone()
    return row[0] if row else 0
```

- [ ] **Step 4: Thread the active filter through list/search**

In `list_recent` (~line 274): add `AND archived_at IS NULL` to BOTH branches. The tag branch becomes:

```python
    if tag:
        cursor = await db.execute(
            "SELECT * FROM videos WHERE user_id = ? AND archived_at IS NULL"
            + _TAG_FILTER_SQL
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, tag, limit, offset),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM videos WHERE user_id = ? AND archived_at IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
```

In `search_fts` (~line 305): add `AND v.archived_at IS NULL` to both queries' WHERE (the JOIN already aliases videos as `v`):

```python
    # tag branch — add after "AND v.user_id = ?":
            WHERE videos_fts MATCH ?
              AND v.user_id = ?
              AND v.archived_at IS NULL
              AND EXISTS (
    # non-tag branch — add after "AND v.user_id = ?":
            WHERE videos_fts MATCH ? AND v.user_id = ?
              AND v.archived_at IS NULL
            ORDER BY rank
```

In `search` (~line 397): the vector path intersects vector_ids against the user's video set. Add the archived filter there so archived items don't survive the vector path:

```python
            user_cursor = await db.execute(
                f"SELECT id FROM videos WHERE user_id = ? "
                f"AND archived_at IS NULL "
                f"AND id IN ({placeholders})",
                (user_id, *vector_ids),
            )
```

- [ ] **Step 5: Run tests**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_repos_videos.py -v`
Expected: PASS (all).

- [ ] **Step 6: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/repos/videos.py tests/test_repos_videos.py
git commit -m "feat(archive): repo archive/restore + active-list filter"
```

---

### Task 3: Filter archived out of Digest, Ask, and Related

**Files:**
- Modify: `app/services/digest.py` (`_gather_pool`, `list_candidates`)
- Modify: `app/services/related.py` (related id query)
- Test: `tests/test_services_digest.py`, `tests/test_services_related.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_services_digest.py` (helpers `_seed_video` exist there from the digest work; if `_seed_video` supports a `highlights` kwarg, use it — it inserts a video with highlights and created_at):

```python
async def test_gather_pool_excludes_archived(db):
    await _seed_video(db, "v1")
    await db.execute(
        "UPDATE videos SET archived_at=datetime('now') WHERE id='v1'"
    )
    await db.commit()
    start = datetime.now(UTC) - timedelta(hours=96)
    pool = await digest_service._gather_pool(
        db, user_id=1, period_start=start,
    )
    assert pool == []


async def test_list_candidates_excludes_archived(db):
    await _seed_video(db, "v1")
    await db.execute(
        "UPDATE videos SET archived_at=datetime('now') WHERE id='v1'"
    )
    await db.commit()
    start = datetime.now(UTC) - timedelta(hours=96)
    candidates, missing = await digest_service.list_candidates(
        db, user_id=1, period_start=start,
    )
    assert candidates == []
    assert missing == 0
```

For related — first read `app/services/related.py` to find the exact query function name and signature, then append a matching test to `tests/test_services_related.py` asserting an archived neighbour is excluded. (The related service queries candidate ids; add `archived_at IS NULL` wherever it selects from `videos`.) If related selects purely from the `video_embeddings` virtual table and then loads via `videos_repo.get_many`, add the filter by switching the final hydration to skip archived: filter the id list against a `SELECT id FROM videos WHERE id IN (...) AND archived_at IS NULL` set, mirroring the `search` vector-path intersection.

- [ ] **Step 2: Run; verify failures**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_digest.py -v -k "archived"`
Expected: FAIL (archived rows still in pool/candidates).

- [ ] **Step 3: Implement digest filters**

In `app/services/digest.py`, `_gather_pool` — add `AND archived_at IS NULL` to the WHERE:

```python
        WHERE user_id = ?
          AND datetime(created_at) >= datetime(?)
          AND archived_at IS NULL
          AND highlights_json IS NOT NULL
          AND highlights_json != '[]'
```

(If Task from the digest feature added a `video_ids` restriction with `{id_clause}`, keep that clause where it is — just insert the `archived_at IS NULL` line into the static WHERE.)

In `list_candidates` — add `AND archived_at IS NULL`:

```python
        FROM videos
        WHERE user_id = ?
          AND datetime(created_at) >= datetime(?)
          AND archived_at IS NULL
        ORDER BY datetime(created_at) DESC
```

- [ ] **Step 4: Implement related filter**

Apply the archived filter in `app/services/related.py` as determined in Step 1 (either in its SQL or via an id-set intersection before hydration).

- [ ] **Step 5: Run tests**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_digest.py tests/test_services_related.py tests/test_services_ask.py -v`
Expected: PASS. (Ask uses `videos_repo.search`, already filtered in Task 2 — its tests should stay green; if `tests/test_services_ask.py` has no archived test, that's fine, just confirm no regressions.)

- [ ] **Step 6: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/services/digest.py app/services/related.py tests/test_services_digest.py tests/test_services_related.py
git commit -m "feat(archive): exclude archived items from digest and related"
```

---

### Task 4: Routes — archive / unarchive / `/archive` page

**Files:**
- Modify: `app/routes/videos.py` (two POST handlers)
- Modify: `app/routes/home.py` (GET `/archive`)
- Create: `app/templates/archive.html`
- Test: `tests/test_routes_videos.py`, `tests/test_routes_home.py`

- [ ] **Step 1: Write the failing route tests**

Append to `tests/test_routes_videos.py`:

```python
def test_archive_then_unarchive(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())

        r1 = client.post("/v/v1/archive", follow_redirects=False)
        assert r1.status_code == 303
        assert r1.headers["location"] == "/"

        async def fetch():
            from app.repos import videos as videos_repo
            return await videos_repo.get(app.state.db, "v1")
        v = asyncio.get_event_loop().run_until_complete(fetch())
        assert v.archived_at is not None

        r2 = client.post("/v/v1/unarchive", follow_redirects=False)
        assert r2.status_code == 303
        assert r2.headers["location"] == "/v/v1"
        v = asyncio.get_event_loop().run_until_complete(fetch())
        assert v.archived_at is None


def test_archive_404_for_foreign_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await app.state.db.execute(
                "INSERT INTO users (id, name) VALUES (2, 'o')"
            )
            await app.state.db.commit()
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v2", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
                user_id=2,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        r = client.post("/v/v2/archive", follow_redirects=False)
    assert r.status_code == 404


def test_archived_item_hidden_from_home(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="HomeTitle",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_archived(
                app.state.db, "v1", user_id=1, archived=True,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert "HomeTitle" not in resp.text
```

Append to `tests/test_routes_home.py`:

```python
def test_archive_page_lists_archived(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="ArchivedTitle",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_archived(
                app.state.db, "v1", user_id=1, archived=True,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/archive")
    assert resp.status_code == 200
    assert "ArchivedTitle" in resp.text


def test_archive_page_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/archive")
    assert resp.status_code == 200
    assert "Nothing archived" in resp.text
```

- [ ] **Step 2: Run; verify failures**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_routes_videos.py tests/test_routes_home.py -v -k "archive"`
Expected: FAIL (routes don't exist → 404/405; home-hidden test may already pass thanks to Task 2 — that's fine).

- [ ] **Step 3: Add the two POST routes**

In `app/routes/videos.py`, add after `retranscribe_video` (~line 478) and BEFORE the `@router.get("/v/{video_id}", ...)` catch-all (route order: specific paths before the `{video_id}` GET — but these are POSTs so order vs the GET doesn't collide; still, keep them above the GET for readability):

```python
@router.post("/v/{video_id}/archive")
async def archive_video(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    ok = await videos_repo.set_archived(
        db, video_id, user_id=current_user_id, archived=True,
    )
    if not ok:
        raise HTTPException(404)
    return RedirectResponse("/", status_code=303)


@router.post("/v/{video_id}/unarchive")
async def unarchive_video(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    ok = await videos_repo.set_archived(
        db, video_id, user_id=current_user_id, archived=False,
    )
    if not ok:
        raise HTTPException(404)
    return RedirectResponse(f"/v/{video_id}", status_code=303)
```

- [ ] **Step 4: Add the `/archive` page route**

In `app/routes/home.py`, add a handler (place it near the load-more route). It needs the same `templates`, `get_db`, `get_current_user_id`, `get_current_user` deps already imported there:

```python
@router.get("/archive", response_class=HTMLResponse)
async def archive_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
) -> HTMLResponse:
    videos = await videos_repo.list_archived(db, user_id=current_user_id)
    return templates.TemplateResponse(
        request,
        "archive.html",
        {"videos": videos, "current_user": current_user},
    )
```

- [ ] **Step 5: Create `app/templates/archive.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Archive</h1>
<p class="caption">Archived items are hidden from your library, search, and digests. Restore one to bring it back.</p>
{% if videos %}
<section id="video-list">
  {% for video in videos %}
    {% include "video_card.html" %}
  {% endfor %}
</section>
{% else %}
<p class="empty">Nothing archived.</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 6: Run tests**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_routes_videos.py tests/test_routes_home.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/routes/videos.py app/routes/home.py app/templates/archive.html tests/test_routes_videos.py tests/test_routes_home.py
git commit -m "feat(archive): archive/unarchive routes + /archive page"
```

---

### Task 5: UI — Archive/Restore button on detail page + Archive link on home

**Files:**
- Modify: `app/templates/video_detail.html` (action group ~line 59; archived banner)
- Modify: `app/templates/home.html` (Library section — Archive link)
- Modify: `app/routes/home.py` (pass `archived_count` to home template)
- Test: `tests/test_routes_videos.py`, `tests/test_routes_home.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routes_videos.py`:

```python
def test_detail_shows_archive_button_when_active(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/v1")
    assert 'action="/v/v1/archive"' in resp.text
    assert 'action="/v/v1/unarchive"' not in resp.text


def test_detail_shows_restore_button_when_archived(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_archived(
                app.state.db, "v1", user_id=1, archived=True,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/v1")
    assert 'action="/v/v1/unarchive"' in resp.text
    assert "Archived" in resp.text
```

Append to `tests/test_routes_home.py`:

```python
def test_home_shows_archive_link_when_items_archived(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_archived(
                app.state.db, "v1", user_id=1, archived=True,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert 'href="/archive"' in resp.text
```

- [ ] **Step 2: Run; verify failures**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_routes_videos.py tests/test_routes_home.py -v -k "archive_button or restore_button or archive_link"`
Expected: FAIL.

- [ ] **Step 3: Detail-page buttons + banner**

In `app/templates/video_detail.html`, inside the `<div class="action-group" ...>` block, after the Re-transcribe `{% endif %}` (~line 60) and before the closing `</div>` of `action-group`, add:

```html
        {% if video.archived_at %}
          <form method="post" action="/v/{{ video.id }}/unarchive">
            <button type="submit" class="link-button">Restore from archive</button>
          </form>
        {% else %}
          <form method="post" action="/v/{{ video.id }}/archive">
            <button type="submit" class="link-button">Archive</button>
          </form>
        {% endif %}
```

And add an "Archived" banner near the top of the `<header>` (right after the `<h1>{{ video.title }}</h1>` line):

```html
    {% if video.archived_at %}
      <p class="archived-banner" role="status">{{ icon('archive', 16) }} Archived — hidden from your library.</p>
    {% endif %}
```

If the `archive` icon doesn't exist in the icon set, use plain text without the `{{ icon(...) }}` call (check `app/static/icons/` or the `icon()` macro). Keeping it text-only is acceptable:

```html
    {% if video.archived_at %}
      <p class="archived-banner" role="status">Archived — hidden from your library.</p>
    {% endif %}
```

- [ ] **Step 4: Home — Archive link, gated on count**

In `app/routes/home.py` `home()`, after `recent_digests = ...` (~line 124), add:

```python
    archived_count = await videos_repo.count_archived(
        db, user_id=current_user_id,
    )
```

And add `"archived_count": archived_count,` to the template context dict.

In `app/templates/home.html`, in the Library section (after the `#video-list` section / load-more block, near the other `playlist-strip-more` links), add:

```html
{% if archived_count %}
  <p class="playlist-strip-more">
    <a href="/archive">Archive ({{ archived_count }}) →</a>
  </p>
{% endif %}
```

- [ ] **Step 5: Run tests**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_routes_videos.py tests/test_routes_home.py -v`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/templates/video_detail.html app/templates/home.html app/routes/home.py tests/test_routes_videos.py tests/test_routes_home.py
git commit -m "feat(archive): detail-page archive/restore button + home archive link"
```

---

### Task 6: Full-suite verification

- [ ] **Step 1: Run the whole suite**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/ -q`
Expected: PASS. Likely stragglers if any: a home/test that asserted a now-archived video appears, or an ask/digest test. Fix forward.

- [ ] **Step 2: Lint**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: Commit any straggler fixes**

```bash
git add -A
git commit -m "test(archive): align remaining tests with archive filter"
```

(Skip if nothing changed.)
