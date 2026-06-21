# Speaker Chat — PR 1: Schema, Seeds & Show Matching — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the database foundation for the "Chat with Speakers" feature — all new tables, the `videos` rebuild, seeded known-shows/known-speakers, and the deterministic metadata-based speaker detection — with nothing user-facing yet.

**Architecture:** SQLite schema lives in `app/db.py` (a `SCHEMA` executescript for fresh installs + an idempotent `_run_migrations` for upgrades). This PR adds the speaker tables to `SCHEMA`, rebuilds `videos` once to widen its `kind` CHECK and add `channel_id`, ships two JSON seed files loaded behind `settings` version markers, captures `channel_id` from yt-dlp, and implements `show_match.identify_from_metadata` (pure string/pattern work, no LLM). Repos and models expose the interfaces PR 2–4 consume.

**Tech Stack:** Python 3.12, aiosqlite, FastAPI (not touched here), pytest + pytest-asyncio. House test style: in-memory SQLite via the `db` fixture, no network, no browser.

## Global Constraints

- Python ≥ 3.12; use `StrEnum` for enums and `@dataclass` for records (matches `app/models.py`).
- All repo functions take `db: aiosqlite.Connection` as the first positional arg and act as `user_id=1` by default (matches `app/repos/settings.py`, `app/repos/chat.py`).
- Migrations must be **idempotent**: `init_schema` runs `_run_migrations` then `executescript(SCHEMA)`; both must survive running twice on the same DB. Gate every migration step on a feature check (`_table_exists` / `_table_columns`).
- No seeded **positions** — `known_speakers.style_note` describes speaking style only, never beliefs/claims.
- `known_shows.guest_rule` is an enumerated tag (`after:<sep>` / `before:<sep>` / NULL), never executable code.
- Commit after every green test. Branch base: `docs/speaker-chat-v1_5-merge` (or a fresh `feat/speaker-chat-pr1`).
- Source of truth: [`docs/superpowers/specs/2026-06-21-chat-with-speakers-v1_5-design.md`](../specs/2026-06-21-chat-with-speakers-v1_5-design.md).

---

## File Structure

- `app/db.py` — **modify**: add new tables to `SCHEMA`; add the `videos` table-rebuild migration; add `channel_id`.
- `app/models.py` — **modify**: add `VideoKind.TEXT`; add `Speaker`, `KnownShow`, `DetectedSpeaker` dataclasses.
- `app/data/known_shows.json` — **create**: seeded show rules.
- `app/data/known_speakers.json` — **create**: seeded speaker directory (identity only).
- `app/services/seed.py` — **create**: idempotent JSON seed loaders gated by `settings` version markers.
- `app/services/show_match.py` — **create**: `identify_from_metadata` + `guest_rule` parser.
- `app/services/youtube.py` — **modify**: capture `info["channel_id"]`.
- `app/repos/speakers.py` — **create**: `resolve_speaker`, CRUD, `name_key` normalisation.
- `app/repos/known_shows.py` — **create**: list/CRUD for show rules.
- `tests/test_db_speaker_schema.py`, `tests/test_seed.py`, `tests/test_show_match.py`, `tests/test_repos_speakers.py` — **create**.

---

## Interfaces this PR PRODUCES (PR 2–4 depend on these exact signatures)

```python
# app/models.py
class VideoKind(StrEnum):
    YOUTUBE = "youtube"; WEB = "web"; EMAIL = "email"; TEXT = "text"

@dataclass
class Speaker:
    id: int
    user_id: int
    known_speaker_id: int | None
    name: str
    name_key: str
    role: str | None
    avatar_id: str | None
    avatar_photo_path: str | None
    style_note: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

@dataclass
class DetectedSpeaker:
    name: str
    role: str | None          # role in this source, if known
    is_host: bool

# app/repos/speakers.py
def normalize_name_key(name: str) -> str: ...        # lower, punct/space collapsed
async def resolve_speaker(db, *, user_id: int = 1, name: str, role: str | None = None) -> int: ...   # returns speaker_id (upsert on (user_id, name_key))
async def get_speaker(db, speaker_id: int) -> Speaker | None: ...
async def list_for_user(db, *, user_id: int = 1, active_only: bool = False) -> list[Speaker]: ...
async def set_active(db, speaker_id: int, active: bool) -> None: ...

# app/repos/known_shows.py
async def list_enabled(db, *, user_id: int = 1) -> list[KnownShow]: ...

# app/services/show_match.py
async def identify_from_metadata(db, video) -> list[DetectedSpeaker]: ...

# app/services/seed.py
async def seed_known_shows(db) -> None: ...          # idempotent, behind settings marker 'known_shows_seed_version'
async def seed_known_speakers(db) -> None: ...       # idempotent, behind settings marker 'known_speakers_seed_version'
```

---

### Task 1: Add `VideoKind.TEXT` to models

**Files:**
- Modify: `app/models.py` (the `VideoKind` StrEnum near line 15)
- Test: `tests/test_db_speaker_schema.py`

**Interfaces:**
- Produces: `VideoKind.TEXT == "text"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_speaker_schema.py
from app.models import VideoKind


def test_videokind_has_text():
    assert VideoKind.TEXT == "text"
    assert VideoKind("text") is VideoKind.TEXT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_speaker_schema.py::test_videokind_has_text -v`
Expected: FAIL — `AttributeError: TEXT` (or ValueError on `VideoKind("text")`).

- [ ] **Step 3: Add the enum member**

In `app/models.py`, in `class VideoKind(StrEnum)`, add after the `EMAIL` line:

```python
    TEXT = "text"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_db_speaker_schema.py::test_videokind_has_text -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_db_speaker_schema.py
git commit -m "feat(speakers): add VideoKind.TEXT"
```

---

### Task 2: Rebuild `videos` — widen `kind` CHECK + add `channel_id`

SQLite cannot `ALTER` a column's `CHECK` in place. We rebuild `videos` once, reusing the `settings`/`feedback` rebuild pattern already in `db.py`. The rebuild runs only when needed (old CHECK present **or** `channel_id` missing), and is idempotent.

**Files:**
- Modify: `app/db.py` — the `SCHEMA` `CREATE TABLE videos` CHECK (line ~17) and the migration body in `_run_migrations` (the `videos` block near line 336).
- Test: `tests/test_db_speaker_schema.py`

**Interfaces:**
- Produces: `videos.kind` accepts `'text'`; `videos.channel_id` column exists.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_db_speaker_schema.py (append)
import asyncio
import aiosqlite
from app.db import connect, init_schema
from app.config import Config


def _fresh_db(tmp_path):
    cfg = Config(data_dir=tmp_path); cfg.ensure_dirs()
    async def go():
        conn = await connect(cfg)
        await init_schema(conn)
        return conn
    return asyncio.get_event_loop().run_until_complete(go())


def test_videos_accepts_text_kind(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        await conn.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('text-1', 1, 'text', '', 'pasted')"
        )
        await conn.commit()
        cur = await conn.execute("SELECT kind FROM videos WHERE id='text-1'")
        row = await cur.fetchone()
        assert row[0] == "text"
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())


def test_videos_has_channel_id(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        cur = await conn.execute("PRAGMA table_info(videos)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "channel_id" in cols
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_db_speaker_schema.py -k "text_kind or channel_id" -v`
Expected: FAIL — insert hits the old CHECK; `channel_id` absent.

- [ ] **Step 3: Update SCHEMA**

In `app/db.py`, change the `videos` `CREATE TABLE` CHECK to include `text`, and add the column:

```sql
    kind TEXT NOT NULL DEFAULT 'youtube'
        CHECK(kind IN ('youtube','web','email','text')),
```
Add (place it near `youtube_id`, anywhere in the column list):
```sql
    channel_id TEXT,
```

- [ ] **Step 4: Add the rebuild migration**

In `_run_migrations`, inside the `if await _table_exists(conn, "videos"):` block, after the existing `_ensure_column` calls, add:

```python
        # The kind CHECK gained 'text'. SQLite can't ALTER a CHECK in place,
        # so rebuild the table once (same pattern as settings/feedback below).
        # Trigger the rebuild when channel_id is missing — that column is added
        # in the SAME rebuild, so its absence is a reliable "not yet rebuilt"
        # signal. (We can't cheaply introspect the CHECK text, but channel_id
        # presence is a faithful proxy.)
        if "channel_id" not in video_cols:
            await conn.execute("PRAGMA foreign_keys=OFF")
            await conn.executescript(
                """
                CREATE TABLE videos_new (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL DEFAULT 1,
                    kind TEXT NOT NULL DEFAULT 'youtube'
                        CHECK(kind IN ('youtube','web','email','text')),
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    thumbnail_path TEXT,
                    duration_seconds INTEGER,
                    transcript TEXT,
                    transcript_segments TEXT,
                    transcript_source TEXT,
                    summary TEXT,
                    summary_model TEXT,
                    summary_embedded_at TEXT,
                    youtube_id TEXT,
                    source_language TEXT,
                    summary_language TEXT,
                    transcript_language TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    archived_at TEXT,
                    highlights_json TEXT,
                    image_query TEXT,
                    related_links_json TEXT,
                    channel_id TEXT
                );
                INSERT INTO videos_new (
                    id, user_id, kind, url, title, description, thumbnail_path,
                    duration_seconds, transcript, transcript_segments,
                    transcript_source, summary, summary_model, summary_embedded_at,
                    youtube_id, source_language, summary_language,
                    transcript_language, created_at, updated_at, archived_at,
                    highlights_json, image_query, related_links_json
                )
                SELECT
                    id, user_id, kind, url, title, description, thumbnail_path,
                    duration_seconds, transcript, transcript_segments,
                    transcript_source, summary, summary_model, summary_embedded_at,
                    youtube_id, source_language, summary_language,
                    transcript_language, created_at, updated_at, archived_at,
                    highlights_json, image_query, related_links_json
                FROM videos;
                DROP TABLE videos;
                ALTER TABLE videos_new RENAME TO videos;
                """
            )
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.commit()
```

> **Note on column list:** copy the column list from the *current* `SCHEMA` `CREATE TABLE videos` at implementation time — if the live schema has columns not listed above, add them to both the `videos_new` DDL and the INSERT/SELECT, or the rebuild drops data. Verify with `PRAGMA table_info(videos)` against a fresh `init_schema` DB before running on real data.

- [ ] **Step 5: Run to verify pass + idempotency**

Run: `.venv/bin/pytest tests/test_db_speaker_schema.py -k "text_kind or channel_id" -v`
Expected: PASS.

Add and run an idempotency test:

```python
# tests/test_db_speaker_schema.py (append)
from app.db import _run_migrations  # noqa


def test_migrations_run_twice_clean(tmp_path):
    cfg = Config(data_dir=tmp_path); cfg.ensure_dirs()
    async def go():
        conn = await connect(cfg)
        await init_schema(conn)
        await _run_migrations(conn)   # second pass
        await init_schema(conn)       # third pass
        cur = await conn.execute("PRAGMA table_info(videos)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "channel_id" in cols
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())
```

Run: `.venv/bin/pytest tests/test_db_speaker_schema.py::test_migrations_run_twice_clean -v`
Expected: PASS.

- [ ] **Step 6: Run the full existing suite to prove no regression**

Run: `.venv/bin/pytest tests/test_repos_videos.py tests/test_routes_chat.py -q`
Expected: PASS (the rebuild preserves `videos` for all existing consumers).

- [ ] **Step 7: Commit**

```bash
git add app/db.py tests/test_db_speaker_schema.py
git commit -m "feat(speakers): rebuild videos for kind='text' + channel_id"
```

---

### Task 3: Create the speaker tables in SCHEMA

All new tables go into the `SCHEMA` executescript (fresh installs) AND must be created on upgrade. Because they are brand-new tables (not column changes), a single `CREATE TABLE IF NOT EXISTS` in `SCHEMA` covers both paths — `init_schema` runs `SCHEMA` on every boot, so existing DBs gain the tables idempotently. No `_run_migrations` entry needed.

**Files:**
- Modify: `app/db.py` — append to `SCHEMA`.
- Test: `tests/test_db_speaker_schema.py`

**Interfaces:**
- Produces: tables `known_shows`, `known_speakers`, `speakers`, `source_speakers`, `speaker_source_candidates`, `speaker_claims`, `chat_threads`; `chat_messages.thread_id` (new column) and `chat_messages.video_id` **made nullable** (so `scope='speaker'` rows persist with `video_id=NULL`); the three `chat_threads` partial unique indexes. (`speaker_claim_embeddings` is deferred to PR 4.)

  **Contract for PR 2/3:** whole-dossier (`scope='speaker'`) chat rows are keyed by `thread_id` and carry `video_id=NULL`. PR 2's thread-aware `chat_repo` must pass `None` for `video_id` on speaker-scope rows — never `''`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_speaker_schema.py (append)
EXPECTED_TABLES = {
    "known_shows", "known_speakers", "speakers", "source_speakers",
    "speaker_source_candidates", "speaker_claims", "chat_threads",
}

def test_speaker_tables_exist(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in await cur.fetchall()}
        assert EXPECTED_TABLES <= names
        cur = await conn.execute("PRAGMA table_info(chat_messages)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "thread_id" in cols
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())


def test_chat_threads_partial_unique_blocks_dupe_speaker_thread(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        await conn.execute("INSERT INTO speakers (user_id, name, name_key) VALUES (1,'X','x')")
        await conn.execute("INSERT INTO chat_threads (user_id, scope, speaker_id) VALUES (1,'speaker',1)")
        await conn.commit()
        import aiosqlite as _a
        raised = False
        try:
            await conn.execute("INSERT INTO chat_threads (user_id, scope, speaker_id) VALUES (1,'speaker',1)")
            await conn.commit()
        except _a.IntegrityError:
            raised = True
        assert raised, "partial unique index must block a duplicate speaker thread"
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_db_speaker_schema.py -k "speaker_tables or partial_unique" -v`
Expected: FAIL — tables/index absent.

- [ ] **Step 3: Append the DDL to SCHEMA**

Add to the `SCHEMA` string in `app/db.py` (copy the table definitions verbatim from the spec's Data model section; `chat_messages.thread_id` is added as a column in the `chat_messages` CREATE plus the partial indexes). Use the exact DDL from the spec for: `known_shows`, `known_speakers`, `speakers`, `source_speakers`, `speaker_source_candidates`, `speaker_claims`, `chat_threads`, the three `CREATE UNIQUE INDEX … WHERE scope=…` partial indexes, and `ALTER`-free `thread_id` on the `chat_messages` CREATE TABLE.

> **`chat_messages` needs a rebuild, not just `ADD COLUMN thread_id`.** A
> `scope='speaker'` (whole-dossier) chat row has NO video, but today
> `chat_messages.video_id` is `TEXT NOT NULL REFERENCES videos(id)`. A
> placeholder `''` violates the FK; `NULL` violates `NOT NULL`. So this PR makes
> `video_id` **nullable** (the spec keeps `video_id` "for compatibility until the
> repo layer is fully thread-based" — nullable is exactly that). SQLite can't
> drop `NOT NULL` via `ALTER`, so rebuild `chat_messages` once (same pattern as
> the `videos` rebuild in Task 2), adding BOTH `thread_id` and the relaxed
> `video_id` in the one rebuild. In `SCHEMA`, the fresh `CREATE TABLE
> chat_messages` is:
>
> ```sql
> CREATE TABLE IF NOT EXISTS chat_messages (
>     id INTEGER PRIMARY KEY AUTOINCREMENT,
>     user_id INTEGER NOT NULL DEFAULT 1,
>     video_id TEXT REFERENCES videos(id),          -- nullable now (speaker-scope threads have no video)
>     role TEXT NOT NULL CHECK(role IN ('user','assistant')),
>     content TEXT NOT NULL,
>     thread_id INTEGER REFERENCES chat_threads(id),
>     created_at TEXT NOT NULL DEFAULT (datetime('now'))
> );
> CREATE INDEX IF NOT EXISTS idx_chat_video_created ON chat_messages(video_id, created_at);
> CREATE INDEX IF NOT EXISTS idx_chat_thread_created ON chat_messages(thread_id, created_at);
> ```
>
> In `_run_migrations`, in the existing `if await _table_exists(conn,
> "chat_messages"):` block (near line 383), AFTER the existing `user_id`/
> `created_at` guards, add a rebuild gated on `thread_id` absence:
>
> ```python
>         if "thread_id" not in chat_cols:
>             await conn.execute("PRAGMA foreign_keys=OFF")
>             await conn.executescript(
>                 """
>                 CREATE TABLE chat_messages_new (
>                     id INTEGER PRIMARY KEY AUTOINCREMENT,
>                     user_id INTEGER NOT NULL DEFAULT 1,
>                     video_id TEXT REFERENCES videos(id),
>                     role TEXT NOT NULL CHECK(role IN ('user','assistant')),
>                     content TEXT NOT NULL,
>                     thread_id INTEGER REFERENCES chat_threads(id),
>                     created_at TEXT NOT NULL DEFAULT (datetime('now'))
>                 );
>                 INSERT INTO chat_messages_new (id, user_id, video_id, role, content, created_at)
>                 SELECT id, user_id, video_id, role, content, created_at FROM chat_messages;
>                 DROP TABLE chat_messages;
>                 ALTER TABLE chat_messages_new RENAME TO chat_messages;
>                 """
>             )
>             await conn.execute("PRAGMA foreign_keys=ON")
>             await conn.commit()
> ```
>
> NOTE: `chat_threads` must be created BEFORE this migration runs (the
> `thread_id` FK references it). Since `_run_migrations` runs before
> `executescript(SCHEMA)`, either create `chat_threads` inside the migration too,
> or drop the FK clause in the rebuilt table and rely on SCHEMA's fresh table for
> new installs. Simplest: in the rebuild DDL above, keep `thread_id INTEGER`
> WITHOUT the `REFERENCES chat_threads(id)` clause (the column type is what
> matters for upgrades; fresh installs via SCHEMA get the full FK). Document this.

> **`speaker_claim_embeddings`** (sqlite-vec) is deferred to PR 4 — do NOT create it here. PR 4 owns the vec table to keep this PR free of the vec extension dependency at schema time.

- [ ] **Step 4: Add a test proving `video_id` is nullable**

```python
# tests/test_db_speaker_schema.py (append)
def test_chat_messages_video_id_nullable(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        # a thread-scoped row with NO video must be insertable
        await conn.execute("INSERT INTO speakers (user_id, name, name_key) VALUES (1,'X','x')")
        await conn.execute("INSERT INTO chat_threads (user_id, scope, speaker_id) VALUES (1,'speaker',1)")
        await conn.execute(
            "INSERT INTO chat_messages (user_id, video_id, role, content, thread_id) "
            "VALUES (1, NULL, 'user', 'hi', 1)"
        )
        await conn.commit()
        cur = await conn.execute("SELECT video_id, thread_id FROM chat_messages WHERE thread_id=1")
        row = await cur.fetchone()
        assert row[0] is None and row[1] == 1
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_db_speaker_schema.py -k "speaker_tables or partial_unique or video_id_nullable" -v`
Expected: PASS.

- [ ] **Step 6: Run existing chat tests (the rebuild must preserve them)**

Run: `.venv/bin/pytest tests/test_repos_chat.py tests/test_routes_chat.py -q`
Expected: PASS — the rebuild keeps all existing `chat_messages` rows + behaviour.

- [ ] **Step 7: Commit**

```bash
git add app/db.py tests/test_db_speaker_schema.py
git commit -m "feat(speakers): speaker tables + chat_threads + chat_messages rebuild (thread_id, nullable video_id)"
```

---

### Task 4: `name_key` normalisation + `resolve_speaker`

**Files:**
- Create: `app/repos/speakers.py`
- Modify: `app/models.py` — add the `Speaker` dataclass.
- Test: `tests/test_repos_speakers.py`

**Interfaces:**
- Consumes: the `speakers` table (Task 3), `Speaker` model.
- Produces: `normalize_name_key`, `resolve_speaker`, `get_speaker`, `list_for_user`, `set_active` (signatures in the PRODUCES block above).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repos_speakers.py
import asyncio
from app.repos import speakers as repo


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_normalize_name_key():
    assert repo.normalize_name_key("Chamath  Palihapitiya!") == "chamath palihapitiya"
    assert repo.normalize_name_key("CHAMATH") == "chamath"


def test_resolve_speaker_upserts_same_person(db):
    async def go():
        a = await repo.resolve_speaker(db, name="Chamath Palihapitiya")
        b = await repo.resolve_speaker(db, name="chamath  palihapitiya")  # same key
        c = await repo.resolve_speaker(db, name="Jason Calacanis")
        assert a == b
        assert a != c
        sp = await repo.get_speaker(db, a)
        assert sp.name == "Chamath Palihapitiya"   # first spelling wins
        assert sp.is_active is False
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_repos_speakers.py -v`
Expected: FAIL — module/functions don't exist.

- [ ] **Step 3: Implement the repo**

```python
# app/repos/speakers.py
import re
from datetime import datetime

import aiosqlite

from app.models import Speaker

_DEFAULT_USER = 1
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_name_key(name: str) -> str:
    s = _PUNCT.sub(" ", name.lower())
    return _WS.sub(" ", s).strip()


def _row_to_speaker(row: aiosqlite.Row) -> Speaker:
    return Speaker(
        id=row["id"], user_id=row["user_id"],
        known_speaker_id=row["known_speaker_id"],
        name=row["name"], name_key=row["name_key"], role=row["role"],
        avatar_id=row["avatar_id"], avatar_photo_path=row["avatar_photo_path"],
        style_note=row["style_note"], is_active=bool(row["is_active"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


async def resolve_speaker(
    db: aiosqlite.Connection, *, user_id: int = _DEFAULT_USER,
    name: str, role: str | None = None,
) -> int:
    key = normalize_name_key(name)
    cur = await db.execute(
        "SELECT id FROM speakers WHERE user_id=? AND name_key=?", (user_id, key)
    )
    row = await cur.fetchone()
    if row is not None:
        return row["id"]
    cur = await db.execute(
        "INSERT INTO speakers (user_id, name, name_key, role) VALUES (?,?,?,?)",
        (user_id, name, key, role),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def get_speaker(db: aiosqlite.Connection, speaker_id: int) -> Speaker | None:
    cur = await db.execute("SELECT * FROM speakers WHERE id=?", (speaker_id,))
    row = await cur.fetchone()
    return _row_to_speaker(row) if row else None


async def list_for_user(
    db: aiosqlite.Connection, *, user_id: int = _DEFAULT_USER,
    active_only: bool = False,
) -> list[Speaker]:
    q = "SELECT * FROM speakers WHERE user_id=?"
    if active_only:
        q += " AND is_active=1"
    q += " ORDER BY name COLLATE NOCASE"
    cur = await db.execute(q, (user_id,))
    return [_row_to_speaker(r) for r in await cur.fetchall()]


async def set_active(db: aiosqlite.Connection, speaker_id: int, active: bool) -> None:
    await db.execute(
        "UPDATE speakers SET is_active=?, updated_at=datetime('now') WHERE id=?",
        (1 if active else 0, speaker_id),
    )
    await db.commit()
```

Add the `Speaker` dataclass to `app/models.py` (import `datetime` is already present):

```python
@dataclass
class Speaker:
    id: int
    user_id: int
    known_speaker_id: int | None
    name: str
    name_key: str
    role: str | None
    avatar_id: str | None
    avatar_photo_path: str | None
    style_note: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

> The `db` fixture connects with `aiosqlite` — ensure rows are dict-accessible. `app/repos/chat.py` already relies on `row["col"]`, so the connection sets `row_factory = aiosqlite.Row` in `connect()`. No change needed.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_repos_speakers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/repos/speakers.py app/models.py tests/test_repos_speakers.py
git commit -m "feat(speakers): resolve_speaker repo + name_key normalisation"
```

---

### Task 5: `guest_rule` parser + `show_match.identify_from_metadata`

**Files:**
- Create: `app/services/show_match.py`
- Create: `app/repos/known_shows.py`
- Modify: `app/models.py` — add `KnownShow`, `DetectedSpeaker`.
- Test: `tests/test_show_match.py`

**Interfaces:**
- Consumes: `known_shows` table, `Video` model (has `.channel_id`, `.title`, `.description`).
- Produces: `identify_from_metadata`, `KnownShow`, `DetectedSpeaker`, `known_shows.list_enabled`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_show_match.py
import asyncio
from app.services import show_match
from app.models import Video, VideoKind


def _run(c): return asyncio.get_event_loop().run_until_complete(c)


def _video(**kw):
    base = dict(id="v1", user_id=1, kind=VideoKind.YOUTUBE, url="", title="", description="")
    base.update(kw)
    return Video(**base)


def test_parse_guest_after():
    assert show_match._parse_guest("after:with ", "Money with Morgan Housel") == "Morgan Housel"


def test_parse_guest_before():
    assert show_match._parse_guest("before:: ", "Elon Musk: Mars | Lex #1") == "Elon Musk"


def test_identify_matches_channel_and_parses_guest(db):
    async def go():
        await db.execute(
            "INSERT INTO known_shows (user_id, name, channel_id, hosts_json, guest_rule, enabled) "
            "VALUES (NULL, 'Lex Fridman Podcast', 'UCchan', '[\"Lex Fridman\"]', 'before:: ', 1)"
        )
        await db.commit()
        v = _video(channel_id="UCchan", title="Elon Musk: Mars | Lex Fridman Podcast #1")
        out = await show_match.identify_from_metadata(db, v)
        names = {d.name for d in out}
        assert "Lex Fridman" in names and "Elon Musk" in names
        host = next(d for d in out if d.name == "Lex Fridman")
        assert host.is_host is True
    _run(go())


def test_identify_no_match_returns_empty(db):
    async def go():
        v = _video(channel_id="UNKNOWN", title="random")
        assert await show_match.identify_from_metadata(db, v) == []
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_show_match.py -v`
Expected: FAIL — module/functions absent. (If `Video` lacks `channel_id`, add the field to the `Video` dataclass in `app/models.py` with default `None` — Task 2 added the column.)

- [ ] **Step 3: Add models**

```python
# app/models.py
@dataclass
class KnownShow:
    id: int
    user_id: int | None
    name: str
    channel_id: str | None
    title_pattern: str | None
    description_pattern: str | None
    hosts_json: str
    guest_rule: str | None
    enabled: bool

@dataclass
class DetectedSpeaker:
    name: str
    role: str | None
    is_host: bool
```

Also add `channel_id: str | None = None` to the `Video` dataclass if not already present.

- [ ] **Step 4: Implement the repo + service**

```python
# app/repos/known_shows.py
import aiosqlite
from app.models import KnownShow

def _row(r) -> KnownShow:
    return KnownShow(
        id=r["id"], user_id=r["user_id"], name=r["name"],
        channel_id=r["channel_id"], title_pattern=r["title_pattern"],
        description_pattern=r["description_pattern"], hosts_json=r["hosts_json"],
        guest_rule=r["guest_rule"], enabled=bool(r["enabled"]),
    )

async def list_enabled(db: aiosqlite.Connection, *, user_id: int = 1) -> list[KnownShow]:
    # Shipped rows (user_id IS NULL) + this profile's own rows.
    cur = await db.execute(
        "SELECT * FROM known_shows WHERE enabled=1 AND (user_id IS NULL OR user_id=?)",
        (user_id,),
    )
    return [_row(r) for r in await cur.fetchall()]
```

```python
# app/services/show_match.py
import json
from app.models import DetectedSpeaker
from app.repos import known_shows as shows_repo


def _parse_guest(rule: str | None, text: str) -> str | None:
    """Enumerated guest parser. rule is 'after:<sep>' or 'before:<sep>' or None."""
    if not rule or not text:
        return None
    mode, _, sep = rule.partition(":")
    if not sep:
        return None
    if mode == "after":
        idx = text.lower().find(sep.lower())
        if idx == -1:
            return None
        return text[idx + len(sep):].strip() or None
    if mode == "before":
        idx = text.find(sep)
        if idx == -1:
            return None
        return text[:idx].strip() or None
    return None


def _matches(show, video) -> bool:
    if show.channel_id and video.channel_id and show.channel_id == video.channel_id:
        return True
    if show.title_pattern and show.title_pattern.lower() in (video.title or "").lower():
        return True
    if show.description_pattern and show.description_pattern.lower() in (video.description or "").lower():
        return True
    return False


async def identify_from_metadata(db, video) -> list[DetectedSpeaker]:
    out: list[DetectedSpeaker] = []
    seen: set[str] = set()
    for show in await shows_repo.list_enabled(db, user_id=video.user_id):
        if not _matches(show, video):
            continue
        for host in json.loads(show.hosts_json or "[]"):
            if host.lower() not in seen:
                seen.add(host.lower())
                out.append(DetectedSpeaker(name=host, role="host", is_host=True))
        guest = _parse_guest(show.guest_rule, video.title) or _parse_guest(show.guest_rule, video.description)
        if guest and guest.lower() not in seen:
            seen.add(guest.lower())
            out.append(DetectedSpeaker(name=guest, role="guest", is_host=False))
        break  # first matching show wins
    return out
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_show_match.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/show_match.py app/repos/known_shows.py app/models.py tests/test_show_match.py
git commit -m "feat(speakers): show_match.identify_from_metadata + guest_rule parser"
```

---

### Task 6: Seed files + idempotent loaders

**Files:**
- Create: `app/data/known_shows.json`, `app/data/known_speakers.json`
- Create: `app/services/seed.py`
- Test: `tests/test_seed.py`

**Interfaces:**
- Consumes: `known_shows`/`known_speakers` tables, `app/repos/settings.py` (`get`/`set`).
- Produces: `seed_known_shows`, `seed_known_speakers` (idempotent, version-gated).

- [ ] **Step 1: Create the seed files**

```json
// app/data/known_shows.json
{
  "version": 1,
  "shows": [
    {"name": "Lex Fridman Podcast", "channel_id": "UCSHZKyawb77ixDdsGog4iWA",
     "title_pattern": "Lex Fridman Podcast", "hosts": ["Lex Fridman"], "guest_rule": "before:: "},
    {"name": "The Diary Of A CEO", "channel_id": null,
     "title_pattern": "The Diary Of A CEO", "hosts": ["Steven Bartlett"], "guest_rule": "after:with "},
    {"name": "All-In Podcast", "channel_id": null, "title_pattern": "All-In",
     "hosts": ["Chamath Palihapitiya", "Jason Calacanis", "David Sacks", "David Friedberg"], "guest_rule": null}
  ]
}
```

```json
// app/data/known_speakers.json
{
  "version": 1,
  "speakers": [
    {"name": "Lex Fridman", "role": "host, AI researcher", "known_shows": "Lex Fridman Podcast",
     "avatar_id": "adult-scientist-m", "style_note": "calm, earnest, long-form, asks big questions"},
    {"name": "Chamath Palihapitiya", "role": "investor, All-In co-host", "known_shows": "All-In",
     "avatar_id": "adult-techreviewer-m", "style_note": "blunt, contrarian, fast-moving investor tone"}
  ]
}
```

> Keep `style_note` to *style only* — no positions/beliefs. Extend the lists later; the loader re-imports on a version bump.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_seed.py
import asyncio
from app.services import seed
from app.repos import settings as settings_repo


def _run(c): return asyncio.get_event_loop().run_until_complete(c)


def test_seed_shows_idempotent(db):
    async def go():
        await seed.seed_known_shows(db)
        await seed.seed_known_shows(db)   # second call must not duplicate
        cur = await db.execute("SELECT COUNT(*) FROM known_shows WHERE user_id IS NULL")
        n = (await cur.fetchone())[0]
        assert n >= 3
        # marker set
        assert await settings_repo.get(db, "known_shows_seed_version") == "1"
        # no duplication
        cur = await db.execute("SELECT name, COUNT(*) c FROM known_shows GROUP BY name HAVING c>1")
        assert await cur.fetchone() is None
    _run(go())


def test_seed_speakers_idempotent(db):
    async def go():
        await seed.seed_known_speakers(db)
        await seed.seed_known_speakers(db)
        cur = await db.execute("SELECT COUNT(*) FROM known_speakers")
        assert (await cur.fetchone())[0] >= 2
    _run(go())
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_seed.py -v`
Expected: FAIL — module absent.

- [ ] **Step 4: Implement the loader**

```python
# app/services/seed.py
import json
from pathlib import Path

import aiosqlite

from app.repos import settings as settings_repo

_DATA = Path(__file__).resolve().parent.parent / "data"


async def _seed(db, *, file: str, marker: str, table: str, insert) -> None:
    payload = json.loads((_DATA / file).read_text(encoding="utf-8"))
    version = str(payload.get("version", 1))
    if await settings_repo.get(db, marker) == version:
        return
    # Explicit key — `payload.keys() & {...}` returns a set, which is NOT
    # subscriptable (and set-ordering is not a contract). Pick the one real key.
    items_key = "shows" if "shows" in payload else "speakers"
    # UPSERT, never wipe. A DELETE on known_speakers would hit a FOREIGN KEY
    # constraint the moment any profile speaker.known_speaker_id references a
    # row (nullable != ON DELETE SET NULL — verified: SQLite raises
    # "FOREIGN KEY constraint failed"). Upsert keys are: known_shows.name,
    # known_speakers.name_key. Both must have a UNIQUE for ON CONFLICT to work.
    for item in payload[items_key]:
        await insert(db, item, version)
    await settings_repo.set(db, marker, version)
    await db.commit()


async def seed_known_shows(db: aiosqlite.Connection) -> None:
    async def ins(db, s, version):
        # Upsert shipped rows on (name). Requires a UNIQUE on known_shows.name
        # for seeded rows — add `CREATE UNIQUE INDEX IF NOT EXISTS
        # uq_known_shows_seed_name ON known_shows(name) WHERE user_id IS NULL`
        # to SCHEMA (partial: user rows may share a name).
        await db.execute(
            "INSERT INTO known_shows (user_id, name, channel_id, title_pattern, "
            "hosts_json, guest_rule, seed_version) VALUES (NULL,?,?,?,?,?,?) "
            "ON CONFLICT(name) WHERE user_id IS NULL DO UPDATE SET "
            "channel_id=excluded.channel_id, title_pattern=excluded.title_pattern, "
            "hosts_json=excluded.hosts_json, guest_rule=excluded.guest_rule, "
            "seed_version=excluded.seed_version",
            (s["name"], s.get("channel_id"), s.get("title_pattern"),
             json.dumps(s.get("hosts", [])), s.get("guest_rule"), int(version)),
        )
    await _seed(db, file="known_shows.json", marker="known_shows_seed_version",
                table="known_shows", insert=ins)


async def seed_known_speakers(db: aiosqlite.Connection) -> None:
    async def ins(db, s, version):
        # Upsert on name_key (already UNIQUE in the table). No wipe → no FK break.
        await db.execute(
            "INSERT INTO known_speakers (name, name_key, role, known_shows, "
            "avatar_id, style_note, seed_version) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(name_key) DO UPDATE SET "
            "name=excluded.name, role=excluded.role, known_shows=excluded.known_shows, "
            "avatar_id=excluded.avatar_id, style_note=excluded.style_note, "
            "seed_version=excluded.seed_version",
            (s["name"], _key(s["name"]), s.get("role"), s.get("known_shows"),
             s.get("avatar_id"), s.get("style_note"), int(version)),
        )
    await _seed(db, file="known_speakers.json", marker="known_speakers_seed_version",
                table="known_speakers", insert=ins)


def _key(name: str) -> str:
    from app.repos.speakers import normalize_name_key
    return normalize_name_key(name)
```

> **No wipe-on-reseed (verified bug fix).** Earlier this used `DELETE FROM
> known_speakers`, claimed safe because the FK is nullable. It is NOT: a nullable
> FK is not `ON DELETE SET NULL`, so the DELETE raises `FOREIGN KEY constraint
> failed` once any profile speaker links to a seeded row. The loader now UPSERTs
> on the natural key (`known_speakers.name_key` UNIQUE; `known_shows.name` via a
> partial UNIQUE on seeded rows), which re-imports on a version bump without ever
> deleting a referenced parent. As defence-in-depth, ALSO add `ON DELETE SET
> NULL` to the `speakers.known_speaker_id` FK in SCHEMA (and the spec), so even a
> future hard delete degrades gracefully:
> `known_speaker_id INTEGER REFERENCES known_speakers(id) ON DELETE SET NULL`.

> **Add to Task 3's SCHEMA DDL** (so the upserts above have their conflict
> targets): `CREATE UNIQUE INDEX IF NOT EXISTS uq_known_shows_seed_name ON
> known_shows(name) WHERE user_id IS NULL;` and ensure `speakers.known_speaker_id`
> carries `ON DELETE SET NULL`.

- [ ] **Step 5: Wire seeding into boot**

In `app/db.py`'s `init_schema` (after `SCHEMA` runs), call the seeders. Find where `init_schema` finishes and add:

```python
    from app.services import seed
    await seed.seed_known_shows(conn)
    await seed.seed_known_speakers(conn)
```

> Verify `init_schema`'s signature/location at implementation time; if seeding-on-every-boot is undesirable in tests, the version marker already makes the second+ call a no-op, so it's cheap.

- [ ] **Step 6: Run to verify pass + full suite**

Run: `.venv/bin/pytest tests/test_seed.py -v && .venv/bin/pytest -q`
Expected: PASS; existing suite green.

- [ ] **Step 7: Commit**

```bash
git add app/data/known_shows.json app/data/known_speakers.json app/services/seed.py app/db.py tests/test_seed.py
git commit -m "feat(speakers): seed known_shows + known_speakers (idempotent, versioned)"
```

---

### Task 7: Capture `channel_id` from yt-dlp

**Files:**
- Modify: `app/services/youtube.py` (the `VideoMeta`/`Video` construction near line 97).
- Test: `tests/test_youtube_channel_id.py`

**Interfaces:**
- Produces: ingested YouTube videos carry `channel_id` from `info["channel_id"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_youtube_channel_id.py
from app.services import youtube


def test_metadata_captures_channel_id():
    info = {"id": "abc", "title": "t", "description": "d",
            "channel_id": "UCxyz", "webpage_url": "https://y/abc"}
    meta = youtube._meta_from_info(info, "https://y/abc")  # adjust to real fn name
    assert meta.channel_id == "UCxyz"
```

> At implementation time, find the actual function that builds the metadata object from `info` (around line 97 it constructs with `id=info["id"]`, `title=…`). Mirror its real name/shape in the test. If it returns a `Video`/`VideoMeta`, assert the channel field there; if `channel_id` isn't a field on that object yet, add it.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_youtube_channel_id.py -v`
Expected: FAIL — field not populated.

- [ ] **Step 3: Add the field read**

In the metadata construction, add:

```python
        channel_id=info.get("channel_id"),
```

and ensure `channel_id` is persisted by the ingest path that writes the `videos` row (follow the existing `description`/`title` write).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_youtube_channel_id.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/youtube.py tests/test_youtube_channel_id.py
git commit -m "feat(speakers): capture channel_id from yt-dlp info"
```

---

## PR 1 done-criteria

- `.venv/bin/pytest -q` fully green (new + existing).
- A fresh DB and an upgraded DB both end with the speaker tables, the rebuilt `videos`, the partial unique indexes, and the seeded shows/speakers.
- `show_match.identify_from_metadata` returns hosts + parsed guests for a seeded show, `[]` otherwise.
- No UI, no LLM, no pipeline wiring yet — those are PR 2/3.
