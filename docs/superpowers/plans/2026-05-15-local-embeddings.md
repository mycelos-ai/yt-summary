# Local Embeddings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace LiteLLM-based embeddings with a local `sentence-transformers` model (`paraphrase-multilingual-MiniLM-L12-v2`, 384d), remove embedding configuration from Settings, and re-embed existing videos in the background via the scheduler.

**Architecture:** New `app/services/embeddings_local.py` owns a process-wide `SentenceTransformer` singleton, with inference dispatched via `asyncio.to_thread`. `app/services/embeddings.py` shrinks to a compatibility shim that delegates to it (legacy kwargs ignored). A one-shot DB migration drops the old 768d `video_embeddings` table, recreates it at 384d via the existing schema CREATE, and marks all videos with summaries as needing re-embedding. The `PlaylistScheduler` gains a per-tick `_reembed_pending_batch(limit=10)` step that drains the queue.

**Tech Stack:** Python 3.11+, `sentence-transformers>=3.0` (new dependency, pulls torch+transformers+tokenizers), aiosqlite, sqlite-vec, FastAPI, pytest with `asyncio_mode = "auto"`. All other tooling unchanged.

---

## File Structure

**New files:**
- `app/services/embeddings_local.py` — `SentenceTransformer` singleton + async `embed_text(text)`.
- `tests/test_services_embeddings_local.py` — singleton, dimension, German input, empty-text rejection.
- `tests/test_db_migration_v7_embedding_dim.py` — migration cases (upgrade, fresh install, idempotency).

**Modified files:**
- `pyproject.toml` — add `sentence-transformers>=3.0` dependency.
- `app/services/embeddings.py` — collapse to a thin shim delegating to `embeddings_local`.
- `app/db.py` — change `video_embeddings` DDL from `FLOAT[768]` to `FLOAT[384]`; add `_migrate_v7_embedding_dim`; call it from `init_schema` BEFORE `executescript(SCHEMA)`.
- `app/repos/embeddings.py` — add `videos_pending_reembed(db, limit)` and `count_pending_reembed(db)`.
- `app/scheduler.py` — add `_reembed_pending_batch(limit=10)`; call from `run()` after the playlist sync loop, before `_record_tick`.
- `app/templates/settings.html` — remove the entire Embeddings card and the embedding parts of the Quick Setup wizard; add a small "Embeddings run locally" note.
- `app/services/providers.py` — drop `default_embedding` field, drop `list_embedding_models`, drop the embedding branch in `apply_preset`.
- `app/routes/settings.py` — drop `test_embedding` route, drop `embedding_model`/`embedding_base_url` form fields from `save_settings`, drop `list_embedding_models` import + usage, pass `reembed_pending` into the diagnostics template context.
- `app/templates/diagnostics.html` — show `Re-embed pending: N videos` line in the Scheduler card when N>0.
- `tests/test_services_embeddings.py` — replace LiteLLM-mocking tests with shim-delegation tests.
- `tests/test_services_providers.py` — drop assertions on `embedding_model`/`embedding_base_url` keys.
- `tests/test_routes_settings.py` — remove the test-embedding test, remove embedding-key assertions from save tests.
- `tests/test_scheduler.py` — add re-embed-batch test.
- `tests/test_routes_diagnostics.py` — add re-embed-pending visibility test.
- `tests/conftest.py` — add `_local_embedder` session-scoped fixture so tests share the loaded model.

---

## Task 1: Add `sentence-transformers` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Inspect current dependencies**

Run: `grep -A 20 "^dependencies" pyproject.toml`

Identify the existing `dependencies = [...]` list. Note the current version pins of unrelated packages so you don't disturb them.

- [ ] **Step 2: Add the dependency**

Edit `pyproject.toml`. Find the line with the closing `]` of `dependencies = [` and add a new entry alphabetically — typically right after `"respx"` or wherever `s` sorts:

```toml
    "sentence-transformers>=3.0",
```

- [ ] **Step 3: Install**

Run: `pip install -e .`
Expected: pip installs `sentence-transformers`, plus its transitive deps (`torch`, `transformers`, `tokenizers`, `huggingface-hub`). On macOS this takes ~1 minute and ~700 MB on disk. The output ends with `Successfully installed ...`.

- [ ] **Step 4: Verify import works**

Run: `python -c "from sentence_transformers import SentenceTransformer; print('ok')"`
Expected: `ok` (no traceback).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "deps: add sentence-transformers for local embeddings"
```

---

## Task 2: `embeddings_local` service

**Files:**
- Create: `app/services/embeddings_local.py`
- Test: `tests/test_services_embeddings_local.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_services_embeddings_local.py`:

```python
import pytest

from app.services.embeddings_local import EMBEDDING_DIM, embed_text


async def test_embed_text_returns_correct_dimension():
    """First call loads the model; result must be EMBEDDING_DIM floats."""
    vec = await embed_text("hello world")
    assert isinstance(vec, list)
    assert len(vec) == EMBEDDING_DIM
    assert all(isinstance(x, float) for x in vec)


async def test_embed_text_handles_german():
    """Multilingual MiniLM must produce a non-zero vector for German."""
    vec = await embed_text("Hallo Welt, das ist ein Test.")
    assert len(vec) == EMBEDDING_DIM
    # Smoke check: not all zeros (would mean tokenization died silently).
    assert any(abs(x) > 1e-6 for x in vec)


async def test_embed_text_rejects_empty():
    with pytest.raises(ValueError):
        await embed_text("")
    with pytest.raises(ValueError):
        await embed_text("   ")


async def test_embed_text_singleton_reuses_model():
    """Two calls share the same loaded model — no second download."""
    from app.services import embeddings_local
    # Reset the singleton to force a "first load" if the test runs in
    # isolation; subsequent assert proves the second call hits the cache.
    embeddings_local._model = None
    await embed_text("warm up")
    first_id = id(embeddings_local._model)
    await embed_text("again")
    second_id = id(embeddings_local._model)
    assert first_id == second_id


async def test_embed_text_similar_strings_have_similar_vectors():
    """Sanity: 'cat' and 'kitten' should be closer than 'cat' and 'banking'.

    Cosine similarity, computed manually because we don't want a numpy
    dep just for tests.
    """
    a = await embed_text("cat")
    b = await embed_text("kitten")
    c = await embed_text("banking")

    def cos(u: list[float], v: list[float]) -> float:
        dot = sum(x * y for x, y in zip(u, v, strict=True))
        nu = sum(x * x for x in u) ** 0.5
        nv = sum(x * x for x in v) ** 0.5
        return dot / (nu * nv)

    assert cos(a, b) > cos(a, c)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_services_embeddings_local.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.embeddings_local'`.

- [ ] **Step 3: Write the implementation**

Create `app/services/embeddings_local.py`:

```python
"""Local embedding via sentence-transformers.

Loads `paraphrase-multilingual-MiniLM-L12-v2` (384d) lazily on first
use and keeps it in a process-wide singleton. Inference runs in a
worker thread so the asyncio event loop stays responsive.

The model auto-downloads to ``~/.cache/huggingface/`` on first call
(~120 MB). Subsequent process starts hit the cache.
"""
from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger(__name__)

EMBEDDING_DIM = 384
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Module-level singleton + load lock. The lock prevents two
# concurrent first-time loads if embed_text is called twice on a
# fresh process before the first call has finished. After the first
# successful load, _model is non-None and the lock is uncontended.
_model = None  # type: ignore[var-annotated]
_load_lock = threading.Lock()


def _load_model_sync():
    """Heavy import + load. Called inside a worker thread."""
    global _model
    with _load_lock:
        if _model is not None:
            return _model
        log.info("Loading sentence-transformers model %s …", MODEL_NAME)
        # Imported lazily so the rest of the app doesn't pay the
        # transformers+torch import cost when embeddings aren't used.
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
        log.info("Model %s ready (dim=%d)", MODEL_NAME, EMBEDDING_DIM)
    return _model


def _encode_sync(text: str) -> list[float]:
    """Run the actual encode call. Numpy → plain list at the boundary."""
    model = _load_model_sync()
    # convert_to_numpy=True keeps memory predictable; we tolist() right after.
    arr = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
    return [float(x) for x in arr]


async def embed_text(text: str) -> list[float]:
    """Return the 384d embedding vector for `text`.

    Empty / whitespace-only input raises ``ValueError`` (matches the
    contract of the previous LiteLLM-backed implementation).
    """
    text = text.strip()
    if not text:
        raise ValueError("Cannot embed empty text")
    return await asyncio.to_thread(_encode_sync, text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_services_embeddings_local.py -v`
Expected: PASS (5 tests). The first run downloads the model (~30 s on a fast connection); subsequent runs are instant.

- [ ] **Step 5: Commit**

```bash
git add app/services/embeddings_local.py tests/test_services_embeddings_local.py
git commit -m "feat(embeddings): local sentence-transformers embedder"
```

---

## Task 3: Collapse `embeddings.py` to a shim

**Files:**
- Modify: `app/services/embeddings.py`
- Modify: `tests/test_services_embeddings.py`

- [ ] **Step 1: Replace the test file**

The existing tests mock `litellm.aembedding`. The shim no longer touches LiteLLM, so they're obsolete. Replace the entire file content of `tests/test_services_embeddings.py` with:

```python
"""Tests for the embeddings.py compatibility shim.

The shim accepts the legacy `model` / `api_key` / `base_url` kwargs
but ignores them — the real work happens in embeddings_local.
"""
from unittest.mock import AsyncMock, patch

import pytest


async def test_shim_delegates_to_local():
    """The shim must call embeddings_local.embed_text with the text only."""
    from app.services import embeddings as shim
    with patch(
        "app.services.embeddings_local.embed_text",
        AsyncMock(return_value=[0.1] * 384),
    ) as m:
        v = await shim.embed_text("hello")
    assert v == [0.1] * 384
    m.assert_awaited_once_with("hello")


async def test_shim_ignores_legacy_kwargs():
    """Old callers that pass model/api_key/base_url must not break."""
    from app.services import embeddings as shim
    with patch(
        "app.services.embeddings_local.embed_text",
        AsyncMock(return_value=[0.0] * 384),
    ) as m:
        await shim.embed_text(
            "hi",
            model="ollama/nomic-embed-text",
            api_key="secret",
            base_url="http://example",
        )
    # The shim drops every kwarg before delegating.
    m.assert_awaited_once_with("hi")


async def test_shim_propagates_value_error_for_empty():
    from app.services import embeddings as shim
    with pytest.raises(ValueError):
        await shim.embed_text("")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_services_embeddings.py -v`
Expected: FAIL — the existing `embeddings.py` still imports `litellm` and the new test patches the wrong target. Errors will vary; the point is to see them go red before fixing.

- [ ] **Step 3: Replace `app/services/embeddings.py`**

Replace the entire file content with:

```python
"""Compatibility shim — delegates to embeddings_local.

The legacy ``model`` / ``api_key`` / ``base_url`` parameters are
accepted but ignored. They will be removed in a follow-up cleanup
once all callers (pipeline.py, home.py, routes/settings.py) stop
passing them.
"""
from __future__ import annotations


async def embed_text(
    text: str,
    *,
    model: str | None = None,    # noqa: ARG001 — kept for back-compat
    api_key: str = "",            # noqa: ARG001
    base_url: str | None = None,  # noqa: ARG001
) -> list[float]:
    """Return the embedding vector for `text`.

    All positional/keyword args except `text` are ignored — the local
    `paraphrase-multilingual-MiniLM-L12-v2` model is the only embedder.
    """
    from app.services import embeddings_local
    return await embeddings_local.embed_text(text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_services_embeddings.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the broader suite to confirm callers still work**

Run: `pytest tests/test_pipeline.py -q`
Expected: PASS — `_try_embed_summary` still works because the shim signature is unchanged.

- [ ] **Step 6: Commit**

```bash
git add app/services/embeddings.py tests/test_services_embeddings.py
git commit -m "refactor(embeddings): collapse embeddings.py to a local shim"
```

---

## Task 4: New repo helpers — `videos_pending_reembed` + `count_pending_reembed`

**Files:**
- Modify: `app/repos/embeddings.py`
- Test: `tests/test_repos_embeddings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repos_embeddings.py` (or create the file if it doesn't exist — verify with `ls tests/test_repos_embeddings.py`):

```python
import aiosqlite

from app.repos import embeddings as embeddings_repo
from app.repos import videos as videos_repo


async def _video_with_summary(
    db: aiosqlite.Connection, vid: str, *, summary_embedded: bool,
) -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await db.execute(
        "UPDATE videos SET summary='some summary' WHERE id=?", (vid,)
    )
    if summary_embedded:
        await db.execute(
            "UPDATE videos SET summary_embedded_at=datetime('now') WHERE id=?",
            (vid,),
        )
    await db.commit()


async def _video_without_summary(db: aiosqlite.Connection, vid: str) -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )


async def test_videos_pending_reembed_returns_unembedded_with_summary(
    db: aiosqlite.Connection,
):
    await _video_with_summary(db, "a", summary_embedded=False)
    await _video_with_summary(db, "b", summary_embedded=False)
    await _video_with_summary(db, "c", summary_embedded=True)
    await _video_without_summary(db, "d")  # no summary → not pending

    pending = await embeddings_repo.videos_pending_reembed(db, limit=10)
    assert set(pending) == {"a", "b"}


async def test_videos_pending_reembed_respects_limit(db: aiosqlite.Connection):
    for vid in ("a", "b", "c", "d"):
        await _video_with_summary(db, vid, summary_embedded=False)
    pending = await embeddings_repo.videos_pending_reembed(db, limit=2)
    assert len(pending) == 2


async def test_count_pending_reembed_matches_list_length(
    db: aiosqlite.Connection,
):
    await _video_with_summary(db, "a", summary_embedded=False)
    await _video_with_summary(db, "b", summary_embedded=True)
    await _video_with_summary(db, "c", summary_embedded=False)
    n = await embeddings_repo.count_pending_reembed(db)
    full = await embeddings_repo.videos_pending_reembed(db, limit=999)
    assert n == len(full) == 2


async def test_count_pending_reembed_returns_zero_on_empty_table(
    db: aiosqlite.Connection,
):
    assert await embeddings_repo.count_pending_reembed(db) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_repos_embeddings.py -v -k "pending_reembed"`
Expected: FAIL with `AttributeError: module 'app.repos.embeddings' has no attribute 'videos_pending_reembed'`.

- [ ] **Step 3: Write the implementation**

Append to `app/repos/embeddings.py`:

```python
async def videos_pending_reembed(
    db: aiosqlite.Connection, limit: int,
) -> list[str]:
    """Video IDs that have a summary but no current embedding.

    Used by the scheduler to drain the re-embed queue after the
    768d → 384d migration. Order is by `id` (deterministic, and
    matches insertion order on a typical install).
    """
    cursor = await db.execute(
        """
        SELECT id FROM videos
        WHERE summary IS NOT NULL AND summary_embedded_at IS NULL
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def count_pending_reembed(db: aiosqlite.Connection) -> int:
    """COUNT(*) of the same predicate as videos_pending_reembed.

    Cheap; the diagnostics page polls it on each render.
    """
    cursor = await db.execute(
        """
        SELECT COUNT(*) FROM videos
        WHERE summary IS NOT NULL AND summary_embedded_at IS NULL
        """
    )
    row = await cursor.fetchone()
    return row[0] if row else 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_repos_embeddings.py -v`
Expected: PASS (4 new tests).

- [ ] **Step 5: Commit**

```bash
git add app/repos/embeddings.py tests/test_repos_embeddings.py
git commit -m "feat(embeddings): videos_pending_reembed/count helpers"
```

---

## Task 5: Schema migration — drop 768d table, set re-embed flag

**Files:**
- Modify: `app/db.py`
- Test: `tests/test_db_migration_v7_embedding_dim.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db_migration_v7_embedding_dim.py`:

```python
"""Tests for the 768d → 384d embedding migration.

Note: pyproject sets `asyncio_mode = "auto"`, so async tests need no
decorator. The `db` fixture from conftest.py gives a clean in-memory
DB with init_schema already applied — but we need to *bypass* that
fixture in some tests so we can simulate an upgrade from the
old 768d shape.
"""
import struct

import aiosqlite
import pytest

from app.config import Config
from app.db import connect, init_schema
from app.repos import settings as settings_repo


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


@pytest.fixture
async def legacy_db(tmp_path):
    """A DB initialised with the OLD 768d schema, mid-upgrade.

    This simulates a real user upgrading: their existing DB has the
    768d vec0 table, some videos with summaries, and no migration
    flag. We open a connection but DO NOT run init_schema yet —
    each test calls init_schema explicitly to exercise the migration.
    """
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = await connect(cfg)
    # Hand-roll the legacy schema: just the bits the migration cares about.
    await conn.executescript(
        """
        CREATE TABLE settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        INSERT INTO users (id, name) VALUES (1, 'admin');
        CREATE TABLE videos (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            thumbnail_path TEXT,
            duration_seconds INTEGER,
            transcript TEXT,
            transcript_source TEXT,
            summary TEXT,
            summary_model TEXT,
            user_id INTEGER NOT NULL DEFAULT 1,
            youtube_id TEXT,
            kind TEXT NOT NULL DEFAULT 'youtube',
            transcript_segments TEXT,
            source_language TEXT,
            summary_language TEXT,
            transcript_language TEXT,
            summary_embedded_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE VIRTUAL TABLE video_embeddings USING vec0(
            video_id TEXT PRIMARY KEY,
            summary_vec FLOAT[768]
        );
        """
    )
    await conn.commit()
    try:
        yield conn
    finally:
        await conn.close()


async def test_migration_drops_old_table_on_upgrade(legacy_db):
    # Seed: video with summary + filled embedded_at, plus a 768d vector.
    await legacy_db.execute(
        "INSERT INTO videos (id, url, title, description, summary, "
        "summary_embedded_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        ("v1", "u", "t", "", "some summary"),
    )
    await legacy_db.execute(
        "INSERT INTO video_embeddings (video_id, summary_vec) VALUES (?, ?)",
        ("v1", _pack([0.1] * 768)),
    )
    await legacy_db.commit()

    await init_schema(legacy_db)

    # The new table accepts a 384d insert (proves new dimension).
    await legacy_db.execute(
        "INSERT INTO video_embeddings (video_id, summary_vec) VALUES (?, ?)",
        ("v_new", _pack([0.0] * 384)),
    )
    await legacy_db.commit()

    # And rejects a 768d insert (proves the OLD dimension is gone).
    with pytest.raises(aiosqlite.Error):
        await legacy_db.execute(
            "INSERT INTO video_embeddings (video_id, summary_vec) VALUES (?, ?)",
            ("v_old", _pack([0.0] * 768)),
        )


async def test_migration_marks_videos_with_summary_as_pending(legacy_db):
    await legacy_db.execute(
        "INSERT INTO videos (id, url, title, description, summary, "
        "summary_embedded_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        ("withsum", "u", "t", "", "x"),
    )
    await legacy_db.execute(
        "INSERT INTO videos (id, url, title, description, summary) "
        "VALUES (?, ?, ?, ?, NULL)",
        ("nosum", "u", "t", ""),
    )
    await legacy_db.commit()

    await init_schema(legacy_db)

    cursor = await legacy_db.execute(
        "SELECT id, summary_embedded_at FROM videos ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert {(r[0], r[1]) for r in rows} == {
        ("nosum", None),       # no summary → untouched
        ("withsum", None),     # was set, now cleared by migration
    }


async def test_migration_sets_flag(legacy_db):
    await init_schema(legacy_db)
    flag = await settings_repo.get(legacy_db, "embedding_dim_migrated")
    assert flag == "384"


async def test_migration_is_idempotent(legacy_db):
    # First run does the work.
    await init_schema(legacy_db)
    # Now mark a video as embedded — to prove the second run does NOT
    # reset it.
    await legacy_db.execute(
        "INSERT INTO videos (id, url, title, description, summary, "
        "summary_embedded_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        ("v2", "u", "t", "", "x"),
    )
    await legacy_db.commit()

    await init_schema(legacy_db)

    cursor = await legacy_db.execute(
        "SELECT summary_embedded_at FROM videos WHERE id='v2'"
    )
    row = await cursor.fetchone()
    assert row[0] is not None  # untouched on second run


async def test_migration_on_fresh_install(tmp_path):
    """A fresh DB has no flag and no old table — migration must
    set the flag without crashing on the missing table."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = await connect(cfg)
    try:
        await init_schema(conn)
        flag = await settings_repo.get(conn, "embedding_dim_migrated")
        assert flag == "384"
        # And the new table is queryable.
        cursor = await conn.execute("SELECT COUNT(*) FROM video_embeddings")
        row = await cursor.fetchone()
        assert row[0] == 0
    finally:
        await conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db_migration_v7_embedding_dim.py -v`
Expected: FAIL — most tests fail because `init_schema` still creates 768d tables and there's no migration. Some failures will be "table already exists" because init_schema's CREATE doesn't drop first.

- [ ] **Step 3: Update the SCHEMA dimension**

In `app/db.py`, find the `SCHEMA` constant. Locate the `CREATE VIRTUAL TABLE IF NOT EXISTS video_embeddings` line (~line 131) and change `FLOAT[768]` to `FLOAT[384]`:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS video_embeddings USING vec0(
    video_id TEXT PRIMARY KEY,
    summary_vec FLOAT[384]
);
```

Note: `IF NOT EXISTS` means this DDL is a no-op for an upgrade where the old 768d table still exists. The migration in step 4 handles the recreate.

- [ ] **Step 4: Add the migration function**

Add at module scope, just before `async def init_schema`:

```python
async def _migrate_v7_embedding_dim(conn: aiosqlite.Connection) -> None:
    """One-shot 768d → 384d embedding-dimension migration.

    Idempotent: gated by the ``embedding_dim_migrated=384`` settings
    row. Runs AFTER ``executescript(SCHEMA)`` so the ``settings`` and
    ``videos`` tables already exist (fresh-install case). On upgrade,
    the SCHEMA's ``IF NOT EXISTS`` CREATE leaves the old 768d table
    intact, so this function drops and recreates it at 384d.

    Marks every video with a summary as ``summary_embedded_at = NULL``
    so the scheduler's ``_reembed_pending_batch`` picks them up.

    Uses raw SQL (not settings_repo.get/set) to avoid a circular
    import: settings_repo lives at app.repos.settings, which has no
    db.py dependency, but importing it here would pull repos into
    the db module's startup path.
    """
    cursor = await conn.execute(
        "SELECT value FROM settings WHERE user_id=1 AND key=?",
        ("embedding_dim_migrated",),
    )
    row = await cursor.fetchone()
    if row is not None and row[0] == "384":
        return  # already migrated

    # DROP the (possibly old) table and recreate at the new dimension.
    # On a fresh install the SCHEMA already created the 384d table;
    # the DROP+CREATE is harmless (one round-trip).
    await conn.execute("DROP TABLE IF EXISTS video_embeddings")
    await conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS video_embeddings USING vec0(
            video_id TEXT PRIMARY KEY,
            summary_vec FLOAT[384]
        )
        """
    )
    await conn.execute(
        "UPDATE videos SET summary_embedded_at = NULL "
        "WHERE summary IS NOT NULL"
    )
    await conn.execute(
        """
        INSERT INTO settings (user_id, key, value) VALUES (1, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value
        """,
        ("embedding_dim_migrated", "384"),
    )
    await conn.commit()
```

- [ ] **Step 5: Wire the migration into `init_schema`**

Find the existing `init_schema`:

```python
async def init_schema(conn: aiosqlite.Connection) -> None:
    await _run_migrations(conn)
    await conn.executescript(SCHEMA)
    cursor = await conn.execute("SELECT COUNT(*) FROM users")
    row = await cursor.fetchone()
    if row is not None and row[0] == 0:
        await conn.execute(
            "INSERT INTO users (id, name) VALUES (1, 'admin')"
        )
    await conn.commit()
```

Insert the migration call right after `executescript(SCHEMA)`:

```python
async def init_schema(conn: aiosqlite.Connection) -> None:
    await _run_migrations(conn)
    await conn.executescript(SCHEMA)
    await _migrate_v7_embedding_dim(conn)
    cursor = await conn.execute("SELECT COUNT(*) FROM users")
    row = await cursor.fetchone()
    if row is not None and row[0] == 0:
        await conn.execute(
            "INSERT INTO users (id, name) VALUES (1, 'admin')"
        )
    await conn.commit()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_db_migration_v7_embedding_dim.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Run the broader suite to confirm nothing else broke**

Run: `pytest -q --ignore=tests/test_services_model_info.py`
Expected: PASS — note that some existing tests embed a 768d vector via mocks; if any of those break, the cause is the new dimension. (None should break because the DB-touching tests use the `db` fixture which goes through `init_schema` and gets the new 384d table.)

- [ ] **Step 8: Commit**

```bash
git add app/db.py tests/test_db_migration_v7_embedding_dim.py
git commit -m "feat(db): migrate video_embeddings from 768d to 384d"
```

---

## Task 6: Scheduler `_reembed_pending_batch`

**Files:**
- Modify: `app/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scheduler.py`:

```python
async def test_scheduler_reembeds_pending_videos(
    db: aiosqlite.Connection, tmp_path,
):
    """After one tick, videos with summary but no embedded_at get
    embedded and stamped."""
    from unittest.mock import AsyncMock, patch

    from app.repos import videos as videos_repo

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    # Seed two videos with summaries, neither embedded yet.
    for vid in ("a", "b"):
        await videos_repo.upsert_metadata(
            db, video_id=vid, url="u", title="t", description="",
            thumbnail_path=None, duration_seconds=None,
        )
        await db.execute(
            "UPDATE videos SET summary='hello there' WHERE id=?", (vid,)
        )
    await db.commit()
    await settings_repo.set(db, "playlist_refresh_interval_minutes", "0")

    fake_vec = [0.1] * 384

    with patch(
        "app.services.embeddings_local.embed_text",
        AsyncMock(return_value=fake_vec),
    ):
        scheduler = PlaylistScheduler(
            db=db, config=config, sync_fn=AsyncMock(),
            min_sleep_seconds=0.05,
        )
        task = asyncio.create_task(scheduler.run())
        # Wait for both videos to be marked embedded.
        for _ in range(40):
            await asyncio.sleep(0.05)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM videos "
                "WHERE summary IS NOT NULL AND summary_embedded_at IS NOT NULL"
            )
            row = await cursor.fetchone()
            if row and row[0] == 2:
                break
        scheduler.stop()
        await task

    cursor = await db.execute(
        "SELECT id FROM videos WHERE summary_embedded_at IS NOT NULL "
        "ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert [r[0] for r in rows] == ["a", "b"]


async def test_scheduler_reembed_continues_after_per_video_failure(
    db: aiosqlite.Connection, tmp_path,
):
    """If embed_text raises for one video, the scheduler logs and
    moves on; the other video still gets embedded."""
    from unittest.mock import AsyncMock, patch

    from app.repos import videos as videos_repo

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    for vid in ("good", "bad"):
        await videos_repo.upsert_metadata(
            db, video_id=vid, url="u", title="t", description="",
            thumbnail_path=None, duration_seconds=None,
        )
        await db.execute(
            "UPDATE videos SET summary='x' WHERE id=?", (vid,)
        )
    await db.commit()
    await settings_repo.set(db, "playlist_refresh_interval_minutes", "0")

    async def flaky(text: str):
        # 'bad' video is queried first because list_queue orders by id;
        # but we'd prefer to fail by content for clarity.
        if text == "x" and not flaky.calls:
            flaky.calls = True
            raise RuntimeError("simulated embed failure")
        return [0.0] * 384
    flaky.calls = False

    with patch(
        "app.services.embeddings_local.embed_text", side_effect=flaky,
    ):
        scheduler = PlaylistScheduler(
            db=db, config=config, sync_fn=AsyncMock(),
            min_sleep_seconds=0.05,
        )
        task = asyncio.create_task(scheduler.run())
        for _ in range(40):
            await asyncio.sleep(0.05)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM videos "
                "WHERE summary_embedded_at IS NOT NULL"
            )
            row = await cursor.fetchone()
            if row and row[0] >= 1:
                break
        scheduler.stop()
        await task

    # At least one video succeeded despite the other failing.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM videos WHERE summary_embedded_at IS NOT NULL"
    )
    row = await cursor.fetchone()
    assert row[0] >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scheduler.py::test_scheduler_reembeds_pending_videos -v`
Expected: FAIL — the scheduler doesn't re-embed yet.

- [ ] **Step 3: Modify `app/scheduler.py`**

Add the import block at the top (after the existing imports). Find the existing imports:

```python
import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import aiosqlite

from app.config import Config
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
```

Add:

```python
from app.repos import embeddings as embeddings_repo
from app.repos import videos as videos_repo
from app.services import embeddings as embeddings_service
```

(`embeddings_service` is the shim — keeps the test patch target stable. Tests patch `app.services.embeddings_local.embed_text` and the shim calls into it.)

Add a new method on `PlaylistScheduler`. A natural placement is after `_record_tick` and before `run`:

```python
    async def _reembed_pending_batch(self, limit: int = 10) -> int:
        """Drain up to `limit` videos that need re-embedding.

        Per-video failures are logged and skipped — one bad video must
        not stop the batch. Returns the count of successful embeds for
        the heartbeat step string.
        """
        try:
            ids = await embeddings_repo.videos_pending_reembed(
                self._db, limit
            )
        except Exception:
            log.exception("reembed: videos_pending_reembed failed")
            return 0

        n_done = 0
        for video_id in ids:
            try:
                video = await videos_repo.get(self._db, video_id)
                if video is None or not video.summary:
                    continue
                vector = await embeddings_service.embed_text(video.summary)
                await embeddings_repo.upsert_summary_embedding(
                    self._db, video_id, vector,
                )
                n_done += 1
            except Exception:
                log.exception("reembed: video %s failed", video_id)
        return n_done
```

Now wire it into `run()`. Find the existing run loop body and locate the per-playlist loop end + `_record_tick`:

```python
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

Insert a re-embed step between the playlist loop and `_record_tick`:

```python
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
            n_reembedded = await self._reembed_pending_batch(limit=10)
            if n_reembedded:
                self._touch(
                    current_step=f"re-embedded {n_reembedded} videos"
                )
            await self._record_tick()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS — all existing scheduler tests + 2 new re-embed tests. (12+2 = 14 total in this file at this point.)

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_scheduler.py
git commit -m "feat(scheduler): re-embed pending videos each tick"
```

---

## Task 7: Diagnostics page — show re-embed-pending count

**Files:**
- Modify: `app/routes/settings.py`
- Modify: `app/templates/diagnostics.html`
- Test: `tests/test_routes_diagnostics.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_routes_diagnostics.py`:

```python
def test_diagnostics_shows_reembed_pending_count(tmp_path, monkeypatch):
    """If videos with summary lack embedded_at, the count appears."""
    import asyncio

    from app.repos import videos as videos_repo

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def seed():
            for vid in ("a", "b", "c"):
                await videos_repo.upsert_metadata(
                    app.state.db, video_id=vid, url="u", title="t",
                    description="", thumbnail_path=None,
                    duration_seconds=None,
                )
                await app.state.db.execute(
                    "UPDATE videos SET summary='x' WHERE id=?", (vid,)
                )
            await app.state.db.commit()
        asyncio.get_event_loop().run_until_complete(seed())

        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200
    # Three videos with summaries, none embedded → "Re-embed pending: 3"
    assert "Re-embed pending: 3" in resp.text


def test_diagnostics_hides_reembed_when_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings/diagnostics")
    assert resp.status_code == 200
    assert "Re-embed pending" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routes_diagnostics.py -v -k "reembed"`
Expected: FAIL — the page doesn't render the count yet.

- [ ] **Step 3: Pass `reembed_pending` into the template context**

In `app/routes/settings.py`, find the `diagnostics_page` handler. Locate the line that calls the existing `count_pending_reembed` predecessor — there isn't one. So add the call alongside the existing scheduler-card data fetches.

Find this block in `diagnostics_page`:

```python
    scheduler_last_tick_at = await settings_repo.get(
        db, "scheduler_last_tick_at",
    )
    scheduled_playlists = await playlists_repo.list_for_user(db, 1)
```

Add after it:

```python
    reembed_pending = await embeddings_repo.count_pending_reembed(db)
```

`embeddings_repo` is not imported at module level yet. Add to the existing import block (top of the file):

```python
from app.repos import embeddings as embeddings_repo
```

Find the `templates.TemplateResponse(...)` call at the end of the handler. Add `reembed_pending` to the context dict:

```python
            "scheduled_playlists": scheduled_playlists,
            "reembed_pending": reembed_pending,
            "log_lines": log_lines,
```

- [ ] **Step 4: Render in the template**

In `app/templates/diagnostics.html`, find the Scheduler card. After the playlists table and before the `Jetzt prüfen` form, add:

```html
    {% if reembed_pending %}
      <p class="settings-card-sub">
        Re-embed pending: <strong>{{ reembed_pending }}</strong> videos
      </p>
    {% endif %}
```

Place it inside the Scheduler `<section>`, between the playlists table block and the form. Use `{% if reembed_pending %}` (truthy on N>0) so the line is hidden when there's nothing to show.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_routes_diagnostics.py -v`
Expected: PASS (existing tests + 2 new).

- [ ] **Step 6: Commit**

```bash
git add app/routes/settings.py app/templates/diagnostics.html tests/test_routes_diagnostics.py
git commit -m "feat(diagnostics): show re-embed pending count"
```

---

## Task 8: Strip `embedding_*` from provider presets

**Files:**
- Modify: `app/services/providers.py`
- Test: `tests/test_services_providers.py`

- [ ] **Step 1: Update the failing tests**

Open `tests/test_services_providers.py` and remove every assertion that mentions `embedding_model` or `embedding_base_url`. The simplest approach is to grep-and-edit. Find each occurrence:

```bash
grep -n "embedding_model\|embedding_base_url" tests/test_services_providers.py
```

Each line will be one of:
- `assert "embedding_model" not in out` — keep as a positive assertion that the key is absent.
- `assert "embedding_model" in out` — invert: should now be `not in`.
- `assert out["embedding_model"] == "..."` — delete the line.
- `"embedding_model": "..."` inside a dict literal — delete the line.

For each `assert ... in out` test that was checking embedding presence, replace with `assert "embedding_model" not in out` to lock down the new contract.

Example diff for `tests/test_services_providers.py:107`:

Before:
```python
assert "embedding_model" in out
```

After:
```python
assert "embedding_model" not in out
```

And `tests/test_services_providers.py:230`:

Before:
```python
assert out["embedding_base_url"] == "http://192.168.0.27:11434"
```

After: delete the line.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_services_providers.py -v`
Expected: FAIL — the inverted assertions fail because providers.py still writes embedding keys.

- [ ] **Step 3: Update `app/services/providers.py`**

In `app/services/providers.py`:

**3a.** Remove `default_embedding` from `ProviderPreset`:

Find:
```python
    default_embedding: str | None = None  # litellm-prefixed; None if none
```
Delete that line.

**3b.** Remove `default_embedding=...` from every preset entry. There are 5 occurrences (openai, anthropic, gemini, groq, ollama, openrouter). Delete each line.

**3c.** Delete the `list_embedding_models` function entirely. Find it:

```bash
grep -n "^def list_embedding_models" app/services/providers.py
```

Delete the function body (typically 5–15 lines).

**3d.** In `apply_preset`, remove the `embedding_model_override` parameter and the entire embedding block. Find:

```python
def apply_preset(
    *,
    provider_id: str,
    api_key: str,
    current_settings: dict[str, str],
    llm_model_override: str | None = None,
    llm_base_url_override: str | None = None,
    embedding_model_override: str | None = None,
) -> dict[str, str]:
```

Remove the `embedding_model_override: str | None = None,` line.

Then find the `# ── Embedding ──` block (around lines 228–239) and delete it entirely:

```python
    # ── Embedding (only if this provider has one) ──
    if preset.default_embedding:
        out["embedding_model"] = (
            embedding_model_override or preset.default_embedding
        ).strip()
        if preset.id != "ollama":
            out["embedding_base_url"] = ""
        else:
            out["embedding_base_url"] = chosen_base_url or ""
```

Delete those 9 lines.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_services_providers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/providers.py tests/test_services_providers.py
git commit -m "refactor(providers): drop default_embedding from presets"
```

---

## Task 9: Strip `embedding_*` from settings route

**Files:**
- Modify: `app/routes/settings.py`
- Test: `tests/test_routes_settings.py`

- [ ] **Step 1: Update the tests**

Open `tests/test_routes_settings.py`. Find tests that touch `embedding_model` / `embedding_base_url` / `test-embedding`:

```bash
grep -n "embedding_model\|embedding_base_url\|test-embedding\|test_embedding" tests/test_routes_settings.py
```

For each test:
- A test that asserts `embedding_model` is saved → drop the assertion (the field is gone from the form).
- A test that POSTs to `/settings/test-embedding` → delete the entire test function.
- A test that pre-seeds `embedding_model` in settings before calling `/settings` → remove the seed step (no longer needed).

Specifically: the `test_post_settings_saves` test (around line 16) likely asserts the embedding settings — drop those lines but keep the assertions on `llm_model` / `whisper_model`.

- [ ] **Step 2: Run tests to verify they fail / find the broken ones**

Run: `pytest tests/test_routes_settings.py -v`
Expected: some failures around the modified tests; the fix is in step 3.

- [ ] **Step 3: Update `app/routes/settings.py`**

**3a.** Remove the `test_embedding` route handler. Find:

```python
@router.post("/settings/test-embedding", response_class=HTMLResponse)
async def test_embedding(...):
    ...
```

Delete the entire function body (the `@router.post` decorator through the function's last `return` — typically 30–40 lines).

**3b.** Remove the embedding fields from `save_settings`. Find the function signature:

```python
async def save_settings(
    ...
    embedding_model: str = Form(""),
    embedding_base_url: str = Form(""),
    ...
):
```

Remove the `embedding_model:` and `embedding_base_url:` form parameter lines.

In the function body, find the loop that writes settings (look for `("embedding_model", embedding_model.strip())`). Remove the two tuple entries for `embedding_model` and `embedding_base_url`.

**3c.** Remove the `list_embedding_models` import and usage. Find at the top:

```python
from app.services.providers import (
    ...
    list_embedding_models,
    ...
)
```

Remove `list_embedding_models,` from the import.

In `settings_page`, find:

```python
    preset_embed_models: dict[str, list[str]] = {}
    for p in presets:
        if p.id == "ollama":
            continue
        ...
        if p.default_embedding:
            preset_embed_models[p.id] = list_embedding_models(p.id)
```

Delete the entire `preset_embed_models` dict and its population loop.

Also remove `"preset_embed_models": preset_embed_models,` from the template context dict at the end of `settings_page`.

**3d.** Remove the `quick_setup` route's `embedding_model` form param if present. Find:

```python
async def quick_setup(
    ...
    embedding_model: str = Form(""),
    ...
):
```

Remove that parameter, and remove `embedding_model_override=embedding_model.strip() or None,` from the `apply_preset(...)` call.

**3e.** Remove the embedding block in `quick_setup_ollama_models`. Find the section that builds `embed_block`:

```python
    embed_block = ""
    if embed_tags:
        embed_options = "".join(
            f'<option value="ollama/{tag}">{tag}</option>'
            for tag in embed_tags
        )
        embed_block = (
            '<label class="settings-field">'
            '<span class="settings-label">Embedding model</span>'
            f'<select name="embedding_model">{embed_options}</select>'
            '</label>'
        )
```

Delete those lines, and update the `return HTMLResponse(chat_block + embed_block + summary)` to `return HTMLResponse(chat_block + summary)` (drop `embed_block`). Also drop `embed_tags` from `split_ollama_tags` destructuring if it becomes unused, or leave it — `_` to silence the unused warning.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routes_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/settings.py tests/test_routes_settings.py
git commit -m "refactor(settings): drop embedding_* from routes and form"
```

---

## Task 10: Strip the Embeddings card from settings template

**Files:**
- Modify: `app/templates/settings.html`

- [ ] **Step 1: Locate the Embeddings card**

Run: `grep -n "Embeddings\|embedding_model\|embedding_base_url\|test-embedding" app/templates/settings.html`

Expect hits around lines 363–396 (the `<!-- ── Embeddings ──` block) and around lines 178–197 (the Quick Setup wizard's per-provider embedding labels).

- [ ] **Step 2: Remove the Embeddings card section**

Find the block:

```html
    <!-- ── Embeddings ──────────────────────────────────────────── -->
    <section class="settings-card">
      <header class="settings-card-head">
        <span class="settings-card-icon" aria-hidden="true">
          <img src="{{ url_for('static', path='icons/embedding.png') }}?v={{ asset_version() }}" alt="">
        </span>
        <div class="settings-card-head-text">
          <h2>Embeddings (semantic search)</h2>
          ...
        </div>
      </header>
      <label class="settings-field">
        <span class="settings-label">Embedding model</span>
        <input name="embedding_model" .../>
        ...
      </label>
      <label class="settings-field">
        <span class="settings-label">Embedding base URL ...</span>
        <input name="embedding_base_url" .../>
        ...
      </label>
      <div class="settings-test-row">
        <button type="button" class="btn btn-secondary"
                hx-post="/settings/test-embedding" .../>
        <div id="embedding-test-result" class="settings-test-result"></div>
      </div>
    </section>
```

Delete the entire `<section class="settings-card">` from the `<!-- ── Embeddings ──` comment through the closing `</section>` tag.

- [ ] **Step 3: Remove embedding bits from the Quick Setup wizard**

Find:

```html
              {% if p.default_embedding %}<span class="quicksetup-tag">Embedding</span>{% endif %}
```

Delete that line.

Find around line 178:

```html
                <select name="embedding_model"
                        ...>
                    {% set selected_value = settings.get('embedding_model', '') if is_active else p.default_embedding %}
                    ...
                </select>
```

Delete the entire `<select name="embedding_model">` block (typically 10–20 lines including the surrounding label).

Find around line 196:

```html
            {% if p.default_embedding %}
              · <code>{{ p.default_embedding }}</code>
            {% endif %}
```

Delete those 3 lines.

- [ ] **Step 4: Add a one-line note**

Find the Whisper card (or a logical spot near the end of the LLM/Whisper section). Add a small note that embeddings are local. A good spot is just before the Whisper card or right after the LLM card. Add:

```html
    <!-- Embeddings run locally — no configuration needed -->
    <p class="settings-card-sub" style="margin: 1rem 0">
      <em>Embeddings run locally via sentence-transformers
      (paraphrase-multilingual-MiniLM-L12-v2, 384d). No configuration
      needed.</em>
    </p>
```

This is a nudge for the user who might miss the absent Embeddings card.

- [ ] **Step 5: Manual smoke**

Run the dev server and load `/settings`. Confirm:
- No "Embeddings (semantic search)" card.
- No "Test Embedding" button.
- The Quick Setup wizard tiles no longer show the "Embedding" tag.
- The note about local embeddings is visible somewhere on the page.

```bash
python -m uvicorn app.main:app --port 8000 --reload
```

Then `curl http://localhost:8000/settings | grep -i "embedding"` — should match only the new note.

Stop the server with Ctrl-C.

- [ ] **Step 6: Commit**

```bash
git add app/templates/settings.html
git commit -m "feat(settings): remove Embeddings card, add local-embedding note"
```

---

## Task 11: Test fixture — share the model across tests

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Inspect existing fixtures**

Run: `cat tests/conftest.py`

Note the existing `amy_low_voice` session-scoped fixture pattern.

- [ ] **Step 2: Add a session-scoped warmup**

Append to `tests/conftest.py`:

```python
@pytest.fixture(scope="session", autouse=True)
def _warmup_local_embedder() -> None:
    """Trigger the sentence-transformers model load once per pytest run.

    Without this, the first test that calls embed_text pays the ~30s
    download/load cost. With it, every test sees a warm singleton.

    autouse=True so tests don't have to opt in. The fixture body is
    sync — it spins up an event loop just for the warmup call.
    """
    import asyncio

    from app.services import embeddings_local

    # If a previous test session already loaded the model in this
    # process, the singleton is still set — bail out fast.
    if embeddings_local._model is not None:
        return
    try:
        asyncio.get_event_loop().run_until_complete(
            embeddings_local.embed_text("warmup")
        )
    except Exception:
        # If the model can't load (e.g. offline CI without HF cache),
        # let individual tests fail with their own assertions; don't
        # block the whole suite collection.
        pass
```

- [ ] **Step 3: Run the suite to confirm no regression**

Run: `pytest -q --ignore=tests/test_services_model_info.py`
Expected: PASS. The first test invocation in a fresh process triggers the warmup; subsequent tests share the cached model.

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "test: warm up local embedder once per session"
```

---

## Task 12: Final verification

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q --ignore=tests/test_services_model_info.py`
Expected: ALL PASS.

- [ ] **Step 2: Run ruff**

Run: `ruff check app tests`
Expected: clean (or only pre-existing warnings unrelated to this PR).

- [ ] **Step 3: Manual smoke (recommended)**

```bash
python -m uvicorn app.main:app --port 8000 --reload
```

Open `http://localhost:8000/settings`:
- Confirm no Embeddings card, no Test Embedding button.
- Confirm the "Embeddings run locally" note is visible.

Open `http://localhost:8000/settings/diagnostics`:
- If you have videos with summaries from before this PR, you should see "Re-embed pending: N videos" in the Scheduler card.
- Click "Jetzt prüfen" — within ~2 s the scheduler ticks, processes a batch of 10, and the count drops by 10. Refresh the page to see.

Submit a new YouTube video at `http://localhost:8000/`:
- After processing, the new video gets embedded via the local model. No network calls to Ollama or any LLM provider for the embedding step (verify by checking the diagnostics log tail for `Loading sentence-transformers model …` on first run, then nothing for subsequent embeds).

Stop the server with Ctrl-C.

- [ ] **Step 4: Confirm no embedding-related cruft remains**

```bash
grep -rn "embedding_model\|embedding_base_url\|list_embedding_models\|default_embedding\|test-embedding" app/ --include="*.py" --include="*.html"
```

Expected output: only matches inside the local-embedding service / shim itself, OR the migration's `embedding_dim_migrated` flag. No references to user-facing settings keys, no LiteLLM embedding code paths, no test-embedding routes.

Some hits in `app/templates/diagnostics.html` for `Re-embed pending` are fine.
Hits in `app/pipeline.py:_try_embed_summary` for the now-stale `embedding_model = settings.get(...)` lines are acceptable for this PR — the shim ignores them. A follow-up cleanup task (noted in the spec's "Open questions") will drop them.

- [ ] **Step 5: Final commit/clean check**

```bash
git status
```

Expected: clean. If anything is staged but uncommitted, commit it with a meaningful message before declaring the feature done.

---

## Self-review notes (post-write)

- **Spec coverage:** Every requirement maps to a Task — local embedder (Task 2), shim (Task 3), repo helpers (Task 4), schema migration (Task 5), scheduler integration (Task 6), diagnostics surface (Task 7), provider preset cleanup (Task 8), routes cleanup (Task 9), template cleanup (Task 10), test infrastructure (Task 11), final pass (Task 12). The non-goals (configurable model, new worker, pre-bundled image, transcript embedding, FTS changes) are honored.
- **Type consistency:** `EMBEDDING_DIM = 384` is the only constant defining the dimension; everything else (DDL, tests, migration) refers to 384 by literal value to keep tests readable. The shim signature stays `embed_text(text, *, model=None, api_key="", base_url=None)` to match `app/pipeline.py` callers (verified at line 336–341).
- **Migration ordering:** the migration runs AFTER `executescript(SCHEMA)`, so it can rely on `settings` and `videos` tables being present. It does its own `DROP TABLE IF EXISTS video_embeddings; CREATE VIRTUAL TABLE …` to recreate at the new dimension because the schema's `IF NOT EXISTS` won't replace an existing 768d table.
- **Tests:** the new `_warmup_local_embedder` autouse session fixture pays the model-load cost once. The integration test at Task 6 patches `app.services.embeddings_local.embed_text` directly so it doesn't hit the real model — fast and deterministic.
- **Naming:** `_reembed_pending_batch`, `videos_pending_reembed`, `count_pending_reembed`, `_migrate_v7_embedding_dim` — consistent verb-first names.
