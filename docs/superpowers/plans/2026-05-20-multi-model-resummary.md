# Multi-model configuration & per-run resummary override — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move LLM config from a single `settings` row to a managed
`llm_models` table with a default, expose a Re-summarize panel that
picks model + extra one-shot prompt for the next run, expose the same
model picker in the chat box, and surface the available models to MCP
clients via tool docstrings.

**Architecture:** New `llm_models` table + repo. Pipeline/worker/chat
resolve model rows (override or default). Jobs gain two nullable
override columns (`llm_model_id`, `additional_prompt`) that the
Re-summarize endpoint writes and the worker passes through. Settings
UI gets a "Configured models" card listing the rows with a green
default border; Quick Setup becomes the add/edit wizard. MCP tools
bake the model labels into their docstrings at registration time.

**Tech Stack:** SQLite (aiosqlite), FastAPI, Jinja2 templates,
HTMX + Alpine.js for the inline panels, litellm for completions,
FastMCP for the MCP server, pytest-asyncio for tests.

**Spec:** `docs/superpowers/specs/2026-05-20-multi-model-resummary-design.md`

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `app/repos/llm_models.py` | CRUD + default-row management for `llm_models`. Single source of truth for model lookup. |
| `tests/test_repos_llm_models.py` | Unit tests for the repo (insert, list, get_default, set_default, delete, FK behaviour). |
| `tests/test_db_migration_llm_models.py` | Migration test: existing `settings.llm_model` becomes a default row. |

### Modified files

| Path | What changes |
|---|---|
| `app/db.py` | New `llm_models` table in `SCHEMA`, migration in `_run_migrations` (create table, migrate settings, add `jobs` columns). |
| `app/models.py` | New `LlmModel` dataclass; two new fields on `Job`. |
| `app/repos/jobs.py` | `enqueue` gains override kwargs; `_row_to_job` reads the new columns. |
| `app/pipeline.py` | `process_video` takes override kwargs; resolves via `llm_models_repo`; passes `additional_prompt` to `summarize()`. |
| `app/services/summarizer.py` | Prompt builders + `summarize()` accept `additional_prompt`. |
| `app/services/chat.py` | `stream_reply` signature unchanged; routes layer resolves model row. |
| `app/services/api.py` | `submit_video`, `reindex_video`, `chat_about_video` thread overrides through. |
| `app/worker.py` | Type alias + call site pass override kwargs. |
| `app/routes/settings.py` | Render new card; add/edit/delete/default/test endpoints; drop old `quick-setup`/`test-llm`. |
| `app/routes/videos.py` | `reindex_video` accepts override form fields; `video_detail` passes models list to template. |
| `app/routes/chat.py` | Accepts `llm_model_id` form field; resolves via repo. |
| `app/routes/mcp.py` | Bake model labels into docstrings; add `list_models` + `resummarize` tools; `submit_url` gains override params. |
| `app/templates/settings.html` | New "Configured models" card; Quick Setup becomes Add/Edit form; remove manual LLM card. |
| `app/templates/video_detail.html` | Inline resummary panel with model select + textarea; chat form gets model select. |

### Untouched

`app/repos/settings.py` (still owns whisper, language, TTS, playlist).
`app/scheduler.py`, `app/tts_worker.py`, `app/services/embeddings*.py`.

---

## Task Sequencing

Tasks 1-4 build the data layer end-to-end (table, repo, migration,
tests). Tasks 5-7 thread the override through pipeline/worker. Tasks
8-10 expose it in the HTTP routes. Task 11 covers the settings UI.
Task 12 covers the video-detail UI. Tasks 13-15 cover chat + MCP.

Each task ends with a commit so a partial implementation is recoverable.

---

### Task 1: Data layer — schema and `LlmModel` dataclass

**Files:**
- Modify: `app/models.py`
- Modify: `app/db.py:1-180` (SCHEMA constant)
- Test: `tests/test_db.py` (add a small smoke test for fresh-schema shape)

- [ ] **Step 1: Add the `LlmModel` dataclass to `app/models.py`**

Append below the existing `User` dataclass:

```python
@dataclass
class LlmModel:
    id: int
    label: str
    provider_id: str
    model: str
    api_key: str
    base_url: str
    is_default: bool
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 2: Add the table to `SCHEMA` in `app/db.py`**

Place this block in the `SCHEMA` string, after the `users` table and
before the `videos` table (it's referenced from `jobs` only, so order
amongst peers doesn't matter — keep it grouped with config-ish tables):

```sql
CREATE TABLE IF NOT EXISTS llm_models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT    NOT NULL,
    provider_id TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    api_key     TEXT    NOT NULL DEFAULT '',
    base_url    TEXT    NOT NULL DEFAULT '',
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_models_default
    ON llm_models(is_default) WHERE is_default = 1;
```

- [ ] **Step 3: Add `llm_model_id` and `additional_prompt` to the `jobs` table in `SCHEMA`**

Locate the existing `CREATE TABLE IF NOT EXISTS jobs (...)` block in
the `SCHEMA` constant. Inside the column list, add these two columns
right before the `FOREIGN KEY` clause:

```sql
    llm_model_id      INTEGER REFERENCES llm_models(id) ON DELETE SET NULL,
    additional_prompt TEXT,
```

- [ ] **Step 4: Add a smoke test for the new shape in `tests/test_db.py`**

Append:

```python
async def test_schema_contains_llm_models(db):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_models'"
    )
    assert await cursor.fetchone() is not None
    cursor = await db.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert "llm_model_id" in cols
    assert "additional_prompt" in cols
```

- [ ] **Step 5: Run the smoke test**

```
pytest tests/test_db.py::test_schema_contains_llm_models -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/db.py tests/test_db.py
git commit -m "feat(db): add llm_models table + jobs override columns to SCHEMA"
```

---

### Task 2: Data layer — `llm_models` repo (TDD)

**Files:**
- Create: `app/repos/llm_models.py`
- Create: `tests/test_repos_llm_models.py`

- [ ] **Step 1: Write the failing tests file**

Create `tests/test_repos_llm_models.py`:

```python
import aiosqlite
import pytest

from app.repos import llm_models as repo


async def test_insert_first_row_can_be_default(db: aiosqlite.Connection):
    new_id = await repo.insert(
        db,
        label="Claude",
        provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="sk-xxx",
        base_url="",
        make_default=True,
    )
    row = await repo.get(db, new_id)
    assert row is not None
    assert row.label == "Claude"
    assert row.is_default is True


async def test_get_default_returns_none_when_empty(db: aiosqlite.Connection):
    assert await repo.get_default(db) is None


async def test_get_default_returns_the_default_row(db: aiosqlite.Connection):
    await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    second = await repo.insert(
        db, label="B", provider_id="ollama", model="ollama_chat/llama3.1",
        api_key="", base_url="http://lan:11434", make_default=False,
    )
    default = await repo.get_default(db)
    assert default is not None
    assert default.label == "A"
    # Sanity: the non-default row is reachable too.
    row = await repo.get(db, second)
    assert row is not None and row.is_default is False


async def test_set_default_flips_atomically(db: aiosqlite.Connection):
    a = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    b = await repo.insert(
        db, label="B", provider_id="ollama", model="ollama_chat/llama3.1",
        api_key="", base_url="http://lan:11434", make_default=False,
    )
    await repo.set_default(db, b)
    default = await repo.get_default(db)
    assert default is not None and default.id == b
    # The previous default lost the flag.
    row_a = await repo.get(db, a)
    assert row_a is not None and row_a.is_default is False


async def test_list_all_orders_default_first_then_label(db: aiosqlite.Connection):
    await repo.insert(
        db, label="Zeta",  provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=False,
    )
    await repo.insert(
        db, label="Alpha", provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="k", base_url="", make_default=True,
    )
    await repo.insert(
        db, label="Beta",  provider_id="groq",
        model="groq/llama-3.3-70b-versatile",
        api_key="k", base_url="", make_default=False,
    )
    rows = await repo.list_all(db)
    assert [r.label for r in rows] == ["Alpha", "Beta", "Zeta"]


async def test_update_changes_fields_in_place(db: aiosqlite.Connection):
    rid = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    await repo.update(
        db, rid,
        label="A renamed",
        model="openai/gpt-5.4",
        api_key="new-key",
        base_url="",
    )
    row = await repo.get(db, rid)
    assert row is not None
    assert row.label == "A renamed"
    assert row.model == "openai/gpt-5.4"
    assert row.api_key == "new-key"
    assert row.is_default is True  # unchanged


async def test_delete_non_default_row(db: aiosqlite.Connection):
    a = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    b = await repo.insert(
        db, label="B", provider_id="ollama",
        model="ollama_chat/llama3.1", api_key="", base_url="x",
        make_default=False,
    )
    await repo.delete(db, b)
    assert await repo.get(db, b) is None
    # Default row is untouched.
    assert await repo.get(db, a) is not None


async def test_delete_default_row_raises(db: aiosqlite.Connection):
    a = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    with pytest.raises(ValueError):
        await repo.delete(db, a)
    # Row still there.
    assert await repo.get(db, a) is not None
```

- [ ] **Step 2: Run the tests to confirm they fail**

```
pytest tests/test_repos_llm_models.py -v
```

Expected: ImportError / module not found.

- [ ] **Step 3: Implement the repo**

Create `app/repos/llm_models.py`:

```python
"""CRUD for the llm_models table.

A single global registry of configured LLM "profiles" (provider + key
+ base_url + model id + human label). Exactly one row carries
``is_default=1`` (enforced by the partial unique index in SCHEMA).
The pipeline, worker, chat service and MCP server all resolve the
target model through this repo — no other code path should read the
old ``settings.llm_model`` keys (they no longer exist).
"""

from datetime import datetime

import aiosqlite

from app.models import LlmModel


def _row_to_model(row: aiosqlite.Row) -> LlmModel:
    return LlmModel(
        id=row["id"],
        label=row["label"],
        provider_id=row["provider_id"],
        model=row["model"],
        api_key=row["api_key"],
        base_url=row["base_url"],
        is_default=bool(row["is_default"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


async def get(db: aiosqlite.Connection, model_id: int) -> LlmModel | None:
    cursor = await db.execute(
        "SELECT * FROM llm_models WHERE id=?", (model_id,)
    )
    row = await cursor.fetchone()
    return _row_to_model(row) if row else None


async def get_default(db: aiosqlite.Connection) -> LlmModel | None:
    cursor = await db.execute(
        "SELECT * FROM llm_models WHERE is_default=1 LIMIT 1"
    )
    row = await cursor.fetchone()
    return _row_to_model(row) if row else None


async def list_all(db: aiosqlite.Connection) -> list[LlmModel]:
    """Return all configured models. Default row first, then alphabetical
    by label (case-insensitive). Empty list on a fresh install."""
    cursor = await db.execute(
        """
        SELECT * FROM llm_models
        ORDER BY is_default DESC, LOWER(label) ASC, id ASC
        """
    )
    rows = await cursor.fetchall()
    return [_row_to_model(r) for r in rows]


async def insert(
    db: aiosqlite.Connection,
    *,
    label: str,
    provider_id: str,
    model: str,
    api_key: str,
    base_url: str,
    make_default: bool,
) -> int:
    """Insert a new row. When ``make_default=True``, clears any existing
    default first so the partial unique index never fires."""
    if make_default:
        await db.execute("UPDATE llm_models SET is_default=0 WHERE is_default=1")
    cursor = await db.execute(
        """
        INSERT INTO llm_models
            (label, provider_id, model, api_key, base_url, is_default)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (label, provider_id, model, api_key, base_url, 1 if make_default else 0),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def update(
    db: aiosqlite.Connection,
    model_id: int,
    *,
    label: str,
    model: str,
    api_key: str,
    base_url: str,
) -> None:
    """Update the user-facing fields. is_default is NOT modified here —
    use set_default() for that to keep the invariant transactional."""
    await db.execute(
        """
        UPDATE llm_models
        SET label=?, model=?, api_key=?, base_url=?,
            updated_at=datetime('now')
        WHERE id=?
        """,
        (label, model, api_key, base_url, model_id),
    )
    await db.commit()


async def set_default(db: aiosqlite.Connection, model_id: int) -> None:
    """Flip the default flag onto ``model_id``. Two UPDATEs in one
    commit — single-writer SQLite makes this race-free."""
    await db.execute("UPDATE llm_models SET is_default=0 WHERE is_default=1")
    await db.execute(
        "UPDATE llm_models SET is_default=1, updated_at=datetime('now') WHERE id=?",
        (model_id,),
    )
    await db.commit()


async def delete(db: aiosqlite.Connection, model_id: int) -> None:
    """Delete a non-default row. Raises ValueError if the row is the
    current default — callers must move the default first."""
    row = await get(db, model_id)
    if row is None:
        return  # idempotent
    if row.is_default:
        raise ValueError(
            f"Cannot delete default model {model_id} — "
            "make another model default first."
        )
    await db.execute("DELETE FROM llm_models WHERE id=?", (model_id,))
    await db.commit()
```

- [ ] **Step 4: Run the tests**

```
pytest tests/test_repos_llm_models.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/repos/llm_models.py tests/test_repos_llm_models.py
git commit -m "feat(repos): add llm_models repo with default-row invariant"
```

---

### Task 3: Data layer — migration from `settings.llm_model`

**Files:**
- Modify: `app/db.py` (`_run_migrations`)
- Create: `tests/test_db_migration_llm_models.py`

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_llm_models.py`:

```python
"""Migration test: a pre-existing settings.llm_model row becomes a
default llm_models entry, and the old settings keys are deleted."""

import aiosqlite
import pytest

from app.db import _run_migrations
from app.repos import llm_models as llm_models_repo
from app.repos import settings as settings_repo


async def _make_legacy_db(path: str) -> aiosqlite.Connection:
    """Build a stripped-down DB containing only the legacy settings shape
    the migration cares about — enough to exercise the migration code
    without depending on the full SCHEMA's older shape."""
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("""
        CREATE TABLE settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        )
    """)
    await conn.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            state TEXT NOT NULL,
            step TEXT NOT NULL DEFAULT '',
            error_message TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await conn.commit()
    return conn


async def test_migration_creates_default_row_from_settings(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "legacy.db"))
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_model', 'anthropic/claude-sonnet-4-6')"
    )
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_api_key', 'sk-test')"
    )
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_base_url', '')"
    )
    await conn.commit()

    await _run_migrations(conn)

    row = await llm_models_repo.get_default(conn)
    assert row is not None
    assert row.model == "anthropic/claude-sonnet-4-6"
    assert row.api_key == "sk-test"
    assert row.provider_id == "anthropic"
    assert row.label == "Anthropic"
    # Legacy keys are gone.
    assert await settings_repo.get(conn, "llm_model") is None
    assert await settings_repo.get(conn, "llm_api_key") is None
    assert await settings_repo.get(conn, "llm_base_url") is None
    await conn.close()


async def test_migration_skips_when_no_legacy_settings(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "fresh.db"))
    await _run_migrations(conn)
    assert await llm_models_repo.get_default(conn) is None
    rows = await llm_models_repo.list_all(conn)
    assert rows == []
    await conn.close()


async def test_migration_adds_jobs_override_columns(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "jobs.db"))
    await _run_migrations(conn)
    cursor = await conn.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert "llm_model_id" in cols
    assert "additional_prompt" in cols
    await conn.close()


async def test_migration_is_idempotent(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "idem.db"))
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_model', 'openai/gpt-5.5')"
    )
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_api_key', 'k')"
    )
    await conn.commit()
    await _run_migrations(conn)
    await _run_migrations(conn)  # second run must be a no-op
    rows = await llm_models_repo.list_all(conn)
    assert len(rows) == 1
    await conn.close()


async def test_migration_provider_id_falls_back_to_custom(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "custom.db"))
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_model', 'mystery/foo-1')"
    )
    await conn.commit()
    await _run_migrations(conn)
    row = await llm_models_repo.get_default(conn)
    assert row is not None
    assert row.provider_id == "custom"
    assert row.label == "Custom"
    await conn.close()
```

- [ ] **Step 2: Run the test to confirm it fails**

```
pytest tests/test_db_migration_llm_models.py -v
```

Expected: tests fail because `_run_migrations` doesn't yet create
`llm_models` from legacy settings.

- [ ] **Step 3: Implement the migration in `app/db.py`**

Find `_run_migrations`. After the existing migration blocks (and
before the function ends), append a new block:

```python
    # ── Multi-model migration (V8) ──────────────────────────────
    #
    # Move the legacy single LLM config (settings.llm_model /
    # llm_api_key / llm_base_url) into a managed llm_models table
    # with a default row. Adds two override columns to `jobs` so
    # the Re-summarize endpoint can carry a per-run model + prompt
    # tweak through to the worker. Idempotent — each step is gated
    # on the relevant feature check.
    if not await _table_exists(conn, "llm_models"):
        await conn.execute(
            """
            CREATE TABLE llm_models (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT    NOT NULL,
                provider_id TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                api_key     TEXT    NOT NULL DEFAULT '',
                base_url    TEXT    NOT NULL DEFAULT '',
                is_default  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.execute(
            "CREATE UNIQUE INDEX idx_llm_models_default "
            "ON llm_models(is_default) WHERE is_default = 1"
        )
        await conn.commit()

    await _ensure_column(conn, "jobs", "llm_model_id", "INTEGER")
    await _ensure_column(conn, "jobs", "additional_prompt", "TEXT")

    # Backfill from legacy settings keys, but only once: if any row
    # already exists in llm_models, the migration has already run
    # (or the user has added a model manually) — leave it alone.
    cursor = await conn.execute("SELECT COUNT(*) FROM llm_models")
    row = await cursor.fetchone()
    if row is not None and row[0] == 0:
        cursor = await conn.execute(
            "SELECT key, value FROM settings WHERE user_id=1 AND key IN "
            "('llm_model','llm_api_key','llm_base_url')"
        )
        legacy = {r[0]: r[1] for r in await cursor.fetchall()}
        legacy_model = (legacy.get("llm_model") or "").strip()
        if legacy_model:
            from app.services.providers import PROVIDER_PRESETS

            head = legacy_model.split("/", 1)[0]
            provider_id = "custom"
            label = "Custom"
            for preset_id, preset in PROVIDER_PRESETS.items():
                if head == preset.litellm_provider or head.startswith(
                    preset.litellm_provider
                ):
                    provider_id = preset_id
                    label = preset.name
                    break
            await conn.execute(
                """
                INSERT INTO llm_models
                    (label, provider_id, model, api_key, base_url, is_default)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    label,
                    provider_id,
                    legacy_model,
                    legacy.get("llm_api_key", ""),
                    legacy.get("llm_base_url", ""),
                ),
            )
            await conn.execute(
                "DELETE FROM settings WHERE user_id=1 AND key IN "
                "('llm_model','llm_api_key','llm_base_url')"
            )
            await conn.commit()
```

- [ ] **Step 4: Run the migration tests**

```
pytest tests/test_db_migration_llm_models.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Run the full test suite to catch incidental breakage**

```
pytest -x -q
```

Expected: failures concentrated in routes/services that still read
`settings.get("llm_model")` (we'll fix those in later tasks). At this
stage **only the repo + migration tests must pass cleanly**; other
failures are tolerated *for this commit only*.

- [ ] **Step 6: Commit**

```bash
git add app/db.py tests/test_db_migration_llm_models.py
git commit -m "feat(db): migrate legacy settings.llm_model into llm_models"
```

---

### Task 4: Jobs repo — accept override kwargs

**Files:**
- Modify: `app/models.py` (`Job` dataclass)
- Modify: `app/repos/jobs.py`
- Modify: `tests/test_repos_jobs.py`

- [ ] **Step 1: Extend the `Job` dataclass**

In `app/models.py`, modify `Job`:

```python
@dataclass
class Job:
    id: int
    video_id: str
    state: JobState
    step: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    llm_model_id: int | None = None
    additional_prompt: str | None = None
```

- [ ] **Step 2: Update `_row_to_job` and `enqueue` in `app/repos/jobs.py`**

```python
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
```

- [ ] **Step 3: Add tests for the new behaviour to `tests/test_repos_jobs.py`**

Append:

```python
from app.repos import llm_models as llm_models_repo


async def test_enqueue_without_overrides_defaults_to_null(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.llm_model_id is None
    assert job.additional_prompt is None


async def test_enqueue_with_overrides_persists_them(db: aiosqlite.Connection):
    await _video(db)
    mid = await llm_models_repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    job_id = await jobs_repo.enqueue(
        db, "v1",
        llm_model_id=mid,
        additional_prompt="be terse",
    )
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.llm_model_id == mid
    assert job.additional_prompt == "be terse"
```

- [ ] **Step 4: Run jobs-repo tests**

```
pytest tests/test_repos_jobs.py -v
```

Expected: all existing tests still pass + 2 new ones PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/repos/jobs.py tests/test_repos_jobs.py
git commit -m "feat(jobs): persist optional llm_model_id + additional_prompt on enqueue"
```

---

### Task 5: Summarizer — accept `additional_prompt`

**Files:**
- Modify: `app/services/summarizer.py`
- Modify: `tests/test_services_summarizer.py` (create if not present)

- [ ] **Step 1: Check whether `tests/test_services_summarizer.py` exists**

```
ls tests/test_services_summarizer.py 2>/dev/null || echo "missing"
```

If missing, create it with the imports header:

```python
"""Unit tests for the prompt builders. The summarize() function itself
hits litellm and is exercised by the integration tests; here we just
exercise pure-string assembly."""

from app.services.summarizer import build_reduce_prompt, build_system_prompt
```

- [ ] **Step 2: Add a failing test**

Append:

```python
def test_build_system_prompt_appends_additional_prompt_block():
    out = build_system_prompt(
        language="en",
        custom_system_prompt=None,
        with_timestamps=False,
        additional_prompt="be terse and quote dollar amounts",
    )
    assert "USER OVERRIDE FOR THIS RUN:" in out
    assert "be terse and quote dollar amounts" in out
    # Override block lives at the END so it overrides earlier
    # instructions in the model's attention budget.
    assert out.rstrip().endswith("be terse and quote dollar amounts")


def test_build_system_prompt_omits_block_when_no_override():
    out = build_system_prompt(
        language="en",
        custom_system_prompt=None,
        with_timestamps=False,
        additional_prompt=None,
    )
    assert "USER OVERRIDE FOR THIS RUN" not in out


def test_build_reduce_prompt_appends_additional_prompt_block():
    out = build_reduce_prompt(
        language="en",
        with_timestamps=False,
        additional_prompt="answer in bullet points only",
    )
    assert "USER OVERRIDE FOR THIS RUN:" in out
    assert "answer in bullet points only" in out
```

- [ ] **Step 3: Run the tests to confirm they fail**

```
pytest tests/test_services_summarizer.py -v
```

Expected: TypeError — `additional_prompt` is not a parameter.

- [ ] **Step 4: Wire `additional_prompt` into the summarizer**

In `app/services/summarizer.py`, add the helper near the other module
constants (right after `_TIMESTAMP_INSTRUCTION`):

```python
def _additional_prompt_block(additional_prompt: str | None) -> str:
    text = (additional_prompt or "").strip()
    if not text:
        return ""
    return (
        "USER OVERRIDE FOR THIS RUN:\n"
        f"{text}"
    )
```

Modify `build_system_prompt` to accept and append the block. Change
the signature:

```python
def build_system_prompt(
    *,
    language: str | None,
    custom_system_prompt: str | None = None,
    with_timestamps: bool = False,
    additional_prompt: str | None = None,
) -> str:
```

Inside the function, after the existing return-string assembly,
replace the two `return ...` statements so they end with the override
block. The cleanest way: compute `override = _additional_prompt_block(additional_prompt)`
at the top of the function, then change each `return` to suffix
`+ (override and "\n\n" + override + "\n" or "")`. Concretely:

```python
def build_system_prompt(
    *,
    language: str | None,
    custom_system_prompt: str | None = None,
    with_timestamps: bool = False,
    additional_prompt: str | None = None,
) -> str:
    """... (keep the existing docstring; add a line:)

    additional_prompt: optional one-shot override appended at the very
        end of the system prompt, marked with a ``USER OVERRIDE FOR
        THIS RUN:`` header. Used by the Re-summarize panel to bias the
        next single run without persisting anything.
    """
    custom = (custom_system_prompt or "").strip()
    timestamp_block = _TIMESTAMP_INSTRUCTION if with_timestamps else ""
    override_block = _additional_prompt_block(additional_prompt)
    override_suffix = f"\n\n{override_block}" if override_block else ""

    if custom:
        return (
            f"{_language_directive(language)}\n"
            "OUTPUT FORMAT: Markdown. Tables with `| col | col |` "
            "syntax (plus a `|---|---|` separator row) render as "
            "proper HTML tables.\n\n"
            f"{custom}\n\n"
            f"{timestamp_block}"
        ).rstrip() + "\n" + override_suffix
    return (
        # ── (keep the entire existing standard-prompt body verbatim) ──
        # The trailing line of the existing string is the WHAT TO IGNORE
        # block; we keep it and add the override after.
        # (Body unchanged from the current file — do not retype it; this
        # comment is a marker for the engineer to leave that string
        # alone and just suffix `+ override_suffix` at the return.)
    ) + override_suffix
```

> **Engineer note:** the standard-prompt return string is ~80 lines
> and unchanged. Don't retype it. Just append ` + override_suffix` at
> the end of the existing `return (...)` expression. Same trick for the
> `custom` branch above.

Modify `build_reduce_prompt` the same way:

```python
def build_reduce_prompt(
    *,
    language: str | None,
    with_timestamps: bool = False,
    additional_prompt: str | None = None,
) -> str:
    """... (keep docstring, add line about additional_prompt) ..."""
    # Compute override_suffix the same way; append at end of return.
    override_block = _additional_prompt_block(additional_prompt)
    override_suffix = f"\n\n{override_block}" if override_block else ""
    # ... existing function body, but the final `).rstrip()` becomes:
    # ).rstrip() + override_suffix
```

- [ ] **Step 5: Thread `additional_prompt` through `summarize()`**

Add the param to `summarize`:

```python
async def summarize(
    *,
    transcript: str,
    model: str,
    api_key: str,
    base_url: str | None,
    title: str = "",
    description: str = "",
    language: str | None = None,
    custom_system_prompt: str | None = None,
    playlist_context: list[str] | None = None,
    transcript_segments: list[dict] | None = None,
    additional_prompt: str | None = None,
    progress: ProgressCb | None = None,
    on_partial: Callable[[str], Awaitable[None]] | None = None,
) -> str:
```

Pass it through:

```python
    system_prompt = build_system_prompt(
        language=language,
        custom_system_prompt=custom_system_prompt,
        with_timestamps=has_segments,
        additional_prompt=additional_prompt,
    )
    reduce_prompt = build_reduce_prompt(
        language=language,
        with_timestamps=has_segments,
        additional_prompt=additional_prompt,
    )
```

- [ ] **Step 6: Run the summarizer tests**

```
pytest tests/test_services_summarizer.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/summarizer.py tests/test_services_summarizer.py
git commit -m "feat(summarizer): accept optional additional_prompt one-shot override"
```

---

### Task 6: Pipeline — resolve model row, pass override to summarize

**Files:**
- Modify: `app/pipeline.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Read the existing pipeline test patterns**

```
grep -n "monkeypatch\|process_video\|llm_model" tests/test_pipeline.py | head -20
```

Note the existing pattern (monkeypatching `summarizer.summarize` etc.).
We follow it.

- [ ] **Step 2: Add a failing test for the override path**

Append to `tests/test_pipeline.py`:

```python
from app.repos import llm_models as llm_models_repo


async def test_pipeline_uses_override_model_when_set(
    db: aiosqlite.Connection, config, monkeypatch
):
    """When the job's llm_model_id is set, pipeline must resolve that
    row (not the default) and pass its model + key + base_url to
    summarize()."""
    from app import pipeline as pipeline_mod

    # Set up two models — default is "ollama"; override is "anthropic".
    await llm_models_repo.insert(
        db, label="Ollama", provider_id="ollama",
        model="ollama_chat/llama3.1",
        api_key="", base_url="http://lan:11434",
        make_default=True,
    )
    override_id = await llm_models_repo.insert(
        db, label="Claude", provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="sk-test", base_url="",
        make_default=False,
    )

    # Insert a video + transcript so summarize() actually gets called.
    from app.models import TranscriptSource, VideoKind
    from app.repos import videos as videos_repo
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="https://example.test",
        title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.WEB, user_id=1,
    )
    await videos_repo.set_transcript(
        db, "v1", "body text", TranscriptSource.WEB,
    )

    captured: dict = {}

    async def fake_summarize(**kw):
        captured.update(kw)
        return "S"

    monkeypatch.setattr(pipeline_mod, "summarize", fake_summarize)

    async def noop_step(_: str) -> None: ...

    await pipeline_mod.process_video(
        db, config, "v1", noop_step,
        llm_model_id=override_id,
        additional_prompt="be terse",
    )

    assert captured["model"] == "anthropic/claude-sonnet-4-6"
    assert captured["api_key"] == "sk-test"
    assert captured["base_url"] is None
    assert captured["additional_prompt"] == "be terse"


async def test_pipeline_falls_back_to_default_when_no_override(
    db: aiosqlite.Connection, config, monkeypatch
):
    from app import pipeline as pipeline_mod

    await llm_models_repo.insert(
        db, label="Claude", provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="default-key", base_url="",
        make_default=True,
    )
    from app.models import TranscriptSource, VideoKind
    from app.repos import videos as videos_repo
    await videos_repo.upsert_metadata(
        db, video_id="v2", url="https://example.test",
        title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.WEB, user_id=1,
    )
    await videos_repo.set_transcript(
        db, "v2", "body", TranscriptSource.WEB,
    )

    captured: dict = {}

    async def fake_summarize(**kw):
        captured.update(kw)
        return "S"

    monkeypatch.setattr(pipeline_mod, "summarize", fake_summarize)

    async def noop_step(_: str) -> None: ...

    await pipeline_mod.process_video(db, config, "v2", noop_step)

    assert captured["model"] == "anthropic/claude-sonnet-4-6"
    assert captured["api_key"] == "default-key"
    assert captured["additional_prompt"] is None
```

- [ ] **Step 3: Run tests, expect failure**

```
pytest tests/test_pipeline.py -k "override or fallback_to_default" -v
```

Expected: signature error / KeyError (`additional_prompt`).

- [ ] **Step 4: Modify `process_video` in `app/pipeline.py`**

Change the signature:

```python
async def process_video(
    db: aiosqlite.Connection,
    config: Config,
    video_id: str,
    set_step: Callable[[str], Awaitable[None]],
    *,
    llm_model_id: int | None = None,
    additional_prompt: str | None = None,
) -> None:
```

Replace the block that reads `model`, `api_key`, `base_url` from
`settings` (currently four lines just below `settings = await
settings_repo.get_all(db)`). New block:

```python
    settings = await settings_repo.get_all(db)
    whisper_model = settings.get("whisper_model", "small")

    # Resolve which LLM to use. Background work (auto-import, initial
    # submit) passes llm_model_id=None and we use the default row.
    # The Re-summarize panel may override either or both fields.
    from app.repos import llm_models as llm_models_repo
    model_row = (
        await llm_models_repo.get(db, llm_model_id)
        if llm_model_id is not None
        else await llm_models_repo.get_default(db)
    )
    model = model_row.model if model_row else None
    api_key = model_row.api_key if model_row else ""
    base_url = (model_row.base_url or None) if model_row else None
```

(Delete the now-redundant `settings.get("llm_model")` lines.)

And at the call to `summarize(...)`, add the override:

```python
    summary = await summarize(
        transcript=text,
        model=model,
        api_key=api_key or "",
        base_url=base_url,
        title=video.title,
        description=video.description,
        language=summary_language_setting or None,
        custom_system_prompt=custom_prompt,
        playlist_context=playlist_context or None,
        transcript_segments=segments,
        additional_prompt=additional_prompt,
        progress=set_step,
        on_partial=_persist_partial,
    )
```

> **Engineer note:** there's a second `litellm.acompletion` call in
> `pipeline.py` (language detection fallback). That should also use the
> resolved `model` / `api_key` / `base_url` — it already does, since
> those variables are still in scope. No change needed there.

- [ ] **Step 5: Run the pipeline tests**

```
pytest tests/test_pipeline.py -v
```

Expected: pre-existing tests still pass + the two new ones PASS.

Some pre-existing tests may insert no `llm_models` row but expect
processing to behave like the old "no model" path. The new code
returns early with the same `transcript only` message when
`model_row is None`. That matches the previous `not model` branch.

- [ ] **Step 6: Commit**

```bash
git add app/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): resolve LLM via llm_models repo + thread overrides"
```

---

### Task 7: Worker — pass override fields from claimed job

**Files:**
- Modify: `app/worker.py`
- Modify: `tests/test_worker.py` (extend) — create if not present

- [ ] **Step 1: Update the `ProcessVideo` type alias and call site**

In `app/worker.py`, change the alias:

```python
ProcessVideo = Callable[
    ...,  # too many kwargs — use ellipsis
    Awaitable[None],
]
```

Then change the call in `_run_iteration`:

```python
            await self._process_video(
                self._db, self._config, job.video_id, set_step,
                llm_model_id=job.llm_model_id,
                additional_prompt=job.additional_prompt,
            )
```

- [ ] **Step 2: Confirm via existing pipeline/worker tests**

```
pytest tests/test_pipeline.py tests/test_worker.py -v 2>&1 | tail -40
```

(`test_worker.py` may not exist; that's fine — the pipeline tests
already exercise `process_video` directly.)

Expected: all green. If `test_worker.py` exists and instantiates
`Worker` with a stub `process_video`, the stub may need to accept
`**kwargs`. Fix any failing test accordingly.

- [ ] **Step 3: Commit**

```bash
git add app/worker.py tests/test_worker.py 2>/dev/null
git commit -m "feat(worker): forward llm_model_id + additional_prompt to pipeline"
```

(If `test_worker.py` doesn't exist, only `app/worker.py` is staged.)

---

### Task 8: Routes — `reindex` accepts overrides

**Files:**
- Modify: `app/routes/videos.py`
- Modify: `tests/test_routes_videos.py`

- [ ] **Step 1: Add a failing route test**

Append to `tests/test_routes_videos.py`:

```python
async def test_reindex_with_overrides_persists_them_on_job(
    db, async_client
):
    # Insert a model + video so the route succeeds.
    from app.models import VideoKind
    from app.repos import llm_models as llm_models_repo
    from app.repos import videos as videos_repo

    mid = await llm_models_repo.insert(
        db, label="X", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.YOUTUBE, user_id=1,
    )
    resp = await async_client.post(
        "/v/v1/reindex",
        data={
            "llm_model_id": str(mid),
            "additional_prompt": "be terse",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from app.repos import jobs as jobs_repo
    job = await jobs_repo.latest_for_video(db, "v1")
    assert job is not None
    assert job.llm_model_id == mid
    assert job.additional_prompt == "be terse"


async def test_reindex_without_overrides_leaves_them_null(db, async_client):
    from app.models import VideoKind
    from app.repos import videos as videos_repo

    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.YOUTUBE, user_id=1,
    )
    resp = await async_client.post(
        "/v/v1/reindex", data={}, follow_redirects=False,
    )
    assert resp.status_code == 303

    from app.repos import jobs as jobs_repo
    job = await jobs_repo.latest_for_video(db, "v1")
    assert job is not None
    assert job.llm_model_id is None
    assert job.additional_prompt is None
```

- [ ] **Step 2: Run, expect failure**

```
pytest tests/test_routes_videos.py -k reindex -v
```

Expected: 400 or 200 — fields aren't read; `llm_model_id` on the job
is None.

- [ ] **Step 3: Modify `reindex_video` route**

In `app/routes/videos.py`:

```python
@router.post("/v/{video_id}/reindex")
async def reindex_video(
    video_id: str,
    llm_model_id: str = Form(""),
    additional_prompt: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != current_user_id:
        raise HTTPException(404)
    # Empty/whitespace fields → no override (background pipeline picks
    # the default model). Anything else is forwarded to the worker via
    # the new jobs columns.
    model_id_int: int | None = None
    if llm_model_id.strip():
        try:
            model_id_int = int(llm_model_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid llm_model_id: {e}")
    prompt = additional_prompt.strip() or None
    await jobs_repo.enqueue(
        db, video_id,
        llm_model_id=model_id_int,
        additional_prompt=prompt,
    )
    return RedirectResponse(f"/v/{video_id}", status_code=303)
```

- [ ] **Step 4: Run the route tests**

```
pytest tests/test_routes_videos.py -k reindex -v
```

Expected: PASS.

- [ ] **Step 5: Pass `llm_models` to the detail template**

Modify the `video_detail` handler at the bottom of `routes/videos.py`:

```python
    audio_renderings = await tts_jobs_repo.list_for_video(db, video_id)
    from app.repos import llm_models as llm_models_repo
    llm_models = await llm_models_repo.list_all(db)
    return templates.TemplateResponse(
        request,
        "video_detail.html",
        {
            "video": video,
            "summary_html": summary_html,
            "chat_history": history,
            "job": job,
            "video_tags": video_tags,
            "elapsed_s": _elapsed_seconds(job),
            "transcript_blocks": transcript_blocks,
            "current_user": current_user,
            "renderings": audio_renderings,
            "llm_models": llm_models,
        },
    )
```

- [ ] **Step 6: Commit**

```bash
git add app/routes/videos.py tests/test_routes_videos.py
git commit -m "feat(routes): reindex accepts llm_model_id + additional_prompt"
```

---

### Task 9: Routes — chat accepts `llm_model_id`

**Files:**
- Modify: `app/routes/chat.py`
- Modify: `tests/test_routes_chat.py`

- [ ] **Step 1: Add a failing test**

Append to `tests/test_routes_chat.py` (look at the existing pattern
for how `stream_reply` is monkeypatched — match it):

```python
async def test_chat_uses_override_model_when_id_supplied(
    db, async_client, monkeypatch,
):
    from app.models import TranscriptSource, VideoKind
    from app.repos import llm_models as llm_models_repo
    from app.repos import videos as videos_repo

    await llm_models_repo.insert(
        db, label="Default", provider_id="ollama",
        model="ollama_chat/llama3.1", api_key="",
        base_url="http://lan:11434", make_default=True,
    )
    override_id = await llm_models_repo.insert(
        db, label="Claude", provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="sk-claude", base_url="",
        make_default=False,
    )
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.YOUTUBE, user_id=1,
    )
    await videos_repo.set_transcript(
        db, "v1", "body", TranscriptSource.AUTO_SUBS,
    )

    captured: dict = {}

    async def fake_stream(**kw):
        captured.update(kw)
        if False:
            yield ""  # make this an async generator
        return

    from app.routes import chat as chat_routes
    monkeypatch.setattr(chat_routes, "stream_reply", fake_stream)

    resp = await async_client.post(
        "/v/v1/chat",
        data={"content": "hi", "llm_model_id": str(override_id)},
    )
    assert resp.status_code == 200
    assert captured["model"] == "anthropic/claude-sonnet-4-6"
    assert captured["api_key"] == "sk-claude"
```

- [ ] **Step 2: Run, expect failure**

```
pytest tests/test_routes_chat.py -k override_model -v
```

Expected: 400 "LLM not configured" or wrong model.

- [ ] **Step 3: Modify `post_chat` in `app/routes/chat.py`**

```python
@router.post("/v/{video_id}/chat", response_class=HTMLResponse)
async def post_chat(
    video_id: str,
    content: str = Form(...),
    llm_model_id: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await videos_repo.get(db, video_id)
    if video is None or video.transcript is None:
        raise HTTPException(404, "Video or transcript not found")
    if video.user_id != current_user_id:
        raise HTTPException(404, "Video or transcript not found")

    from app.repos import llm_models as llm_models_repo
    chosen_id: int | None = None
    if llm_model_id.strip():
        try:
            chosen_id = int(llm_model_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid llm_model_id: {e}")
    model_row = (
        await llm_models_repo.get(db, chosen_id)
        if chosen_id is not None
        else await llm_models_repo.get_default(db)
    )
    if model_row is None:
        raise HTTPException(400, "LLM not configured")
    model = model_row.model
    api_key = model_row.api_key or ""
    base_url = model_row.base_url or None
```

(Remove the now-redundant `settings = await settings_repo.get_all(db)`
block and the `settings.get("llm_model")` / `settings.get("llm_api_key")`
lookups — they're replaced by the repo lookup above.)

Update the `stream_reply` call to use `base_url` from the row:

```python
        async for token in stream_reply(
            transcript=video.transcript or "",
            history=history,
            user_message=content,
            model=model,
            api_key=api_key,
            base_url=base_url,
        ):
```

Also remove the now-unused `from app.repos import settings as settings_repo` import.

- [ ] **Step 4: Run the chat tests**

```
pytest tests/test_routes_chat.py -v
```

Expected: existing tests still pass + the new one PASSes. Pre-existing
tests that previously seeded `settings.llm_model` must instead seed an
`llm_models` row via `llm_models_repo.insert(..., make_default=True)`.
Update them now.

- [ ] **Step 5: Commit**

```bash
git add app/routes/chat.py tests/test_routes_chat.py
git commit -m "feat(routes): chat accepts optional llm_model_id form field"
```

---

### Task 10: Services — `api.py` threads overrides

**Files:**
- Modify: `app/services/api.py`
- Modify: `tests/test_services_api.py`

- [ ] **Step 1: Add a failing test**

Append to `tests/test_services_api.py`:

```python
async def test_reindex_video_accepts_overrides(db, config):
    from app.models import VideoKind
    from app.repos import llm_models as llm_models_repo
    from app.repos import videos as videos_repo
    from app.services import api as api_svc

    mid = await llm_models_repo.insert(
        db, label="X", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.YOUTUBE, user_id=1,
    )
    await api_svc.reindex_video(
        db, "v1",
        llm_model_id=mid,
        additional_prompt="be terse",
    )
    from app.repos import jobs as jobs_repo
    job = await jobs_repo.latest_for_video(db, "v1")
    assert job is not None
    assert job.llm_model_id == mid
    assert job.additional_prompt == "be terse"
```

- [ ] **Step 2: Run, expect failure**

```
pytest tests/test_services_api.py -k reindex -v
```

Expected: TypeError (unexpected kwargs).

- [ ] **Step 3: Modify `reindex_video` in `app/services/api.py`**

```python
async def reindex_video(
    db: aiosqlite.Connection,
    video_id: str,
    *,
    llm_model_id: int | None = None,
    additional_prompt: str | None = None,
) -> None:
    if await videos_repo.get(db, video_id) is None:
        raise ValueError(f"Unknown video: {video_id}")
    await jobs_repo.enqueue(
        db, video_id,
        llm_model_id=llm_model_id,
        additional_prompt=additional_prompt,
    )
```

Also extend `submit_video` (around line 132): add the same two kwargs,
forward them to `jobs_repo.enqueue` at its single call site inside
`submit_video` (search for `await jobs_repo.enqueue(db, ` in that
function and modify it identically). The kwargs default to `None`, so
existing callers keep working.

`chat_about_video` (search for it in the same file) currently reads
`settings.llm_model` etc. Replace with `llm_models_repo.get_default(db)`
(matching the route change in Task 9), and add an optional
`llm_model_id: int | None = None` kwarg.

- [ ] **Step 4: Run the services tests**

```
pytest tests/test_services_api.py -v
```

Expected: PASS (including pre-existing tests that may need
`llm_models` seeded instead of `settings.llm_model`).

- [ ] **Step 5: Commit**

```bash
git add app/services/api.py tests/test_services_api.py
git commit -m "feat(services/api): thread llm_model_id + additional_prompt overrides"
```

---

### Task 11: Settings UI — Configured models card + endpoints

**Files:**
- Modify: `app/routes/settings.py`
- Modify: `app/templates/settings.html`
- Modify: `tests/test_routes_settings.py`

This is the largest UI task; expect ~150 lines of template + ~120
lines of route code. Take it in three sub-commits.

#### 11a — Backend endpoints

- [ ] **Step 1: Add failing tests for the new endpoints**

Append to `tests/test_routes_settings.py`:

```python
async def test_post_llm_models_inserts_row(db, async_client):
    resp = await async_client.post(
        "/settings/llm-models",
        data={
            "label": "Claude",
            "provider_id": "anthropic",
            "model": "anthropic/claude-sonnet-4-6",
            "api_key": "sk-test",
            "base_url": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    from app.repos import llm_models as llm_models_repo
    rows = await llm_models_repo.list_all(db)
    assert len(rows) == 1
    assert rows[0].label == "Claude"
    # First-ever insert auto-defaults.
    assert rows[0].is_default is True


async def test_post_llm_models_id_updates(db, async_client):
    from app.repos import llm_models as llm_models_repo
    mid = await llm_models_repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    resp = await async_client.post(
        f"/settings/llm-models/{mid}",
        data={
            "label": "A renamed",
            "model": "openai/gpt-5.4",
            "api_key": "",
            "base_url": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await llm_models_repo.get(db, mid)
    assert row is not None
    assert row.label == "A renamed"
    assert row.model == "openai/gpt-5.4"
    # Blank api_key in form means "keep existing" (matches the quick
    # setup pattern); test it stayed.
    assert row.api_key == "k"


async def test_post_llm_models_default_flips(db, async_client):
    from app.repos import llm_models as llm_models_repo
    a = await llm_models_repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    b = await llm_models_repo.insert(
        db, label="B", provider_id="ollama",
        model="ollama_chat/llama3.1", api_key="", base_url="x",
        make_default=False,
    )
    resp = await async_client.post(
        f"/settings/llm-models/{b}/default", follow_redirects=False,
    )
    assert resp.status_code == 303
    new_default = await llm_models_repo.get_default(db)
    assert new_default is not None and new_default.id == b
    row_a = await llm_models_repo.get(db, a)
    assert row_a is not None and row_a.is_default is False


async def test_post_llm_models_delete_non_default(db, async_client):
    from app.repos import llm_models as llm_models_repo
    a = await llm_models_repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    b = await llm_models_repo.insert(
        db, label="B", provider_id="ollama",
        model="ollama_chat/llama3.1", api_key="", base_url="x",
        make_default=False,
    )
    resp = await async_client.post(
        f"/settings/llm-models/{b}/delete", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await llm_models_repo.get(db, b) is None
    assert await llm_models_repo.get(db, a) is not None


async def test_post_llm_models_delete_default_returns_409(db, async_client):
    from app.repos import llm_models as llm_models_repo
    a = await llm_models_repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    resp = await async_client.post(
        f"/settings/llm-models/{a}/delete", follow_redirects=False,
    )
    assert resp.status_code == 409
```

- [ ] **Step 2: Run, expect failure**

```
pytest tests/test_routes_settings.py -k llm_models -v
```

Expected: 404 (routes don't exist).

- [ ] **Step 3: Implement the endpoints**

In `app/routes/settings.py`, add the import:

```python
from app.repos import llm_models as llm_models_repo
```

Then add these handlers (placement: after the existing `quick_setup`
function for proximity to similar code):

```python
@router.post("/settings/llm-models")
async def llm_models_insert(
    label: str = Form(...),
    provider_id: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Add a new LLM model row. If no models exist yet, the new row
    becomes the default automatically — otherwise the existing default
    is preserved (user can flip it explicitly)."""
    existing = await llm_models_repo.list_all(db)
    make_default = not existing
    new_id = await llm_models_repo.insert(
        db,
        label=label.strip() or "Untitled",
        provider_id=provider_id.strip(),
        model=model.strip(),
        api_key=api_key.strip(),
        base_url=base_url.strip().rstrip("/"),
        make_default=make_default,
    )
    return RedirectResponse(
        f"/settings?added={new_id}", status_code=303,
    )


@router.post("/settings/llm-models/{model_id}")
async def llm_models_update(
    model_id: int,
    label: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Update a model row's user-facing fields. is_default is NOT
    modified here; use /default to flip the default. Empty api_key in
    the form means "keep the existing key" (same pattern as the
    legacy Quick Setup)."""
    row = await llm_models_repo.get(db, model_id)
    if row is None:
        raise HTTPException(404)
    effective_key = api_key.strip() or row.api_key
    await llm_models_repo.update(
        db, model_id,
        label=label.strip() or row.label,
        model=model.strip(),
        api_key=effective_key,
        base_url=base_url.strip().rstrip("/"),
    )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/llm-models/{model_id}/default")
async def llm_models_set_default(
    model_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    row = await llm_models_repo.get(db, model_id)
    if row is None:
        raise HTTPException(404)
    await llm_models_repo.set_default(db, model_id)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/llm-models/{model_id}/delete")
async def llm_models_delete(
    model_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    try:
        await llm_models_repo.delete(db, model_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/llm-models/{model_id}/test", response_class=HTMLResponse)
async def llm_models_test(
    model_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Round-trip a tiny completion through the row's model/key/base_url.
    Used by the per-row [Test] button on the Configured Models card.
    Replaces the global /settings/test-llm endpoint."""
    row = await llm_models_repo.get(db, model_id)
    if row is None:
        return HTMLResponse(
            '<p class="status status-failed">⚠ Model not found.</p>'
        )
    base_url = row.base_url or None
    # Ollama: probe reachability first so failures are clear.
    if base_url and row.model.startswith(("ollama/", "ollama_chat/")):
        err = await _probe_ollama_reachable(base_url)
        if err is not None:
            return HTMLResponse(
                f'<p class="status status-failed">⚠ Cannot reach Ollama at '
                f'{base_url}: {err}</p>'
            )
    kwargs = {
        "model": row.model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "api_key": row.api_key or "",
        "max_tokens": 10,
    }
    if base_url:
        kwargs["api_base"] = base_url
    try:
        response = await litellm.acompletion(**kwargs)
        text = (response.choices[0].message.content or "").strip()
        return HTMLResponse(
            f'<p class="status status-done">✓ {row.model} responded: '
            f'{text[:50] or "(empty)"}</p>'
        )
    except Exception as e:
        return HTMLResponse(
            f'<p class="status status-failed">⚠ {type(e).__name__}: {e}</p>'
        )
```

Also: delete the old `quick_setup` and `test_llm` endpoint bodies — they
are superseded. Keep `_probe_ollama_reachable` (it's used by
`llm_models_test`).

- [ ] **Step 4: Update `settings_page` to pass `llm_models` to the template**

In the `settings_page` handler, add near the top:

```python
    llm_models = await llm_models_repo.list_all(db)
```

And in the `TemplateResponse` context dict, add `"llm_models":
llm_models,`. Drop the now-irrelevant `applied_preset`,
`current_provider_id`, `preset_chat_models`, `preset_chat_models_full`
keys (the new card replaces them).

- [ ] **Step 5: Run backend tests**

```
pytest tests/test_routes_settings.py -k llm_models -v
```

Expected: 5 tests PASS.

- [ ] **Step 6: Commit 11a**

```bash
git add app/routes/settings.py tests/test_routes_settings.py
git commit -m "feat(routes/settings): llm-models CRUD + per-row test endpoint"
```

#### 11b — Template: Configured models card

- [ ] **Step 7: Edit `app/templates/settings.html` — add the new card**

Insert a new section just after the heading block (after `{% if
applied_preset %}` — which itself becomes obsolete; remove it):

```html
  <!-- ── Configured models ──────────────────────────────────── -->
  <section class="settings-card">
    <header class="settings-card-head">
      <span class="settings-card-icon" aria-hidden="true">🧠</span>
      <div class="settings-card-head-text">
        <h2>Configured models</h2>
        <p class="settings-card-sub">
          Pick which model handles the default summary, chat, and any
          background job. Add more so you can switch on a per-resummary
          basis from the video page.
        </p>
      </div>
    </header>

    {% if llm_models %}
      <ul class="llm-model-list">
        {% for m in llm_models %}
          <li class="llm-model-row {% if m.is_default %}is-default{% endif %}">
            <div class="llm-model-row-label">
              <strong>{{ m.label }}</strong>
              {% if m.is_default %}
                <span class="llm-model-badge">Default ✓</span>
              {% endif %}
              <div class="llm-model-row-meta">
                <code>{{ m.model }}</code>
                {% if m.base_url %} · <code>{{ m.base_url }}</code>{% endif %}
              </div>
            </div>
            <div class="llm-model-row-actions">
              <button type="button" class="btn btn-secondary"
                      hx-post="/settings/llm-models/{{ m.id }}/test"
                      hx-target="#llm-test-{{ m.id }}"
                      hx-swap="innerHTML"
                      hx-disabled-elt="this">Test</button>
              <a class="btn btn-secondary"
                 href="/settings?edit={{ m.id }}#quick-setup">Edit</a>
              {% if m.is_default %}
                <button type="button" class="btn btn-secondary" disabled
                        title="Make another model default first">Delete</button>
              {% else %}
                <form method="post"
                      action="/settings/llm-models/{{ m.id }}/delete"
                      onsubmit="return confirm('Delete {{ m.label|e }}?');"
                      style="display:inline">
                  <button type="submit" class="btn btn-secondary">Delete</button>
                </form>
                <form method="post"
                      action="/settings/llm-models/{{ m.id }}/default"
                      style="display:inline">
                  <button type="submit" class="btn btn-secondary">Make default</button>
                </form>
              {% endif %}
            </div>
            <div id="llm-test-{{ m.id }}" class="settings-test-result"></div>
          </li>
        {% endfor %}
      </ul>
    {% else %}
      <p class="settings-card-sub">
        No models configured yet. Pick a provider in <a href="#quick-setup">Quick setup</a> below.
      </p>
    {% endif %}
  </section>
```

- [ ] **Step 8: Add CSS for the new card**

Append to `app/static/app.css`:

```css
/* ── Configured models card ─────────────────────────────────── */
.llm-model-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.llm-model-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.75rem 1rem;
  align-items: center;
  padding: 0.75rem;
  border: 1px solid var(--border, #ccc);
  border-radius: 0.5rem;
  margin-bottom: 0.5rem;
}
.llm-model-row.is-default {
  border-left: 4px solid var(--accent-success, #1f7a3e);
}
.llm-model-row-label strong { font-weight: 600; }
.llm-model-row-meta {
  font-size: 0.85em;
  color: var(--muted, #666);
  margin-top: 0.15rem;
}
.llm-model-badge {
  display: inline-block;
  margin-left: 0.5rem;
  padding: 0.05rem 0.4rem;
  font-size: 0.75em;
  background: var(--accent-success-bg, #d3f0dc);
  color: var(--accent-success, #1f7a3e);
  border-radius: 0.25rem;
}
.llm-model-row-actions {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
}
.llm-model-row > div:last-child { grid-column: 1 / -1; }
```

> **Engineer note:** `--accent-success` may not yet exist as a CSS
> custom property. The fallback value `#1f7a3e` (a forest green) keeps
> it readable without depending on the theme being updated. Same for
> `--accent-success-bg`.

- [ ] **Step 9: Visually verify in the browser**

```
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/settings — the new card renders above the
old Quick Setup with one row per configured model.

- [ ] **Step 10: Commit 11b**

```bash
git add app/templates/settings.html app/static/app.css
git commit -m "feat(settings/ui): Configured models card with default highlight"
```

#### 11c — Wire Quick Setup into Add / Edit

- [ ] **Step 11: Modify Quick Setup form to add a label field and target the new endpoint**

In `app/templates/settings.html`, find the `<form method="post"
action="/settings/quick-setup" class="quicksetup-form">` opening tag.
Change to:

```html
    {% set edit_id = request.query_params.get('edit') %}
    {% set edit_row = (llm_models | selectattr('id', 'equalto', edit_id|int)
                                | first) if edit_id else None %}
    <form id="quick-setup"
          method="post"
          action="{{ '/settings/llm-models/' ~ edit_row.id if edit_row else '/settings/llm-models' }}"
          class="quicksetup-form">

      <label class="settings-field">
        <span class="settings-label">Label</span>
        <input name="label" required
               value="{{ edit_row.label if edit_row else '' }}"
               placeholder="e.g. Claude Sonnet 4.6">
      </label>
```

The submit button at the bottom:

```html
      <div class="settings-test-row">
        <button type="submit" class="btn btn-primary"
                x-bind:disabled="!provider">
          {{ 'Save changes' if edit_row else 'Add model' }}
        </button>
      </div>
```

Add a hidden `provider_id` field (Alpine sets it from the radio):

```html
      <input type="hidden" name="provider_id" x-bind:value="provider">
```

If `edit_row`, pre-fill provider:

```html
    <section class="settings-card settings-quicksetup"
             x-data="{ provider: '{{ edit_row.provider_id if edit_row else current_provider_id }}', showAll: {} }">
```

> **Engineer note:** the existing Quick Setup form's hidden state is
> tracked by Alpine via `provider`. It already submits a `provider`
> radio; we now also need `provider_id` matching the field name in the
> route. Reuse the radio's value via the hidden input above.

- [ ] **Step 12: Make Quick Setup form pre-fill model/key/base_url on edit**

The existing pre-population logic (`{% set selected_value =
settings.get('llm_model', '') if is_active else p.default_llm %}`) is
tied to `settings`. Add an edit-row override above the `{% set
selected_value = ... %}` line:

```jinja
                    {% if edit_row %}
                      {% set selected_value = edit_row.model %}
                    {% else %}
                      {% set selected_value = settings.get('llm_model', '') if is_active else p.default_llm %}
                    {% endif %}
```

Same pattern for the `llm_base_url` Ollama input (line ~98 in the
current template — search for `value="{{ settings.get('llm_base_url'`):

```jinja
                     value="{{ edit_row.base_url if edit_row else settings.get('llm_base_url', p.default_llm_base_url) }}"
```

And for the API key input — leave the placeholder so editing without
re-typing keeps the existing key (matching `llm_models_update`'s
"empty means keep" pattern):

```jinja
              <input type="password"
                     name="api_key"
                     placeholder="{% if edit_row %}leave blank to keep existing key{% else %}leave blank to keep existing key{% endif %}">
```

- [ ] **Step 13: Remove the obsolete "Language model" advanced card**

In `app/templates/settings.html`, find the
`<!-- ── LLM ───────... -->` section inside
`<details class="settings-advanced">`. Delete that whole section
(roughly from `<section class="settings-card">` through `</section>`
for the LLM block — leave the Whisper section intact).

Update the surrounding `<details>` summary text:

```html
      <summary class="settings-advanced-summary">
        <span class="settings-advanced-icon" aria-hidden="true">⚙</span>
        <span>
          <strong>Advanced — Whisper manual config</strong>
          <span class="settings-advanced-hint">
            Override Whisper settings individually. LLM models are managed
            in the Configured models card above.
          </span>
        </span>
      </summary>
```

- [ ] **Step 14: Verify in the browser**

Restart the server (or rely on `--reload`). Visit `/settings`:
- Add a model via Quick Setup → it appears in the card above.
- Click "Edit" on a row → Quick Setup pre-fills.
- Toggle "Make default" → green border moves to that row.

- [ ] **Step 15: Run the full route test file**

```
pytest tests/test_routes_settings.py -v
```

Expected: PASS. Adjust pre-existing tests that referenced the removed
`/settings/quick-setup` and `/settings/test-llm` endpoints — they
should be removed (the new endpoints replace them).

- [ ] **Step 16: Commit 11c**

```bash
git add app/templates/settings.html tests/test_routes_settings.py
git commit -m "feat(settings/ui): Quick Setup repurposed as Add/Edit model"
```

---

### Task 12: Video detail UI — inline resummary panel + chat model picker

**Files:**
- Modify: `app/templates/video_detail.html`
- Modify: `app/static/app.css`

- [ ] **Step 1: Replace the Re-summarize form with an Alpine-toggled panel**

In `app/templates/video_detail.html`, find the `<div class="action-row">`.
Replace the inner `<form method="post" action="/v/{{ video.id }}/reindex">`
block with:

```html
      <div class="action-group" x-data="{ open: false }">
        <button type="button" class="btn btn-accent"
                @click="open = !open">
          {% if video.summary %}Re-summarize{% elif video.transcript %}Generate summary{% else %}Retry{% endif %} ▾
        </button>

        <form method="post" action="/v/{{ video.id }}/reindex"
              class="resummary-panel"
              x-show="open" x-cloak>
          <label class="settings-field">
            <span class="settings-label">Model</span>
            <select name="llm_model_id">
              {% for m in llm_models %}
                <option value="{{ m.id }}"
                        {% if m.is_default %}selected{% endif %}>
                  {{ m.label }}{% if m.is_default %} (Default){% endif %}
                </option>
              {% endfor %}
            </select>
          </label>
          <label class="settings-field">
            <span class="settings-label">Additional instruction (optional)</span>
            <textarea name="additional_prompt" rows="3"
                      placeholder="e.g. focus on the named frameworks, keep it shorter"></textarea>
          </label>
          <div class="resummary-actions">
            <button type="button" class="btn btn-secondary" @click="open=false">Cancel</button>
            <button type="submit" class="btn btn-accent">Re-summarize now</button>
          </div>
        </form>

        {% if video.transcript and video.kind.value != 'web' %}
          <form method="post" action="/v/{{ video.id }}/retranscribe"
                onsubmit="return confirm('Throw away the stored transcript and fetch it fresh?');">
            <button type="submit" class="link-button">Re-transcribe</button>
          </form>
        {% endif %}
      </div>
```

- [ ] **Step 2: Add CSS for the resummary panel**

Append to `app/static/app.css`:

```css
/* ── Resummary inline panel ─────────────────────────────────── */
.resummary-panel {
  display: block;
  margin-top: 0.75rem;
  padding: 0.75rem;
  border: 1px solid var(--border, #ccc);
  border-radius: 0.5rem;
  background: var(--bg-soft, #fafafa);
}
.resummary-panel textarea {
  width: 100%;
  min-height: 4rem;
}
.resummary-actions {
  display: flex;
  gap: 0.5rem;
  justify-content: flex-end;
  margin-top: 0.5rem;
}
[x-cloak] { display: none !important; }
```

(`[x-cloak]` may already exist; if so, skip that rule.)

- [ ] **Step 3: Add the chat-form model picker**

In `app/templates/video_detail.html`, find the chat `<form
class="chat-form">`. Inside the form, after the `<input name="content">`
line and before the `<button type="submit">`, insert:

```html
        <select name="llm_model_id" class="chat-model-select" title="Model for this chat reply">
          {% for m in llm_models %}
            <option value="{{ m.id }}" {% if m.is_default %}selected{% endif %}>
              {{ m.label }}{% if m.is_default %} (Default){% endif %}
            </option>
          {% endfor %}
        </select>
```

Add CSS so the dropdown sits inline without crowding the input on
mobile:

```css
.chat-model-select {
  font-size: 0.85em;
  max-width: 14rem;
}
```

Also update the chat-form's `hx-on::after-request` so it only resets
the text input, not the select. The current handler is
`this.reset(); document.querySelector('input[name=content]').focus()`.
Change to:

```html
        hx-on::after-request="document.querySelector('input[name=content]').value=''; document.querySelector('input[name=content]').focus()"
```

- [ ] **Step 4: Verify in the browser**

Visit any video detail page:
- Re-summarize button toggles the panel; submit redirects back and a
  new job appears.
- Chat dropdown stays visible; select a different model, send a chat,
  the model selection survives the swap.

- [ ] **Step 5: Commit**

```bash
git add app/templates/video_detail.html app/static/app.css
git commit -m "feat(video-detail): inline resummary panel + chat model picker"
```

---

### Task 13: MCP — `list_models` + `resummarize` tools, overrides on `submit_url`

**Files:**
- Modify: `app/routes/mcp.py`
- Modify: `tests/test_routes_mcp.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_routes_mcp.py` (use the existing helper that
invokes `_tool_*` functions directly — look at the first test in the
file for the pattern):

```python
async def test_tool_list_models_returns_rows(db):
    from app.repos import llm_models as llm_models_repo
    from app.routes.mcp import _tool_list_models

    await llm_models_repo.insert(
        db, label="Default", provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="k", base_url="", make_default=True,
    )
    await llm_models_repo.insert(
        db, label="Local", provider_id="ollama",
        model="ollama_chat/llama3.1",
        api_key="", base_url="http://lan:11434",
        make_default=False,
    )
    rows = await _tool_list_models(db)
    assert {r["label"] for r in rows} == {"Default", "Local"}
    default_row = next(r for r in rows if r["is_default"])
    assert default_row["label"] == "Default"


async def test_tool_resummarize_enqueues_job_with_overrides(db, config):
    from app.models import VideoKind
    from app.repos import llm_models as llm_models_repo
    from app.repos import videos as videos_repo
    from app.routes.mcp import _tool_resummarize

    mid = await llm_models_repo.insert(
        db, label="X", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.YOUTUBE, user_id=1,
    )
    out = await _tool_resummarize(
        db, "v1",
        llm_model_id=mid,
        additional_prompt="be terse",
    )
    assert out["queued"] is True
    from app.repos import jobs as jobs_repo
    job = await jobs_repo.latest_for_video(db, "v1")
    assert job is not None
    assert job.llm_model_id == mid
    assert job.additional_prompt == "be terse"
```

- [ ] **Step 2: Run, expect failure**

```
pytest tests/test_routes_mcp.py -k "list_models or resummarize" -v
```

Expected: ImportError.

- [ ] **Step 3: Add the tool implementations to `app/routes/mcp.py`**

After the existing `_tool_*` functions:

```python
async def _tool_list_models(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    from app.repos import llm_models as llm_models_repo
    rows = await llm_models_repo.list_all(db)
    return [
        {
            "id": r.id,
            "label": r.label,
            "model": r.model,
            "provider_id": r.provider_id,
            "is_default": r.is_default,
        }
        for r in rows
    ]


async def _tool_resummarize(
    db: aiosqlite.Connection,
    video_id: str,
    *,
    llm_model_id: int | None = None,
    additional_prompt: str = "",
) -> dict[str, Any]:
    from app.repos import jobs as jobs_repo
    from app.repos import videos as videos_repo
    if await videos_repo.get(db, video_id) is None:
        raise ValueError(f"Unknown video: {video_id}")
    prompt = additional_prompt.strip() or None
    job_id = await jobs_repo.enqueue(
        db, video_id,
        llm_model_id=llm_model_id,
        additional_prompt=prompt,
    )
    return {"video_id": video_id, "job_id": job_id, "queued": True}
```

Also extend `_tool_submit_url` to accept and forward the same two
optional params:

```python
async def _tool_submit_url(
    db: aiosqlite.Connection,
    config: Config,
    url: str,
    *,
    user_id: int = 1,
    wait_for_summary: bool = False,
    wait_timeout: int = 120,
    llm_model_id: int | None = None,
    additional_prompt: str = "",
) -> dict[str, Any]:
    resource = await api_svc.submit_video(
        db, config,
        url=url, user_id=user_id,
        wait=wait_for_summary, wait_timeout=wait_timeout,
        llm_model_id=llm_model_id,
        additional_prompt=additional_prompt.strip() or None,
    )
    # ... rest of the function unchanged ...
```

- [ ] **Step 4: Register the new MCP tools and bake the model list into docstrings**

In the FastMCP setup function (look for `mcp = FastMCP(...)` /
`register_*`), change the registration block so the docstrings are
generated at startup with the model list embedded. Pattern:

```python
async def _models_doc_line(db: aiosqlite.Connection) -> str:
    from app.repos import llm_models as llm_models_repo
    rows = await llm_models_repo.list_all(db)
    if not rows:
        return "No models configured yet — call list_models first."
    parts = [
        f"{r.id}={r.label!r}" + (" (default)" if r.is_default else "")
        for r in rows
    ]
    return "Available llm_model_id values: " + ", ".join(parts) + "."


# In register_mcp_tools (or wherever the FastMCP server is built):
doc_line = await _models_doc_line(app_state.db)

@mcp.tool()
async def submit_url(
    url: str,
    wait_for_summary: bool = False,
    wait_timeout: int = 120,
    llm_model_id: int | None = None,
    additional_prompt: str = "",
) -> dict[str, Any]:
    return await _tool_submit_url(
        app_state.db, app_state.config, url,
        wait_for_summary=wait_for_summary, wait_timeout=wait_timeout,
        llm_model_id=llm_model_id, additional_prompt=additional_prompt,
    )

submit_url.__doc__ = (
    "Submit a URL (YouTube or web article) to be summarized.\n\n"
    f"{doc_line}\n\n"
    "With wait_for_summary=True, the call blocks up to wait_timeout\n"
    "seconds and returns the summary inline if ready."
)


@mcp.tool()
async def resummarize(
    video_id: str,
    llm_model_id: int | None = None,
    additional_prompt: str = "",
) -> dict[str, Any]:
    return await _tool_resummarize(
        app_state.db, video_id,
        llm_model_id=llm_model_id,
        additional_prompt=additional_prompt,
    )

resummarize.__doc__ = (
    "Re-run the summary for an existing video. The new summary\n"
    "replaces the previous one.\n\n"
    f"{doc_line}\n\n"
    "additional_prompt is a one-shot instruction appended to the\n"
    "system prompt for this run only — it is not persisted."
)


@mcp.tool()
async def list_models() -> list[dict[str, Any]]:
    """Return all configured LLM models — id, label, model id,
    provider, and which one is the default. Useful when the
    user has just edited the configured models in Settings."""
    return await _tool_list_models(app_state.db)
```

> **Engineer note:** `register_mcp_tools` (or the equivalent factory)
> is currently a sync function. Use `await _models_doc_line(...)` only
> if the factory runs inside the async lifespan; otherwise compute the
> docstring lazily via `submit_url.__doc__ = await ...` from within
> the lifespan setup. Check `app/main.py` for how `app_state.db` and
> the MCP mount are wired and adapt the placement accordingly.

- [ ] **Step 5: Run the MCP tests**

```
pytest tests/test_routes_mcp.py -v
```

Expected: existing tests still pass + new ones PASS.

- [ ] **Step 6: Manual smoke**

```
uvicorn app.main:app --reload --port 8000
```

Invoke the MCP server via curl or the existing test fixture and
confirm `submit_url` tool description contains "Available llm_model_id
values: ...".

- [ ] **Step 7: Commit**

```bash
git add app/routes/mcp.py tests/test_routes_mcp.py
git commit -m "feat(mcp): list_models + resummarize tools, model labels in docstrings"
```

---

### Task 14: Onboarding — keep the first-run experience working

**Files:**
- Modify: `app/routes/onboarding.py` (if it writes `settings.llm_model`)
- Modify: `tests/test_routes_onboarding.py`

- [ ] **Step 1: Check what onboarding writes**

```
grep -n "llm_model\|llm_api_key\|llm_base_url\|quick-setup" app/routes/onboarding.py
```

If it currently posts to `/settings/quick-setup` (now removed) or
writes the keys directly to `settings`, redirect it to
`/settings/llm-models` (our new POST endpoint) with the same form
fields plus `label` and `provider_id`. Provider is derived from the
selected preset.

- [ ] **Step 2: Update the onboarding flow accordingly**

Pattern (replace any direct `settings_repo.set(db, "llm_model", ...)`
call):

```python
from app.repos import llm_models as llm_models_repo
from app.services.providers import get_preset

preset = get_preset(provider_id)
await llm_models_repo.insert(
    db,
    label=preset.name,
    provider_id=provider_id,
    model=model.strip() or preset.default_llm,
    api_key=api_key.strip(),
    base_url=(base_url or preset.default_llm_base_url).rstrip("/"),
    make_default=True,
)
```

(Onboarding always inserts the first model, so `make_default=True` is
correct.)

- [ ] **Step 3: Update onboarding tests**

Read `tests/test_routes_onboarding.py` to see what they assert, and
change assertions about `settings.llm_model` to use
`llm_models_repo.get_default(db)` instead.

- [ ] **Step 4: Run onboarding tests**

```
pytest tests/test_routes_onboarding.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/onboarding.py tests/test_routes_onboarding.py
git commit -m "feat(onboarding): write first model into llm_models"
```

---

### Task 15: Final regression sweep

**Files:**
- N/A (clean up + verify)

- [ ] **Step 1: Run the full test suite**

```
pytest -x -q
```

Expected: all green. If anything fails, the most likely cause is a
test that pre-seeded `settings.llm_model` and now needs to seed an
`llm_models` row instead. Fix and re-run.

- [ ] **Step 2: Run the linters / type checks the repo uses**

```
ruff check .
ruff format --check .
```

Fix any issues that surface.

- [ ] **Step 3: Manual end-to-end smoke**

Start the server:

```
uvicorn app.main:app --reload
```

Walk through:
1. `/settings` — confirm the Configured models card shows your migrated row.
2. Add a second model via Quick Setup.
3. Toggle the default to the new row → green border moves.
4. On a video detail page, click Re-summarize, pick the non-default
   model, type "be terse, no preamble", submit → job runs with the
   override, the resulting summary is markedly shorter.
5. In the chat box, switch to the non-default model and ask a question
   → response uses that model.

- [ ] **Step 4: Commit any final cleanup**

```bash
git add -A
git status   # verify what's staged
git commit -m "chore: regression sweep and lint pass for multi-model rollout"
```

- [ ] **Step 5: Done**

Spec satisfied; all paths green. Push when ready.
