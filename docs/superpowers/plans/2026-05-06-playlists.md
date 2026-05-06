# Playlists Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Save YouTube playlists as subscriptions, automatically refresh them on a schedule, and surface new videos in the existing library — with multi-user schema groundwork to avoid a future migration.

**Architecture:** Two new tables (`playlists`, `playlist_videos`) plus `user_id` columns on `videos`, `chat_messages`, and `settings`. A `PlaylistScheduler` task in the lifespan polls each playlist on a global interval (default 6 h) and reuses the existing job worker for per-video summarization. Initial import limits to the latest 20 entries; a "Load older" button can pull more on demand.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, yt-dlp `extract_flat`, Jinja2/HTMX. No new dependencies.

**Reference:** [docs/superpowers/specs/2026-05-06-playlists-design.md](../specs/2026-05-06-playlists-design.md)

---

## File Structure

```
app/
  db.py                   # MODIFY: add playlists tables, migrate existing tables
  models.py               # MODIFY: add Playlist dataclass
  scheduler.py            # NEW: PlaylistScheduler class (mirrors Worker)
  main.py                 # MODIFY: start scheduler in lifespan
  repos/
    playlists.py          # NEW: CRUD + linking
    settings.py           # MODIFY: support (user_id, key) PK
    videos.py             # MODIFY: accept user_id on upsert
    chat.py               # MODIFY: accept user_id on append
  services/
    playlist.py           # NEW: yt-dlp wrapper for extract_flat
    playlist_sync.py      # NEW: sync_playlist + load_older_videos
  routes/
    playlists.py          # NEW: /playlists, /p/{id}, refresh, load-older, remove
    home.py               # MODIFY: pass playlists to template
    settings.py           # MODIFY: add interval + initial-limit fields
  templates/
    home.html             # MODIFY: render Playlists strip + Add tile
    playlist_card.html    # NEW: small card for the home strip
    playlist_detail.html  # NEW: /p/{id} page
    playlist_new.html     # NEW: /playlists/new form
    settings.html         # MODIFY: two new fields

tests/
  fixtures/
    yt_dlp_playlist.json  # NEW: recorded extract_flat output
  test_db.py              # MODIFY: assert new tables + migration idempotency
  test_models.py          # MODIFY: Playlist dataclass test
  test_repos_playlists.py # NEW
  test_repos_settings.py  # MODIFY: PK is now composite
  test_repos_videos.py    # MODIFY: user_id parameter
  test_repos_chat.py      # MODIFY: user_id parameter
  test_services_playlist.py     # NEW
  test_services_playlist_sync.py # NEW
  test_scheduler.py       # NEW
  test_routes_playlists.py # NEW
  test_routes_home.py     # MODIFY: assert Playlists strip when present
  test_routes_settings.py # MODIFY: new fields persist
```

**File responsibilities:**
- `db.py` owns the schema. The `init_schema` function detects the database version (by inspecting `PRAGMA table_info`) and applies migrations idempotently.
- `repos/playlists.py` is a thin set of async functions over `aiosqlite.Connection`. No business logic.
- `services/playlist.py` is a stateless wrapper around yt-dlp's `extract_flat` mode.
- `services/playlist_sync.py` is the orchestrator: it composes `playlist.fetch_playlist`, `repos.playlists`, `repos.videos`, `repos.jobs`, and `services.youtube.download_thumbnail`. It does not touch HTTP or templates.
- `scheduler.py` mirrors `worker.py`'s structure — same stop pattern, same interval-loop semantics. It only delegates to `playlist_sync.sync_playlist`.
- `routes/playlists.py` is the only place where HTTP semantics meet the sync logic.

---

## Phase 1: Schema migrations

Goal: `init_schema` brings any existing DB to the V2 shape (with `user_id` columns and the two new tables) and is idempotent across runs.

### Task 1.1: New tables in SCHEMA + idempotent migrations

**Files:**
- Modify: `app/db.py`

- [ ] **Step 1: Replace `app/db.py` with the full V2 version**

```python
import aiosqlite

from app.config import Config

# Base schema for a fresh install. Existing databases are upgraded by
# _run_migrations() below.
SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
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
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(id),
    state TEXT NOT NULL CHECK(state IN ('pending','running','done','failed')),
    step TEXT NOT NULL DEFAULT '',
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    video_id TEXT NOT NULL REFERENCES videos(id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_video_created ON chat_messages(video_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER NOT NULL DEFAULT 1,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS playlists (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    thumbnail_path TEXT,
    last_refreshed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS playlist_videos (
    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(id),
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (playlist_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_videos_video ON playlist_videos(video_id);

CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    id UNINDEXED,
    title,
    description,
    content='videos',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts(rowid, id, title, description)
    VALUES (new.rowid, new.id, new.title, new.description);
END;

CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, id, title, description)
    VALUES ('delete', old.rowid, old.id, old.title, old.description);
END;

CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, id, title, description)
    VALUES ('delete', old.rowid, old.id, old.title, old.description);
    INSERT INTO videos_fts(rowid, id, title, description)
    VALUES (new.rowid, new.id, new.title, new.description);
END;
"""


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _run_migrations(conn: aiosqlite.Connection) -> None:
    """Upgrade an existing database to the current shape.

    Each migration is gated by a feature check, so running this on a fresh
    database (where SCHEMA already produced the V2 shape) is a no-op.
    """
    # Add user_id to videos / chat_messages if missing.
    if "user_id" not in await _table_columns(conn, "videos"):
        await conn.execute(
            "ALTER TABLE videos ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1"
        )

    if "user_id" not in await _table_columns(conn, "chat_messages"):
        await conn.execute(
            "ALTER TABLE chat_messages ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1"
        )

    # Settings: PK migration. SQLite cannot change a PK in place, so we
    # detect the old shape (single-column key PK with no user_id) and
    # rebuild the table.
    settings_cols = await _table_columns(conn, "settings")
    if "user_id" not in settings_cols:
        await conn.executescript(
            """
            CREATE TABLE settings_new (
                user_id INTEGER NOT NULL DEFAULT 1,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            );
            INSERT INTO settings_new (user_id, key, value)
                SELECT 1, key, value FROM settings;
            DROP TABLE settings;
            ALTER TABLE settings_new RENAME TO settings;
            """
        )
    await conn.commit()


async def connect(config: Config) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(config.db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    await conn.commit()
    await _run_migrations(conn)
```

- [ ] **Step 2: Add a test for the migration on a legacy DB**

Append to `tests/test_db.py`:
```python
import sqlite3


def test_init_schema_migrates_legacy_database(tmp_path):
    """A pre-V2 DB (no user_id, single-column settings PK) must upgrade
    cleanly when init_schema runs against it."""
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE videos (
            id TEXT PRIMARY KEY,
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
        );
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL REFERENCES videos(id),
            state TEXT NOT NULL CHECK(state IN ('pending','running','done','failed')),
            step TEXT NOT NULL DEFAULT '',
            error_message TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL REFERENCES videos(id),
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO videos (id, url, title) VALUES ('x', 'u', 't');
        INSERT INTO settings (key, value) VALUES ('llm_model', 'openai/gpt-4o');
        """
    )
    legacy.commit()
    legacy.close()

    import asyncio
    import aiosqlite

    async def run():
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        from app.db import init_schema
        await init_schema(conn)
        # videos.user_id default 1
        cursor = await conn.execute("SELECT user_id FROM videos WHERE id='x'")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1
        # settings now has user_id column and the row carried over
        cursor = await conn.execute(
            "SELECT user_id, value FROM settings WHERE key='llm_model'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] == "openai/gpt-4o"
        # New tables exist
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('playlists','playlist_videos')"
        )
        names = {r[0] for r in await cursor.fetchall()}
        assert names == {"playlists", "playlist_videos"}
        await conn.close()

    asyncio.run(run())


async def test_init_schema_creates_playlists_tables(db):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {r[0] for r in await cursor.fetchall()}
    assert "playlists" in tables
    assert "playlist_videos" in tables


async def test_init_schema_idempotent_on_v2(db):
    """Calling init_schema twice on an already-V2 DB must not error."""
    from app.db import init_schema
    await init_schema(db)
    await init_schema(db)
    cursor = await db.execute("SELECT COUNT(*) FROM playlists")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0
```

- [ ] **Step 3: Run the new tests**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: PASS for all tests including the three new ones.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: PASS. Existing tests should still work because `videos.user_id` and `chat_messages.user_id` have `DEFAULT 1`, and the settings repo still selects by `key` (the new PK is composite but the existing single-`key` SELECT remains valid as long as we don't insert duplicate user-1 rows — which is impossible because `user_id` defaults to 1).

- [ ] **Step 5: Lint + commit**

Run: `.venv/bin/ruff check app tests`
Expected: clean.

```bash
git add app/db.py tests/test_db.py
git commit -m "feat(db): add playlists tables and user_id migrations"
```

---

## Phase 2: Repos & models for the new shape

### Task 2.1: Add `Playlist` dataclass

**Files:**
- Modify: `app/models.py`
- Modify: `tests/test_models.py`

- [ ] **Step 1: Add a failing test**

Append to `tests/test_models.py`:
```python
def test_playlist_dataclass():
    from app.models import Playlist
    p = Playlist(
        id="PLh9GXHYeT6w",
        user_id=1,
        url="https://www.youtube.com/playlist?list=PLh9GXHYeT6w",
        title="My playlist",
        description="",
        thumbnail_path=None,
        last_refreshed_at=None,
        created_at=datetime(2026, 5, 6),
    )
    assert p.id == "PLh9GXHYeT6w"
    assert p.user_id == 1
    assert p.last_refreshed_at is None
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_models.py::test_playlist_dataclass -v`
Expected: FAIL (ImportError for `Playlist`).

- [ ] **Step 3: Add the dataclass**

Append to `app/models.py`:
```python
@dataclass
class Playlist:
    id: str
    user_id: int
    url: str
    title: str
    description: str
    thumbnail_path: str | None
    last_refreshed_at: datetime | None
    created_at: datetime
```

- [ ] **Step 4: Run, verify pass + lint**

```bash
.venv/bin/pytest tests/test_models.py -v
.venv/bin/ruff check app tests
```

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat(models): add Playlist dataclass"
```

### Task 2.2: Update settings repo to scope by user_id

**Files:**
- Modify: `app/repos/settings.py`
- Modify: `tests/test_repos_settings.py`

The new schema uses `(user_id, key)` as the PK. We keep the existing function signatures but pass `user_id=1` internally. This avoids breaking the call sites.

- [ ] **Step 1: Add failing test asserting user-scope behavior**

Append to `tests/test_repos_settings.py`:
```python
async def test_settings_isolated_per_user(db: aiosqlite.Connection):
    from app.repos import settings as settings_repo
    # Default user is 1
    await settings_repo.set(db, "model", "user1-value")
    # Insert a row for user 2 directly
    await db.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (2, 'model', 'user2-value')"
    )
    await db.commit()
    # The repo's get/set/get_all is implicitly user 1.
    assert await settings_repo.get(db, "model") == "user1-value"
    all_settings = await settings_repo.get_all(db)
    assert all_settings.get("model") == "user1-value"
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_repos_settings.py -v`
Expected: at least one FAIL because the repo currently returns whichever row came first.

- [ ] **Step 3: Update `app/repos/settings.py`**

Replace contents:
```python
import aiosqlite

# All public functions act as user 1 implicitly. When auth lands, they
# will accept a user_id parameter and the routes will pass the
# authenticated user's id.
_DEFAULT_USER = 1


async def get(db: aiosqlite.Connection, key: str) -> str | None:
    cursor = await db.execute(
        "SELECT value FROM settings WHERE user_id=? AND key=?",
        (_DEFAULT_USER, key),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value
        """,
        (_DEFAULT_USER, key, value),
    )
    await db.commit()


async def get_all(db: aiosqlite.Connection) -> dict[str, str]:
    cursor = await db.execute(
        "SELECT key, value FROM settings WHERE user_id=?", (_DEFAULT_USER,)
    )
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def delete(db: aiosqlite.Connection, key: str) -> None:
    await db.execute(
        "DELETE FROM settings WHERE user_id=? AND key=?",
        (_DEFAULT_USER, key),
    )
    await db.commit()
```

- [ ] **Step 4: Run, verify pass**

```bash
.venv/bin/pytest tests/test_repos_settings.py -v
.venv/bin/pytest -q   # full suite — settings is used widely
```

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check app tests
git add app/repos/settings.py tests/test_repos_settings.py
git commit -m "feat(settings): scope queries to user_id=1 (multi-user groundwork)"
```

### Task 2.3: Add `Playlist`-aware methods to videos and chat repos

**Files:**
- Modify: `app/repos/videos.py`
- Modify: `app/repos/chat.py`
- Modify: `tests/test_repos_videos.py`
- Modify: `tests/test_repos_chat.py`

The existing `upsert_metadata` and `chat.append` continue to work; we add an optional `user_id` parameter (default 1).

- [ ] **Step 1: Update `videos_repo.upsert_metadata` signature**

In `app/repos/videos.py`, change `upsert_metadata`:
```python
async def upsert_metadata(
    db: aiosqlite.Connection,
    *,
    video_id: str,
    url: str,
    title: str,
    description: str,
    thumbnail_path: str | None,
    duration_seconds: int | None,
    user_id: int = 1,
) -> None:
    await db.execute(
        """
        INSERT INTO videos (id, user_id, url, title, description, thumbnail_path, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            url=excluded.url,
            title=excluded.title,
            description=excluded.description,
            thumbnail_path=COALESCE(excluded.thumbnail_path, videos.thumbnail_path),
            duration_seconds=COALESCE(excluded.duration_seconds, videos.duration_seconds),
            updated_at=datetime('now')
        """,
        (video_id, user_id, url, title, description, thumbnail_path, duration_seconds),
    )
    await db.commit()
```

- [ ] **Step 2: Update `chat_repo.append` signature**

In `app/repos/chat.py`, change `append`:
```python
async def append(
    db: aiosqlite.Connection,
    video_id: str,
    role: ChatRole,
    content: str,
    *,
    user_id: int = 1,
) -> ChatMessage:
    cursor = await db.execute(
        "INSERT INTO chat_messages (user_id, video_id, role, content) VALUES (?, ?, ?, ?)",
        (user_id, video_id, role, content),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    fetched = await db.execute(
        "SELECT * FROM chat_messages WHERE id=?", (cursor.lastrowid,)
    )
    row = await fetched.fetchone()
    assert row is not None
    return _row_to_msg(row)
```

- [ ] **Step 3: Run existing tests — they should still pass because `user_id` has a default**

Run: `.venv/bin/pytest tests/test_repos_videos.py tests/test_repos_chat.py -v`
Expected: PASS.

- [ ] **Step 4: Add explicit user_id tests**

Append to `tests/test_repos_videos.py`:
```python
async def test_upsert_metadata_uses_default_user_when_not_passed(db):
    await videos_repo.upsert_metadata(
        db, video_id="u1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    cursor = await db.execute("SELECT user_id FROM videos WHERE id='u1'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_upsert_metadata_accepts_explicit_user_id(db):
    await videos_repo.upsert_metadata(
        db, video_id="u2", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
        user_id=42,
    )
    cursor = await db.execute("SELECT user_id FROM videos WHERE id='u2'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 42
```

Append to `tests/test_repos_chat.py`:
```python
async def test_append_uses_default_user(db: aiosqlite.Connection):
    await _video(db)
    msg = await chat_repo.append(db, "v1", "user", "hi")
    cursor = await db.execute(
        "SELECT user_id FROM chat_messages WHERE id=?", (msg.id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_append_accepts_explicit_user(db: aiosqlite.Connection):
    await _video(db)
    msg = await chat_repo.append(db, "v1", "user", "hi", user_id=7)
    cursor = await db.execute(
        "SELECT user_id FROM chat_messages WHERE id=?", (msg.id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 7
```

- [ ] **Step 5: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/repos/videos.py app/repos/chat.py tests/test_repos_videos.py tests/test_repos_chat.py
git commit -m "feat(repos): accept user_id on upsert_metadata and chat.append"
```

### Task 2.4: Playlists repo

**Files:**
- Create: `app/repos/playlists.py`
- Create: `tests/test_repos_playlists.py`

- [ ] **Step 1: Write the failing test**

`tests/test_repos_playlists.py`:
```python
import aiosqlite

from app.repos import playlists as playlists_repo
from app.repos import videos as videos_repo


async def _make_playlist(db: aiosqlite.Connection, pid: str = "p1") -> None:
    await playlists_repo.create(
        db,
        playlist_id=pid,
        user_id=1,
        url=f"https://youtube.com/playlist?list={pid}",
        title="My PL",
        description="",
        thumbnail_path=None,
    )


async def _make_video(db: aiosqlite.Connection, vid: str) -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title=vid,
        description="", thumbnail_path=None, duration_seconds=None,
    )


async def test_create_and_get(db: aiosqlite.Connection):
    await _make_playlist(db)
    p = await playlists_repo.get(db, "p1")
    assert p is not None
    assert p.id == "p1"
    assert p.user_id == 1
    assert p.title == "My PL"
    assert p.last_refreshed_at is None


async def test_create_is_idempotent(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_playlist(db)  # second call should not raise
    p = await playlists_repo.get(db, "p1")
    assert p is not None


async def test_list_for_user(db: aiosqlite.Connection):
    await _make_playlist(db, "p1")
    await _make_playlist(db, "p2")
    rows = await playlists_repo.list_for_user(db, 1)
    ids = sorted(p.id for p in rows)
    assert ids == ["p1", "p2"]


async def test_list_for_user_returns_empty_for_other_user(db: aiosqlite.Connection):
    await _make_playlist(db, "p1")
    assert await playlists_repo.list_for_user(db, 99) == []


async def test_delete_cascades_to_links(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    await playlists_repo.link_video(db, "p1", "v1")
    await playlists_repo.delete(db, "p1")
    # link is gone via CASCADE
    cursor = await db.execute("SELECT COUNT(*) FROM playlist_videos")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_link_video_returns_true_when_new(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    assert await playlists_repo.link_video(db, "p1", "v1") is True


async def test_link_video_returns_false_when_already_linked(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    await playlists_repo.link_video(db, "p1", "v1")
    assert await playlists_repo.link_video(db, "p1", "v1") is False


async def test_linked_video_ids(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    await _make_video(db, "v2")
    await playlists_repo.link_video(db, "p1", "v1")
    await playlists_repo.link_video(db, "p1", "v2")
    ids = await playlists_repo.linked_video_ids(db, "p1")
    assert ids == {"v1", "v2"}


async def test_videos_for_playlist_orders_recent_first(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    await _make_video(db, "v2")
    await playlists_repo.link_video(db, "p1", "v1")
    await playlists_repo.link_video(db, "p1", "v2")
    rows = await playlists_repo.videos_for_playlist(db, "p1")
    # v2 was linked second → most recent → comes first
    assert [v.id for v in rows] == ["v2", "v1"]


async def test_set_last_refreshed(db: aiosqlite.Connection):
    await _make_playlist(db)
    await playlists_repo.set_last_refreshed(db, "p1")
    p = await playlists_repo.get(db, "p1")
    assert p is not None
    assert p.last_refreshed_at is not None
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_repos_playlists.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement `app/repos/playlists.py`**

```python
from datetime import datetime

import aiosqlite

from app.models import Playlist, Video
from app.repos.videos import _row_to_video


def _row_to_playlist(row: aiosqlite.Row) -> Playlist:
    last = row["last_refreshed_at"]
    return Playlist(
        id=row["id"],
        user_id=row["user_id"],
        url=row["url"],
        title=row["title"],
        description=row["description"],
        thumbnail_path=row["thumbnail_path"],
        last_refreshed_at=datetime.fromisoformat(last) if last else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def create(
    db: aiosqlite.Connection,
    *,
    playlist_id: str,
    user_id: int,
    url: str,
    title: str,
    description: str,
    thumbnail_path: str | None,
) -> None:
    await db.execute(
        """
        INSERT INTO playlists (id, user_id, url, title, description, thumbnail_path)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            url=excluded.url,
            title=excluded.title,
            description=excluded.description,
            thumbnail_path=COALESCE(excluded.thumbnail_path, playlists.thumbnail_path)
        """,
        (playlist_id, user_id, url, title, description, thumbnail_path),
    )
    await db.commit()


async def get(db: aiosqlite.Connection, playlist_id: str) -> Playlist | None:
    cursor = await db.execute(
        "SELECT * FROM playlists WHERE id=?", (playlist_id,)
    )
    row = await cursor.fetchone()
    return _row_to_playlist(row) if row else None


async def list_for_user(db: aiosqlite.Connection, user_id: int) -> list[Playlist]:
    cursor = await db.execute(
        "SELECT * FROM playlists WHERE user_id=? ORDER BY created_at DESC, id DESC",
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_playlist(r) for r in rows]


async def delete(db: aiosqlite.Connection, playlist_id: str) -> None:
    await db.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    await db.commit()


async def set_last_refreshed(db: aiosqlite.Connection, playlist_id: str) -> None:
    await db.execute(
        "UPDATE playlists SET last_refreshed_at=datetime('now') WHERE id=?",
        (playlist_id,),
    )
    await db.commit()


async def link_video(
    db: aiosqlite.Connection, playlist_id: str, video_id: str
) -> bool:
    """Insert (playlist_id, video_id). Return True if newly inserted,
    False if the link already existed."""
    cursor = await db.execute(
        "INSERT OR IGNORE INTO playlist_videos (playlist_id, video_id) VALUES (?, ?)",
        (playlist_id, video_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def linked_video_ids(
    db: aiosqlite.Connection, playlist_id: str
) -> set[str]:
    cursor = await db.execute(
        "SELECT video_id FROM playlist_videos WHERE playlist_id=?",
        (playlist_id,),
    )
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def videos_for_playlist(
    db: aiosqlite.Connection, playlist_id: str
) -> list[Video]:
    cursor = await db.execute(
        """
        SELECT v.* FROM videos v
        JOIN playlist_videos pv ON v.id = pv.video_id
        WHERE pv.playlist_id = ?
        ORDER BY pv.added_at DESC, pv.video_id DESC
        """,
        (playlist_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_video(r) for r in rows]
```

- [ ] **Step 4: Run, verify pass**

```bash
.venv/bin/pytest tests/test_repos_playlists.py -v
.venv/bin/pytest -q
```

Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check app tests
git add app/repos/playlists.py tests/test_repos_playlists.py
git commit -m "feat(repos): playlists repo (CRUD + linking)"
```

---

## Phase 3: yt-dlp playlist service

### Task 3.1: `fetch_playlist` wrapper

**Files:**
- Create: `app/services/playlist.py`
- Create: `tests/test_services_playlist.py`
- Create: `tests/fixtures/yt_dlp_playlist.json`

- [ ] **Step 1: Save the fixture**

Create `tests/fixtures/yt_dlp_playlist.json`:
```json
{
  "id": "PLh9GXHYeT6wWS05I-U_3f1RtJKa58M9Lr",
  "title": "Sample Playlist",
  "description": "A demo playlist",
  "webpage_url": "https://www.youtube.com/playlist?list=PLh9GXHYeT6wWS05I-U_3f1RtJKa58M9Lr",
  "thumbnails": [
    {"url": "https://i.ytimg.com/vi/abc/hqdefault.jpg", "width": 480, "height": 360}
  ],
  "entries": [
    {
      "id": "vid-aaa-1234",
      "title": "First entry",
      "description": "",
      "duration": 600,
      "thumbnails": [
        {"url": "https://i.ytimg.com/vi/vid-aaa-1234/hqdefault.jpg"}
      ]
    },
    {
      "id": "vid-bbb-5678",
      "title": "Second entry",
      "description": "",
      "duration": 1200,
      "thumbnails": [
        {"url": "https://i.ytimg.com/vi/vid-bbb-5678/hqdefault.jpg"}
      ]
    }
  ]
}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_services_playlist.py`:
```python
import json
from pathlib import Path
from unittest.mock import patch

from app.services.playlist import PlaylistEntry, PlaylistMetadata, fetch_playlist

FIXTURES = Path(__file__).parent / "fixtures"


async def test_fetch_playlist_returns_dataclass():
    fixture = json.loads((FIXTURES / "yt_dlp_playlist.json").read_text())
    with patch("app.services.playlist._extract_playlist_info", return_value=fixture):
        meta = await fetch_playlist(
            "https://www.youtube.com/playlist?list=PLh9GXHYeT6w",
            cookies_path=None,
        )
    assert isinstance(meta, PlaylistMetadata)
    assert meta.id == "PLh9GXHYeT6wWS05I-U_3f1RtJKa58M9Lr"
    assert meta.title == "Sample Playlist"
    assert meta.thumbnail_url is not None
    assert len(meta.entries) == 2
    assert isinstance(meta.entries[0], PlaylistEntry)
    assert meta.entries[0].id == "vid-aaa-1234"
    assert meta.entries[0].title == "First entry"
    assert meta.entries[0].duration_seconds == 600
    assert meta.entries[0].thumbnail_url is not None


async def test_fetch_playlist_handles_missing_fields():
    minimal = {
        "id": "PLfoo",
        "title": "T",
        "webpage_url": "u",
        "entries": [
            {"id": "v1", "title": "v1"},
        ],
    }
    with patch("app.services.playlist._extract_playlist_info", return_value=minimal):
        meta = await fetch_playlist("u", cookies_path=None)
    assert meta.description == ""
    assert meta.thumbnail_url is None
    assert meta.entries[0].description == ""
    assert meta.entries[0].duration_seconds is None
    assert meta.entries[0].thumbnail_url is None


async def test_fetch_playlist_skips_empty_entries():
    """yt-dlp can yield None entries for unavailable videos."""
    payload = {
        "id": "PLfoo",
        "title": "T",
        "webpage_url": "u",
        "entries": [
            None,
            {"id": "v1", "title": "good"},
            None,
        ],
    }
    with patch("app.services.playlist._extract_playlist_info", return_value=payload):
        meta = await fetch_playlist("u", cookies_path=None)
    assert [e.id for e in meta.entries] == ["v1"]
```

- [ ] **Step 3: Run, verify failure**

Run: `.venv/bin/pytest tests/test_services_playlist.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 4: Implement `app/services/playlist.py`**

```python
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL


@dataclass(frozen=True)
class PlaylistEntry:
    id: str
    title: str
    description: str
    thumbnail_url: str | None
    duration_seconds: int | None


@dataclass(frozen=True)
class PlaylistMetadata:
    id: str
    url: str
    title: str
    description: str
    thumbnail_url: str | None
    entries: list[PlaylistEntry]


def _extract_playlist_info(url: str, cookies_path: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)  # type: ignore[return-value]


def _pick_thumbnail(item: dict[str, Any]) -> str | None:
    thumbs = item.get("thumbnails") or []
    if thumbs and isinstance(thumbs, list):
        for t in thumbs:
            if isinstance(t, dict) and t.get("url"):
                return t["url"]
    return item.get("thumbnail")


def _entry_from_dict(raw: dict[str, Any]) -> PlaylistEntry:
    return PlaylistEntry(
        id=raw["id"],
        title=raw.get("title") or "",
        description=raw.get("description") or "",
        thumbnail_url=_pick_thumbnail(raw),
        duration_seconds=raw.get("duration"),
    )


async def fetch_playlist(
    url: str, cookies_path: Path | None
) -> PlaylistMetadata:
    info = await asyncio.to_thread(_extract_playlist_info, url, cookies_path)
    raw_entries = info.get("entries") or []
    entries = [_entry_from_dict(e) for e in raw_entries if e]
    return PlaylistMetadata(
        id=info["id"],
        url=info.get("webpage_url", url),
        title=info.get("title") or "",
        description=info.get("description") or "",
        thumbnail_url=_pick_thumbnail(info),
        entries=entries,
    )
```

- [ ] **Step 5: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_services_playlist.py -v
.venv/bin/ruff check app tests
git add app/services/playlist.py tests/test_services_playlist.py tests/fixtures/yt_dlp_playlist.json
git commit -m "feat(services): yt-dlp playlist extraction"
```

---

## Phase 4: Playlist sync orchestrator

### Task 4.1: `sync_playlist` + `load_older_videos`

**Files:**
- Create: `app/services/playlist_sync.py`
- Create: `tests/test_services_playlist_sync.py`

- [ ] **Step 1: Write the failing test**

`tests/test_services_playlist_sync.py`:
```python
from unittest.mock import AsyncMock, patch

from app.config import Config
from app.repos import jobs as jobs_repo
from app.repos import playlists as playlists_repo
from app.repos import videos as videos_repo
from app.services.playlist import PlaylistEntry, PlaylistMetadata


def _meta(plid: str = "p1", entries: list[PlaylistEntry] | None = None) -> PlaylistMetadata:
    return PlaylistMetadata(
        id=plid,
        url=f"https://youtube.com/playlist?list={plid}",
        title="PL",
        description="",
        thumbnail_url=None,
        entries=entries or [],
    )


def _entry(vid: str, title: str = "x") -> PlaylistEntry:
    return PlaylistEntry(
        id=vid,
        title=title,
        description="",
        thumbnail_url=None,
        duration_seconds=None,
    )


async def test_sync_creates_videos_and_links_them(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )

    meta = _meta(entries=[_entry("vid_aaaaaaaa1"), _entry("vid_bbbbbbbb2")])
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import sync_playlist
        result = await sync_playlist(db, config, "p1")

    assert result.total_in_playlist == 2
    assert result.newly_linked == 2
    assert result.newly_enqueued == 2
    # Both videos exist
    assert await videos_repo.get(db, "vid_aaaaaaaa1") is not None
    assert await videos_repo.get(db, "vid_bbbbbbbb2") is not None
    # Both videos linked
    linked = await playlists_repo.linked_video_ids(db, "p1")
    assert linked == {"vid_aaaaaaaa1", "vid_bbbbbbbb2"}
    # last_refreshed_at populated
    p = await playlists_repo.get(db, "p1")
    assert p is not None
    assert p.last_refreshed_at is not None


async def test_sync_skips_already_linked_videos(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )
    # Pre-link an existing video
    await videos_repo.upsert_metadata(
        db, video_id="vid_old1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await playlists_repo.link_video(db, "p1", "vid_old1")

    meta = _meta(entries=[_entry("vid_old1"), _entry("vid_new1")])
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import sync_playlist
        result = await sync_playlist(db, config, "p1")

    assert result.newly_linked == 1
    assert result.newly_enqueued == 1


async def test_sync_does_not_enqueue_video_with_summary(db, tmp_path):
    """A video that already has a summary (e.g. imported earlier as a
    standalone video) only gets the playlist link, no new job."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="vid_done1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_summary(db, "vid_done1", "summary text", "model")

    meta = _meta(entries=[_entry("vid_done1")])
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import sync_playlist
        result = await sync_playlist(db, config, "p1")

    assert result.newly_linked == 1
    assert result.newly_enqueued == 0
    # No job was created
    job = await jobs_repo.latest_for_video(db, "vid_done1")
    assert job is None


async def test_sync_respects_initial_limit(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )
    entries = [_entry(f"v_{i:08d}") for i in range(50)]
    meta = _meta(entries=entries)
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import sync_playlist
        result = await sync_playlist(db, config, "p1", initial_limit=20)

    assert result.total_in_playlist == 50
    assert result.newly_linked == 20
    assert result.newly_enqueued == 20


async def test_load_older_takes_unlinked_entries(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )
    # Pre-link first 3 entries
    for i in range(3):
        await videos_repo.upsert_metadata(
            db, video_id=f"v_{i:08d}", url="u", title="t",
            description="", thumbnail_path=None, duration_seconds=None,
        )
        await playlists_repo.link_video(db, "p1", f"v_{i:08d}")

    entries = [_entry(f"v_{i:08d}") for i in range(10)]
    meta = _meta(entries=entries)
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import load_older_videos
        result = await load_older_videos(db, config, "p1", count=5)

    # 5 of the unlinked 7 get added
    assert result.newly_linked == 5
    assert result.newly_enqueued == 5


async def test_sync_raises_for_unknown_playlist(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    import pytest
    from app.services.playlist_sync import sync_playlist
    with pytest.raises(KeyError):
        await sync_playlist(db, config, "unknown_id")
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_services_playlist_sync.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement `app/services/playlist_sync.py`**

```python
from dataclasses import dataclass

import aiosqlite

from app.config import Config
from app.repos import jobs as jobs_repo
from app.repos import playlists as playlists_repo
from app.repos import videos as videos_repo
from app.services.playlist import PlaylistEntry, fetch_playlist
from app.services.youtube import download_thumbnail


@dataclass
class SyncResult:
    total_in_playlist: int
    newly_linked: int
    newly_enqueued: int


def _resolve_cookies(config: Config):
    p = config.cookies_path
    return p if p.exists() else None


async def _process_entries(
    db: aiosqlite.Connection,
    config: Config,
    playlist_id: str,
    user_id: int,
    entries: list[PlaylistEntry],
) -> SyncResult:
    """Common logic shared by sync_playlist and load_older_videos.

    Caller is responsible for filtering / slicing the entries list.
    """
    newly_linked = 0
    newly_enqueued = 0
    for entry in entries:
        existing = await videos_repo.get(db, entry.id)
        if existing is None:
            thumb_target = config.thumbnails_dir / f"{entry.id}.jpg"
            await download_thumbnail(entry.thumbnail_url, thumb_target)
            thumb_db_path = str(thumb_target) if thumb_target.exists() else None
            await videos_repo.upsert_metadata(
                db,
                video_id=entry.id,
                url=f"https://www.youtube.com/watch?v={entry.id}",
                title=entry.title,
                description=entry.description,
                thumbnail_path=thumb_db_path,
                duration_seconds=entry.duration_seconds,
                user_id=user_id,
            )
            existing = await videos_repo.get(db, entry.id)

        if await playlists_repo.link_video(db, playlist_id, entry.id):
            newly_linked += 1
            assert existing is not None
            if existing.summary is None:
                await jobs_repo.enqueue(db, entry.id)
                newly_enqueued += 1

    return SyncResult(
        total_in_playlist=0,  # caller will fill in
        newly_linked=newly_linked,
        newly_enqueued=newly_enqueued,
    )


async def sync_playlist(
    db: aiosqlite.Connection,
    config: Config,
    playlist_id: str,
    *,
    initial_limit: int | None = None,
) -> SyncResult:
    playlist = await playlists_repo.get(db, playlist_id)
    if playlist is None:
        raise KeyError(f"Unknown playlist: {playlist_id}")

    cookies = _resolve_cookies(config)
    meta = await fetch_playlist(playlist.url, cookies_path=cookies)
    total = len(meta.entries)
    entries = meta.entries[:initial_limit] if initial_limit else meta.entries

    result = await _process_entries(
        db, config, playlist_id, playlist.user_id, entries
    )
    result.total_in_playlist = total
    await playlists_repo.set_last_refreshed(db, playlist_id)
    return result


async def load_older_videos(
    db: aiosqlite.Connection,
    config: Config,
    playlist_id: str,
    *,
    count: int,
) -> SyncResult:
    playlist = await playlists_repo.get(db, playlist_id)
    if playlist is None:
        raise KeyError(f"Unknown playlist: {playlist_id}")

    cookies = _resolve_cookies(config)
    meta = await fetch_playlist(playlist.url, cookies_path=cookies)
    total = len(meta.entries)
    already_linked = await playlists_repo.linked_video_ids(db, playlist_id)
    candidates = [e for e in meta.entries if e.id not in already_linked]
    to_process = candidates[:count]

    result = await _process_entries(
        db, config, playlist_id, playlist.user_id, to_process
    )
    result.total_in_playlist = total
    return result
```

- [ ] **Step 4: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_services_playlist_sync.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/services/playlist_sync.py tests/test_services_playlist_sync.py
git commit -m "feat(services): playlist sync orchestrator + load older"
```

---

## Phase 5: Scheduler

### Task 5.1: `PlaylistScheduler` class

**Files:**
- Create: `app/scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

`tests/test_scheduler.py`:
```python
import asyncio
from unittest.mock import AsyncMock

import aiosqlite

from app.config import Config
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.scheduler import PlaylistScheduler


async def _make_playlist(db: aiosqlite.Connection, pid: str) -> None:
    await playlists_repo.create(
        db, playlist_id=pid, user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )


async def test_scheduler_calls_sync_for_each_playlist(db: aiosqlite.Connection, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await _make_playlist(db, "p1")
    await _make_playlist(db, "p2")
    # Tiny interval so the loop fires immediately
    await settings_repo.set(db, "playlist_refresh_interval_hours", "0")

    sync_calls: list[str] = []

    async def fake_sync(db_, config_, playlist_id):
        sync_calls.append(playlist_id)

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=fake_sync, min_sleep_seconds=0.05
    )
    task = asyncio.create_task(scheduler.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if len(sync_calls) >= 2:
            break
    scheduler.stop()
    await task

    assert set(sync_calls[:2]) == {"p1", "p2"}


async def test_scheduler_swallows_per_playlist_errors(
    db: aiosqlite.Connection, tmp_path
):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await _make_playlist(db, "p1")
    await _make_playlist(db, "p2")
    await settings_repo.set(db, "playlist_refresh_interval_hours", "0")

    seen: list[str] = []

    async def flaky_sync(db_, config_, playlist_id):
        seen.append(playlist_id)
        if playlist_id == "p1":
            raise RuntimeError("boom")

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=flaky_sync, min_sleep_seconds=0.05
    )
    task = asyncio.create_task(scheduler.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if "p2" in seen:
            break
    scheduler.stop()
    await task

    assert "p1" in seen
    assert "p2" in seen


async def test_scheduler_stops_promptly(db: aiosqlite.Connection, tmp_path):
    """Stop must wake a long sleep so the task ends quickly."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    # No playlists, so the scheduler sleeps the whole interval.
    await settings_repo.set(db, "playlist_refresh_interval_hours", "10")

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=AsyncMock(), min_sleep_seconds=0.05
    )
    task = asyncio.create_task(scheduler.run())
    # Give the scheduler a moment to settle into its first sleep.
    await asyncio.sleep(0.1)
    scheduler.stop()
    # Should return well within 1s, not 10 hours.
    await asyncio.wait_for(task, timeout=1.0)
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `app/scheduler.py`**

```python
import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiosqlite

from app.config import Config
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo

log = logging.getLogger(__name__)

SyncFn = Callable[[aiosqlite.Connection, Config, str], Awaitable[None]]


class PlaylistScheduler:
    """Periodically refresh every saved playlist.

    Reads `playlist_refresh_interval_hours` from settings each tick. The
    scheduler does not refresh on startup — it sleeps one interval first
    to avoid a refresh storm on container restart.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        config: Config,
        sync_fn: SyncFn,
        *,
        min_sleep_seconds: float = 1.0,
    ) -> None:
        self._db = db
        self._config = config
        self._sync_fn = sync_fn
        self._min_sleep_seconds = min_sleep_seconds
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def _interval_seconds(self) -> float:
        raw = await settings_repo.get(self._db, "playlist_refresh_interval_hours")
        try:
            hours = float(raw) if raw is not None else 6.0
        except ValueError:
            hours = 6.0
        return max(self._min_sleep_seconds, hours * 3600)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopped.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        while not self._stopped.is_set():
            await self._sleep_or_stop(await self._interval_seconds())
            if self._stopped.is_set():
                return
            try:
                playlists = await playlists_repo.list_for_user(self._db, 1)
            except Exception:
                log.exception("scheduler: list_for_user failed")
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
```

- [ ] **Step 4: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_scheduler.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/scheduler.py tests/test_scheduler.py
git commit -m "feat: PlaylistScheduler — periodic playlist refresh"
```

### Task 5.2: Wire scheduler into lifespan

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add scheduler start/stop to lifespan**

Replace the `lifespan` function in `app/main.py`:
```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.pipeline import process_video
    from app.scheduler import PlaylistScheduler
    from app.services.playlist_sync import sync_playlist
    from app.worker import Worker

    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    await init_schema(db)
    await jobs_repo.reset_orphaned_running(db)

    worker = Worker(db=db, config=config, process_video=process_video)
    worker_task = asyncio.create_task(worker.run())

    scheduler = PlaylistScheduler(db=db, config=config, sync_fn=sync_playlist)
    scheduler_task = asyncio.create_task(scheduler.run())

    app.state.config = config
    app.state.db = db
    app.state.worker = worker
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.stop()
        worker.stop()
        await scheduler_task
        await worker_task
        await db.close()
```

- [ ] **Step 2: Run the full suite — nothing should break**

Run: `.venv/bin/pytest -q`
Expected: PASS. The scheduler starts in tests too; it sleeps the full interval (default 6 h) and stops cleanly when the lifespan ends.

- [ ] **Step 3: Lint + commit**

```bash
.venv/bin/ruff check app tests
git add app/main.py
git commit -m "feat: start PlaylistScheduler in lifespan"
```

---

## Phase 6: Routes

### Task 6.1: Add playlists router (POST /playlists, GET /p/{id}, refresh, load-older, remove)

**Files:**
- Create: `app/routes/playlists.py`
- Modify: `app/main.py` (mount router)
- Create: `tests/test_routes_playlists.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_routes_playlists.py`:
```python
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.playlist import PlaylistEntry, PlaylistMetadata


def _meta(plid: str = "PLtest", entries: list[PlaylistEntry] | None = None) -> PlaylistMetadata:
    return PlaylistMetadata(
        id=plid,
        url=f"https://youtube.com/playlist?list={plid}",
        title="Test playlist",
        description="",
        thumbnail_url=None,
        entries=entries or [],
    )


def test_post_playlists_imports_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake = _meta(
        entries=[
            PlaylistEntry(
                id=f"v{i:010d}", title=f"v{i}", description="",
                thumbnail_url=None, duration_seconds=None,
            )
            for i in range(3)
        ]
    )
    app = create_app()
    with (
        patch("app.routes.playlists.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        patch("app.routes.playlists.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/playlists",
            data={"url": "https://www.youtube.com/playlist?list=PLtest"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/PLtest"


def test_post_playlists_invalid_url_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/playlists", data={"url": "not-a-url"})
    assert resp.status_code == 400


def test_get_playlist_detail_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            from app.repos import videos as videos_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLshow", user_id=1,
                url="u", title="Show me", description="",
                thumbnail_path=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1show", url="u", title="Inner",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await playlists_repo.link_video(app.state.db, "PLshow", "v1show")

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/p/PLshow")
    assert resp.status_code == 200
    assert "Show me" in resp.text
    assert "Inner" in resp.text


def test_get_playlist_404_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/p/PLnope")
    assert resp.status_code == 404


def test_post_playlist_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake = _meta("PLref")
    app = create_app()
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLref", user_id=1,
                url="https://youtube.com/playlist?list=PLref",
                title="r", description="", thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/p/PLref/refresh", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/PLref"


def test_post_playlist_load_older(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    entries = [
        PlaylistEntry(
            id=f"v{i:010d}", title="t", description="",
            thumbnail_url=None, duration_seconds=None,
        )
        for i in range(10)
    ]
    fake = _meta("PLold", entries=entries)
    app = create_app()
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLold", user_id=1,
                url="https://youtube.com/playlist?list=PLold",
                title="o", description="", thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/p/PLold/load-older", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/PLold"


def test_post_playlist_remove_deletes(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLgone", user_id=1, url="u",
                title="x", description="", thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/p/PLgone/remove", follow_redirects=False)
        assert resp.status_code == 303

        async def check():
            from app.repos import playlists as playlists_repo
            assert await playlists_repo.get(app.state.db, "PLgone") is None

        asyncio.get_event_loop().run_until_complete(check())


def test_get_new_playlist_form(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/playlists/new")
    assert resp.status_code == 200
    assert 'name="url"' in resp.text
    assert 'action="/playlists"' in resp.text
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_routes_playlists.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `app/routes/playlists.py`**

```python
import re

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_db
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.services.playlist import fetch_playlist
from app.services.playlist_sync import load_older_videos, sync_playlist
from app.services.youtube import download_thumbnail

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_PLAYLIST_ID_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


def _parse_playlist_id(url: str) -> str:
    match = _PLAYLIST_ID_RE.search(url)
    if not match:
        raise ValueError(f"Could not extract playlist id from {url!r}")
    return match.group(1)


@router.get("/playlists/new", response_class=HTMLResponse)
async def new_playlist_form(request: Request):
    return templates.TemplateResponse(request, "playlist_new.html", {})


@router.post("/playlists")
async def submit_playlist(
    url: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    try:
        _parse_playlist_id(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    cookies = config.cookies_path if config.cookies_path.exists() else None
    meta = await fetch_playlist(url, cookies_path=cookies)

    thumb_target = config.thumbnails_dir / f"playlist_{meta.id}.jpg"
    await download_thumbnail(meta.thumbnail_url, thumb_target)
    thumb_db_path = str(thumb_target) if thumb_target.exists() else None

    await playlists_repo.create(
        db,
        playlist_id=meta.id,
        user_id=1,
        url=meta.url,
        title=meta.title,
        description=meta.description,
        thumbnail_path=thumb_db_path,
    )

    raw_limit = await settings_repo.get(db, "playlist_initial_import_limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else 20
    except ValueError:
        limit = 20
    initial_limit: int | None = limit if limit > 0 else None

    await sync_playlist(db, config, meta.id, initial_limit=initial_limit)
    return RedirectResponse(f"/p/{meta.id}", status_code=303)


@router.get("/p/{playlist_id}", response_class=HTMLResponse)
async def playlist_detail(
    playlist_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    playlist = await playlists_repo.get(db, playlist_id)
    if playlist is None:
        raise HTTPException(404)
    videos = await playlists_repo.videos_for_playlist(db, playlist_id)
    return templates.TemplateResponse(
        request,
        "playlist_detail.html",
        {"playlist": playlist, "videos": videos},
    )


@router.post("/p/{playlist_id}/refresh")
async def playlist_refresh(
    playlist_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(404)
    await sync_playlist(db, config, playlist_id)
    return RedirectResponse(f"/p/{playlist_id}", status_code=303)


@router.post("/p/{playlist_id}/load-older")
async def playlist_load_older(
    playlist_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(404)
    raw_limit = await settings_repo.get(db, "playlist_initial_import_limit")
    try:
        count = int(raw_limit) if raw_limit is not None else 20
    except ValueError:
        count = 20
    if count <= 0:
        count = 20
    await load_older_videos(db, config, playlist_id, count=count)
    return RedirectResponse(f"/p/{playlist_id}", status_code=303)


@router.post("/p/{playlist_id}/remove")
async def playlist_remove(
    playlist_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(404)
    await playlists_repo.delete(db, playlist_id)
    return RedirectResponse("/", status_code=303)
```

- [ ] **Step 4: Mount the router in `app/main.py`**

In `create_app()`, after `app.include_router(settings_router)` add:
```python
from app.routes.playlists import router as playlists_router
app.include_router(playlists_router)
```

- [ ] **Step 5: Create the templates**

`app/templates/playlist_new.html`:
```html
{% extends "base.html" %}
{% block title %}Add a playlist — yt-summary{% endblock %}
{% block content %}
<div class="settings-page">
  <h1>Add a playlist</h1>
  <form method="post" action="/playlists" class="curl-form">
    <label>
      Playlist URL
      <input name="url" type="url" placeholder="https://www.youtube.com/playlist?list=..." required>
      <small>The latest videos will be imported and summarized. Older entries can be loaded later from the playlist page.</small>
    </label>
    <button type="submit">Save</button>
  </form>
</div>
{% endblock %}
```

`app/templates/playlist_detail.html`:
```html
{% extends "base.html" %}
{% block title %}{{ playlist.title }} — yt-summary{% endblock %}
{% block content %}
<article class="video-detail">
  <header>
    <h1>{{ playlist.title }}</h1>
    <p>
      <a href="{{ playlist.url }}" target="_blank" rel="noopener">↗ Open on YouTube</a>
      {% if playlist.last_refreshed_at %}
        · Last refreshed {{ playlist.last_refreshed_at.strftime('%Y-%m-%d %H:%M') }}
      {% else %}
        · Never refreshed
      {% endif %}
      <form method="post" action="/p/{{ playlist.id }}/refresh" style="display:inline">
        <button type="submit" class="link-button">Refresh</button>
      </form>
      <form method="post" action="/p/{{ playlist.id }}/load-older" style="display:inline">
        <button type="submit" class="link-button">Load older</button>
      </form>
      <form method="post" action="/p/{{ playlist.id }}/remove" style="display:inline"
            onsubmit="return confirm('Remove this playlist? Videos already in your library will stay.')">
        <button type="submit" class="link-button">Remove</button>
      </form>
    </p>
  </header>

  <p class="section-title">Videos in this playlist ({{ videos|length }})</p>

  <section id="video-list">
    {% for video in videos %}
      {% include "video_card.html" %}
    {% else %}
      <p class="empty">No videos linked yet.</p>
    {% endfor %}
  </section>
</article>
{% endblock %}
```

- [ ] **Step 6: Run the tests + lint + commit**

```bash
.venv/bin/pytest tests/test_routes_playlists.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/routes/playlists.py app/main.py app/templates/playlist_new.html app/templates/playlist_detail.html tests/test_routes_playlists.py
git commit -m "feat(routes): playlists router + detail page"
```

---

## Phase 7: Home page integration

### Task 7.1: Show Playlists strip on the home page

**Files:**
- Modify: `app/routes/home.py`
- Modify: `app/templates/home.html`
- Create: `app/templates/playlist_card.html`
- Modify: `tests/test_routes_home.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_routes_home.py`:
```python
def test_home_lists_playlists(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLhome", user_id=1, url="u",
                title="On home", description="",
                thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "On home" in resp.text
    assert "/p/PLhome" in resp.text
    # Add tile is rendered
    assert "/playlists/new" in resp.text


def test_home_no_playlists_strip_when_none(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    # Add tile + strip header are NOT shown when there are no playlists
    assert "/playlists/new" not in resp.text
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_routes_home.py -v`
Expected: at least one FAIL.

- [ ] **Step 3: Update `app/routes/home.py`**

Replace contents:
```python
import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.main import get_db
from app.repos import playlists as playlists_repo
from app.repos import videos as videos_repo

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    q: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
):
    if q:
        videos = await videos_repo.search(db, q)
    else:
        videos = await videos_repo.list_recent(db)
    playlists = await playlists_repo.list_for_user(db, 1)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"videos": videos, "q": q, "playlists": playlists},
    )
```

- [ ] **Step 4: Create `app/templates/playlist_card.html`**

```html
<a href="/p/{{ playlist.id }}" class="playlist-card">
  {% if playlist.thumbnail_path %}
    <img src="/thumbnails/playlist_{{ playlist.id }}.jpg" alt="">
  {% else %}
    <div class="playlist-card-placeholder">▣</div>
  {% endif %}
  <div class="playlist-card-body">
    <h4>{{ playlist.title }}</h4>
    <p class="caption">
      {% if playlist.last_refreshed_at %}
        Last refreshed {{ playlist.last_refreshed_at.strftime('%Y-%m-%d') }}
      {% else %}
        New
      {% endif %}
    </p>
  </div>
</a>
```

- [ ] **Step 5: Update `app/templates/home.html`**

Insert above the `<form method="get" action="/" class="search-form">` line:
```html
{% if playlists %}
  <p class="section-title">Playlists</p>
  <section class="playlist-strip">
    {% for playlist in playlists %}
      {% include "playlist_card.html" %}
    {% endfor %}
    <a href="/playlists/new" class="playlist-card playlist-card-add">
      <div class="playlist-card-placeholder">+</div>
      <div class="playlist-card-body">
        <h4>Add playlist</h4>
        <p class="caption">Subscribe to a YouTube playlist</p>
      </div>
    </a>
  </section>
{% else %}
  <p style="margin: 24px 0;">
    <a href="/playlists/new" class="btn btn-secondary">+ Add a playlist</a>
  </p>
{% endif %}
```

- [ ] **Step 6: Add CSS for playlist cards**

Append to `app/static/app.css`:
```css
.playlist-strip {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
  margin-bottom: 32px;
}
.playlist-card {
  display: flex;
  flex-direction: column;
  background: var(--canvas);
  border: 1px solid var(--hairline);
  border-radius: var(--rounded-lg);
  overflow: hidden;
  transition: border-color 150ms ease, transform 150ms ease;
  text-decoration: none;
}
.playlist-card:hover {
  border-color: var(--stone);
  transform: translateY(-2px);
  text-decoration: none;
}
.playlist-card img {
  width: 100%;
  height: 110px;
  object-fit: cover;
  background: var(--surface);
  display: block;
}
.playlist-card-placeholder {
  width: 100%;
  height: 110px;
  background: var(--surface);
  color: var(--stone);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 28px;
  font-weight: 300;
}
.playlist-card-body {
  padding: 12px 16px;
}
.playlist-card-body h4 {
  margin: 0 0 4px 0;
  font-size: 15px;
  font-weight: 600;
}
.playlist-card-body .caption {
  margin: 0;
  font-size: 13px;
  color: var(--steel);
}
.playlist-card-add .playlist-card-placeholder {
  background: rgba(0, 212, 164, 0.06);
  color: var(--brand-green-deep);
  font-size: 32px;
}
```

- [ ] **Step 7: Run tests + lint + commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/routes/home.py app/templates/home.html app/templates/playlist_card.html app/static/app.css tests/test_routes_home.py
git commit -m "feat(home): show Playlists strip with cards + Add tile"
```

---

## Phase 8: Settings extensions

### Task 8.1: Two new fields in Settings UI

**Files:**
- Modify: `app/routes/settings.py`
- Modify: `app/templates/settings.html`
- Modify: `tests/test_routes_settings.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_routes_settings.py`:
```python
def test_save_settings_persists_playlist_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        client.post(
            "/settings",
            data={
                "llm_model": "openai/gpt-4o",
                "llm_api_key": "",
                "llm_base_url": "",
                "whisper_model": "small",
                "playlist_refresh_interval_hours": "12",
                "playlist_initial_import_limit": "30",
            },
            follow_redirects=False,
        )
        import asyncio

        async def check():
            from app.repos import settings as settings_repo
            s = await settings_repo.get_all(app.state.db)
            assert s["playlist_refresh_interval_hours"] == "12"
            assert s["playlist_initial_import_limit"] == "30"

        asyncio.get_event_loop().run_until_complete(check())


def test_settings_form_renders_playlist_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert "playlist_refresh_interval_hours" in resp.text
    assert "playlist_initial_import_limit" in resp.text
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_routes_settings.py -v`
Expected: FAIL.

- [ ] **Step 3: Update `app/routes/settings.py`**

Modify `save_settings` to accept and persist the two new fields. Replace the function body with:
```python
@router.post("/settings")
async def save_settings(
    llm_model: str = Form(""),
    llm_api_key: str = Form(""),
    llm_base_url: str = Form(""),
    whisper_model: str = Form("small"),
    playlist_refresh_interval_hours: str = Form("6"),
    playlist_initial_import_limit: str = Form("20"),
    db: aiosqlite.Connection = Depends(get_db),
):
    llm_base_url = llm_base_url.strip().rstrip("/")
    for key, value in (
        ("llm_model", llm_model.strip()),
        ("llm_base_url", llm_base_url),
        ("whisper_model", whisper_model),
        ("playlist_refresh_interval_hours", playlist_refresh_interval_hours.strip()),
        ("playlist_initial_import_limit", playlist_initial_import_limit.strip()),
    ):
        if value:
            await settings_repo.set(db, key, value)
        else:
            await settings_repo.delete(db, key)
    if llm_api_key:
        await settings_repo.set(db, "llm_api_key", llm_api_key)
    return RedirectResponse("/settings", status_code=303)
```

- [ ] **Step 4: Update `app/templates/settings.html`**

Insert before the closing `</form>` of the main settings form (the form whose action is `/settings`, not the curl form):
```html
<label>
  Playlist refresh interval (hours)
  <input name="playlist_refresh_interval_hours" type="number" min="1" max="168"
         value="{{ settings.get('playlist_refresh_interval_hours', '6') }}">
  <small>How often the scheduler re-checks every saved playlist for new videos. Default 6.</small>
</label>
<label>
  Playlist initial import limit
  <input name="playlist_initial_import_limit" type="number" min="0" max="500"
         value="{{ settings.get('playlist_initial_import_limit', '20') }}">
  <small>Number of newest videos to enqueue when a playlist is first added, or when "Load older" is clicked. Set to 0 to import all entries on first add (the Pi will be busy for a while).</small>
</label>
```

- [ ] **Step 5: Run tests + lint + commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/routes/settings.py app/templates/settings.html tests/test_routes_settings.py
git commit -m "feat(settings): playlist refresh interval + initial import limit"
```

---

## Phase 9: Final smoke + docs

### Task 9.1: Update spec link in README + manual smoke

**Files:**
- Modify: `README.md` (optional — link the new spec)

- [ ] **Step 1: Append a sentence to README**

In `README.md`, update the design link section:
```markdown
See the [core design spec](docs/superpowers/specs/2026-05-05-yt-summary-design.md)
and the [playlists spec](docs/superpowers/specs/2026-05-06-playlists-design.md)
for architecture.
```

- [ ] **Step 2: Manual smoke test**

```bash
YTS_DATA_DIR=./data .venv/bin/uvicorn app.main:app
```

In a browser:
1. Open `http://localhost:8000/`
2. Configure LLM in Settings (if not already)
3. Click "Add a playlist", paste a YouTube playlist URL, save
4. Verify redirect to `/p/<id>`, video cards visible, jobs running
5. Go to home → "Playlists" strip should show the new playlist
6. Click "Refresh" on the playlist page → success
7. Click "Load older" → success (may report 0 if everything was imported)
8. Click "Remove" → confirm dialog → redirect home, playlist gone, videos remain in library

- [ ] **Step 3: Run full suite + lint a final time**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
```

- [ ] **Step 4: Commit + push**

```bash
git add README.md
git commit -m "docs: link playlists spec from README"
git push origin main
```

- [ ] **Step 5: Tag a release**

```bash
git tag v0.2.0
git push origin v0.2.0
```

This triggers the multi-arch GHCR build for the Pi.

---

## Self-Review

**Spec coverage check:**

- ✅ Schema groundwork (`user_id` on videos/chat_messages/settings, plus `playlists`/`playlist_videos`) — Phase 1 + 2
- ✅ `playlists` and `playlist_videos` tables with cascade — Task 1.1
- ✅ Idempotent migration from V1 schema — Task 1.1
- ✅ `Playlist` dataclass — Task 2.1
- ✅ `playlists` repo (create/get/list/delete/link/linked_video_ids/videos_for_playlist/set_last_refreshed) — Task 2.4
- ✅ `videos` and `chat` repos accept `user_id` — Task 2.3
- ✅ `settings` repo scoped to user 1 — Task 2.2
- ✅ `fetch_playlist` via yt-dlp `extract_flat` — Task 3.1
- ✅ `sync_playlist` with `initial_limit` — Task 4.1
- ✅ `load_older_videos` with `count` — Task 4.1
- ✅ Skip enqueueing when video already has a summary — Task 4.1
- ✅ `PlaylistScheduler` mirrors Worker, sleeps full interval before first run — Task 5.1
- ✅ Per-playlist sync errors don't kill the loop — Task 5.1
- ✅ Settings: `playlist_refresh_interval_hours`, `playlist_initial_import_limit` — Task 8.1
- ✅ POST /playlists / GET /p/{id} / refresh / load-older / remove — Task 6.1
- ✅ Home page Playlists strip + Add tile — Task 7.1
- ✅ Detail-page CSS (cards) — Task 7.1

**Placeholder scan:** No "TBD"/"TODO"/"add appropriate error handling"/"similar to". Every step has runnable code or a runnable command.

**Type consistency:** `PlaylistMetadata` and `PlaylistEntry` are defined once in Task 3.1 and referenced consistently in Task 4.1. `SyncResult` is defined in Task 4.1 and used in tests in the same task. `Playlist` dataclass defined in Task 2.1, returned by Task 2.4 repo methods. Repo function names (`create`, `get`, `list_for_user`, `delete`, `link_video`, `linked_video_ids`, `videos_for_playlist`, `set_last_refreshed`) are used identically wherever they're referenced. Scheduler interface (`stop`, `run`) matches Worker's pattern.

**One callout:** `videos_repo._row_to_video` is imported as a private symbol from `videos.py` into `playlists.py`. That's a small layering compromise — alternative would be exporting it. Since both are repos and stay together, the import is acceptable. Document this if it grows uncomfortable later.

No spec gaps detected.
