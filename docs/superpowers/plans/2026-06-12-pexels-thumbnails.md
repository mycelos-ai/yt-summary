# Pexels Stock Thumbnails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** E-mail summaries (and web articles lacking an og:image) get a fitting Pexels stock photo as their thumbnail, driven by an LLM-suggested `image_query`; a repeatable CLI backfills existing items.

**Architecture:** The highlights-extraction JSON envelope gains an `image_query` field, parsed by a new `parse_image_query` helper and persisted to a new `videos.image_query` column. A small, fully fault-tolerant `stock_images` service fetches one Pexels photo; a shared `ensure_stock_thumbnail` helper is called both from the pipeline (live) and from a `python -m app.scripts.backfill_thumbnails` CLI (`--force`, `--dry-run`, `--user-id`, `--limit`). Missing API key or query → no-op. Any network/parse error → swallowed.

**Tech Stack:** FastAPI + aiosqlite + httpx + litellm + Jinja2. Tests: pytest + pytest-asyncio (auto mode), `unittest.mock` (AsyncMock/patch) + monkeypatch.

**Spec:** `docs/superpowers/specs/2026-06-12-pexels-thumbnails-design.md`

**Conventions (read before starting):**
- Run tests with `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest <paths> -v` from the worktree root.
- **After every task run `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .` — CI fails on E501 (line length 100). Wrap long lines.**
- Outbound HTTP uses `httpx.AsyncClient` (see `download_thumbnail` in `app/services/youtube.py`). `download_thumbnail(url, target)` already exists and writes JPEG bytes — reuse it.
- LLM one-off calls use `litellm.acompletion(model=, messages=, api_key=[, api_base=])` and resolve the model via `llm_models_repo.get_default(db)` (returns a row with `.model`, `.api_key`, `.base_url`).
- Settings: `settings_repo.set(db, key, value)` / `settings_repo.get(db, key)` (global, user_id=1) and `settings_repo.get_for_user(db, user_id, key)`. Empty value → `settings_repo.delete(db, key)`.
- Standalone scripts live in `scripts/` at repo root; a `python -m app.scripts.X` module needs `app/scripts/__init__.py`. Get a connection via `from app.db import connect` + `Config.from_env()`.

**Prerequisite ordering:** This plan assumes the archive feature may or may not have merged. It touches `videos` (new column) and `_gather_pool` is NOT modified here. No conflict with the archive plan beyond both adding a `videos` column via `_ensure_column` (independent lines).

---

### Task 1: Schema + migration + model for `image_query`

**Files:**
- Modify: `app/db.py` (SCHEMA videos block; `_run_migrations`)
- Modify: `app/models.py` (Video dataclass)
- Modify: `app/repos/videos.py` (`_row_to_video`; new setter)
- Test: `tests/test_db_migration_image_query.py` (new), `tests/test_repos_videos.py`

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_image_query.py`:

```python
import asyncio

import aiosqlite

from app.config import Config
from app.db import connect, init_schema


def test_videos_gains_image_query_column(tmp_path):
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
    assert "image_query" in cols
```

- [ ] **Step 2: Run; verify it fails**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_db_migration_image_query.py -v`
Expected: FAIL.

- [ ] **Step 3: Add column + migration**

In `app/db.py` SCHEMA `videos` table, add after `highlights_json TEXT` (the last column; add a comma to the line above and a new line):

```sql
    highlights_json TEXT,
    -- LLM-suggested stock-photo search query for the thumbnail
    -- (e.g. "solar panels rooftop"). NULL = not yet generated.
    image_query TEXT
```

In `_run_migrations`, in the `videos` block after the `highlights_json` ensure-column (and after `archived_at` if the archive feature merged):

```python
        await _ensure_column(conn, "videos", "image_query", "TEXT")
```

- [ ] **Step 4: Model + setter + row mapping**

In `app/models.py`, Video dataclass — add after `highlights_json: str | None = None`:

```python
    # LLM-suggested stock-photo search query (Pexels). None = not set.
    image_query: str | None = None
```

In `app/repos/videos.py`, `_row_to_video` — add a fallback read alongside the others:

```python
    try:
        image_query = row["image_query"]
    except (IndexError, KeyError):
        image_query = None
```

and append `image_query=image_query,` to the `Video(...)` kwargs.

Add a setter after `set_highlights` (~line 512):

```python
async def set_image_query(
    db: aiosqlite.Connection, video_id: str, image_query: str | None,
) -> None:
    await db.execute(
        "UPDATE videos SET image_query=? WHERE id=?",
        (image_query, video_id),
    )
    await db.commit()
```

- [ ] **Step 5: Write + run a repo test**

Append to `tests/test_repos_videos.py`:

```python
async def test_set_image_query_roundtrip(db):
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_image_query(db, "v1", "mountain sunrise")
    v = await videos_repo.get(db, "v1")
    assert v.image_query == "mountain sunrise"
```

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_db_migration_image_query.py tests/test_repos_videos.py tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/db.py app/models.py app/repos/videos.py tests/test_db_migration_image_query.py tests/test_repos_videos.py
git commit -m "feat(thumbnails): image_query column + setter"
```

---

### Task 2: LLM envelope — add `image_query` to the prompt + a parser

**Files:**
- Modify: `app/services/highlight_parser.py` (`HIGHLIGHTS_SCHEMA_HINT`; new `parse_image_query`)
- Test: `tests/test_services_highlight_parser.py`

- [ ] **Step 1: Write failing parser tests**

Append to `tests/test_services_highlight_parser.py` (it imports from the module; add `parse_image_query` to the import):

```python
from app.services.highlight_parser import parse_image_query


def test_parse_image_query_present():
    raw = '{"summary": "s", "highlights": [], "image_query": "solar panels"}'
    assert parse_image_query(raw) == "solar panels"


def test_parse_image_query_missing():
    raw = '{"summary": "s", "highlights": []}'
    assert parse_image_query(raw) is None


def test_parse_image_query_wrong_type():
    raw = '{"summary": "s", "image_query": 123}'
    assert parse_image_query(raw) is None


def test_parse_image_query_blank():
    raw = '{"summary": "s", "image_query": "   "}'
    assert parse_image_query(raw) is None


def test_parse_image_query_unparseable():
    assert parse_image_query("not json at all") is None
```

- [ ] **Step 2: Run; verify it fails**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_highlight_parser.py -v -k image_query`
Expected: FAIL (`cannot import name 'parse_image_query'`).

- [ ] **Step 3: Extend the schema hint + add parser**

In `app/services/highlight_parser.py`, extend `HIGHLIGHTS_SCHEMA_HINT`. Add `"image_query"` to the JSON shape block and a rule. Change the shape to:

```python
HIGHLIGHTS_SCHEMA_HINT = """\
Return your answer as a single JSON object with this exact shape:

{
  "summary": "<the full markdown summary>",
  "image_query": "<2-4 English keywords for a fitting stock photo>",
  "highlights": [
    {"text": "<one concrete noteworthy point, <40 words>",
     "rank": <integer 1..5, 1 = most noteworthy>,
     "reason": "<one short sentence on why this matters>"},
    ...
  ]
}

Rules for "image_query":
- 2 to 4 concrete, visual English keywords describing the main topic,
  suitable for a stock-photo search (e.g. "data center servers",
  "wind turbines field"). Avoid abstract words and proper nouns that
  won't match stock libraries. Omit the field only if nothing visual
  fits.

Rules for "highlights":
- 3 to 5 entries is typical. If nothing in the content is genuinely
  worth surfacing, return [] (empty list). Silence is better than
  filler.
- Each "text" should be a self-contained statement readable out of
  context — not "this video discusses X" but "X claims Y".
- Use the interest-profile context (if provided) to decide what counts
  as noteworthy for this reader.
```

(Preserve any trailing lines of the original constant after the highlights rules.)

Add the parser function (it reuses the module's existing `_extract_json_blob`):

```python
def parse_image_query(raw: str) -> str | None:
    """Pull the optional `image_query` string out of the LLM envelope.

    Tolerant: returns None when the envelope is unparseable, the field
    is absent, blank, or not a string. Never raises — image queries are
    cosmetic.
    """
    blob = _extract_json_blob(raw)
    if blob is None:
        return None
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("image_query")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
```

- [ ] **Step 4: Run; verify pass**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_highlight_parser.py -v`
Expected: PASS (all, including pre-existing `parse_summary_payload` tests — they must be unaffected; the extra envelope key is ignored by that parser).

- [ ] **Step 5: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/services/highlight_parser.py tests/test_services_highlight_parser.py
git commit -m "feat(thumbnails): image_query in LLM envelope + parser"
```

---

### Task 3: `stock_images` service — Pexels fetch (`fetch_pexels_thumbnail`)

**Files:**
- Create: `app/services/stock_images.py`
- Test: `tests/test_services_stock_images.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_services_stock_images.py`:

```python
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services import stock_images


@pytest.mark.asyncio
async def test_fetch_pexels_no_key_returns_false(tmp_path):
    ok = await stock_images.fetch_pexels_thumbnail(
        query="x", api_key="", target=tmp_path / "v.jpg",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_fetch_pexels_hit_downloads(tmp_path, monkeypatch):
    target = tmp_path / "v.jpg"

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"photos": [{"src": {"large": "https://img/large.jpg"}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return FakeResp()

    monkeypatch.setattr(stock_images.httpx, "AsyncClient", FakeClient)

    async def fake_download(url, tgt):
        Path(tgt).write_bytes(b"jpeg")

    monkeypatch.setattr(stock_images, "download_thumbnail", fake_download)

    ok = await stock_images.fetch_pexels_thumbnail(
        query="solar", api_key="KEY", target=target,
    )
    assert ok is True
    assert target.exists()


@pytest.mark.asyncio
async def test_fetch_pexels_no_results_returns_false(tmp_path, monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"photos": []}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return FakeResp()

    monkeypatch.setattr(stock_images.httpx, "AsyncClient", FakeClient)
    ok = await stock_images.fetch_pexels_thumbnail(
        query="solar", api_key="KEY", target=tmp_path / "v.jpg",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_fetch_pexels_http_error_returns_false(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            raise RuntimeError("429 boom")

    monkeypatch.setattr(stock_images.httpx, "AsyncClient", FakeClient)
    ok = await stock_images.fetch_pexels_thumbnail(
        query="solar", api_key="KEY", target=tmp_path / "v.jpg",
    )
    assert ok is False
```

- [ ] **Step 2: Run; verify failure**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_stock_images.py -v`
Expected: FAIL (`No module named 'app.services.stock_images'`).

- [ ] **Step 3: Implement the service**

Create `app/services/stock_images.py`:

```python
"""Fetch a fitting stock photo from Pexels as an item thumbnail.

Fully fault-tolerant: a missing key, no search hit, a rate-limit (429),
a timeout, or malformed JSON all return False. Thumbnails are cosmetic
and must never block ingestion.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.services.youtube import download_thumbnail

log = logging.getLogger(__name__)

_PEXELS_SEARCH = "https://api.pexels.com/v1/search"


async def fetch_pexels_thumbnail(
    *, query: str, api_key: str, target: Path,
) -> bool:
    """Search Pexels for `query`, download the top landscape photo to
    `target`. Returns True only if a file was written."""
    if not api_key or not query.strip():
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.get(
                _PEXELS_SEARCH,
                headers={"Authorization": api_key},
                params={
                    "query": query,
                    "per_page": 1,
                    "orientation": "landscape",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        photos = data.get("photos") or []
        if not photos:
            return False
        src = (photos[0].get("src") or {}).get("large")
        if not src:
            return False
        await download_thumbnail(src, target)
        return target.exists()
    except Exception as e:  # pragma: no cover - defensive
        log.info("pexels: thumbnail fetch failed for %r: %s", query, e)
        return False
```

NOTE on the test's `client.get(url, headers=...)` signature: the fake's `get` accepts `headers=None` and ignores `params`; real code passes `params=` too. Update the fakes if needed so they accept `**kwargs` — adjust the test fakes' `get` signatures to `async def get(self, url, headers=None, params=None):` before running. Make that edit in Step 1's fakes now if you didn't.

- [ ] **Step 4: Fix the test fakes' get signature, run, verify pass**

Ensure each `FakeClient.get` is `async def get(self, url, headers=None, params=None):`.

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_stock_images.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/services/stock_images.py tests/test_services_stock_images.py
git commit -m "feat(thumbnails): Pexels stock-image service"
```

---

### Task 4: Shared `ensure_stock_thumbnail` helper

**Files:**
- Modify: `app/services/stock_images.py` (add the orchestration helper)
- Test: `tests/test_services_stock_images.py`

This helper centralizes the "should we fetch, and with what query" logic so the pipeline and the backfill CLI share it (DRY).

- [ ] **Step 1: Write failing tests**

Append to `tests/test_services_stock_images.py`:

```python
from app.config import Config
from app.repos import videos as videos_repo
from app.models import VideoKind


async def _seed(db, vid, *, kind=VideoKind.EMAIL, thumb=None, iq="cats"):
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=thumb, duration_seconds=None, kind=kind,
    )
    if iq is not None:
        await videos_repo.set_image_query(db, vid, iq)


@pytest.mark.asyncio
async def test_ensure_sets_thumbnail_for_email(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=False,
    )
    assert changed is True
    v = await videos_repo.get(db, "v1")
    assert v.thumbnail_path is not None


@pytest.mark.asyncio
async def test_ensure_skips_when_thumbnail_present(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", thumb="thumbnails/v1.jpg")
    called = False

    async def fake_fetch(*, query, api_key, target):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=False,
    )
    assert changed is False
    assert called is False


@pytest.mark.asyncio
async def test_ensure_force_overwrites(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", thumb="thumbnails/v1.jpg")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=True,
    )
    assert changed is True


@pytest.mark.asyncio
async def test_ensure_skips_youtube(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", kind=VideoKind.YOUTUBE)
    called = False

    async def fake_fetch(*, query, api_key, target):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=False,
    )
    assert changed is False
    assert called is False


@pytest.mark.asyncio
async def test_ensure_no_query_skips(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", iq=None)
    called = False

    async def fake_fetch(*, query, api_key, target):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(stock_images, "fetch_pexels_thumbnail", fake_fetch)
    v = await videos_repo.get(db, "v1")
    changed = await stock_images.ensure_stock_thumbnail(
        db, v, config=cfg, api_key="KEY", force=False,
    )
    assert changed is False
    assert called is False
```

- [ ] **Step 2: Run; verify failure**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_stock_images.py -v -k ensure`
Expected: FAIL (`no attribute 'ensure_stock_thumbnail'`).

- [ ] **Step 3: Implement the helper**

Append to `app/services/stock_images.py` (add `from app.models import Video, VideoKind`, `from app.config import Config`, `from app.repos import videos as videos_repo` at the top):

```python
_ELIGIBLE_KINDS = (VideoKind.EMAIL, VideoKind.WEB)


async def ensure_stock_thumbnail(
    db, video: Video, *, config: Config, api_key: str, force: bool,
) -> bool:
    """Fetch+set a Pexels thumbnail for an email/web item when missing
    (or always, when `force`). Returns True if a thumbnail was written.

    No-ops (returns False) for: ineligible kind, empty api_key, no
    image_query, or an existing thumbnail when not forcing.
    """
    if video.kind not in _ELIGIBLE_KINDS:
        return False
    if not api_key:
        return False
    if video.thumbnail_path and not force:
        return False
    query = (video.image_query or "").strip()
    if not query:
        return False
    target = config.thumbnails_dir / f"{video.id}.jpg"
    ok = await fetch_pexels_thumbnail(
        query=query, api_key=api_key, target=target,
    )
    if not ok:
        return False
    await videos_repo.set_thumbnail_path(db, video.id, str(target))
    return True
```

This calls a new `videos_repo.set_thumbnail_path`. Add it to `app/repos/videos.py` after `set_image_query`:

```python
async def set_thumbnail_path(
    db: aiosqlite.Connection, video_id: str, thumbnail_path: str,
) -> None:
    await db.execute(
        "UPDATE videos SET thumbnail_path=?, updated_at=datetime('now') "
        "WHERE id=?",
        (thumbnail_path, video_id),
    )
    await db.commit()
```

- [ ] **Step 4: Run; verify pass**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_stock_images.py tests/test_repos_videos.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/services/stock_images.py app/repos/videos.py tests/test_services_stock_images.py
git commit -m "feat(thumbnails): ensure_stock_thumbnail shared helper"
```

---

### Task 5: Settings — Pexels API key field

**Files:**
- Modify: `app/routes/settings.py` (`save_settings` form param + set/delete)
- Modify: `app/templates/settings.html` (text input)
- Test: `tests/test_routes_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_routes_settings.py` (match the file's existing app-build pattern — read one test first to mirror it). A representative test:

```python
def test_pexels_api_key_saved_and_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    import asyncio
    with TestClient(app) as client:
        client.post("/settings", data={"pexels_api_key": "PKEY"},
                    follow_redirects=False)

        async def get_key():
            from app.repos import settings as settings_repo
            return await settings_repo.get(app.state.db, "pexels_api_key")
        assert asyncio.get_event_loop().run_until_complete(get_key()) == "PKEY"

        client.post("/settings", data={"pexels_api_key": ""},
                    follow_redirects=False)
        assert asyncio.get_event_loop().run_until_complete(get_key()) is None
```

NOTE: the POST sends only `pexels_api_key`; all other settings fields use `Form(default=...)`, so the partial form is accepted. Confirm by reading the handler's signature (every field has a default).

- [ ] **Step 2: Run; verify failure**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_routes_settings.py -v -k pexels`
Expected: FAIL (key never stored).

- [ ] **Step 3: Wire the form param**

In `app/routes/settings.py` `save_settings`, add a parameter (after `default_tts_length_scale`):

```python
    pexels_api_key: str = Form(""),
```

And in the `for key, value in (...)` tuple, add a row:

```python
        ("pexels_api_key", pexels_api_key.strip()),
```

(The loop already does set-or-delete based on truthiness.)

- [ ] **Step 4: Add the settings.html field**

In `app/templates/settings.html`, near the other API-key / integration fields, add:

```html
      <label class="settings-field">
        <span class="settings-label">Pexels API Key <span class="settings-hint-inline">— optional, stock photos for email/web thumbnails</span></span>
        <input name="pexels_api_key" value="{{ settings.get('pexels_api_key', '') }}"
               placeholder="Your Pexels API key">
        <small>Get a free key at <code>pexels.com/api</code>. When set, email summaries (and web articles without their own image) get a fitting stock photo instead of a placeholder icon.</small>
      </label>
```

(Confirm the template variable is `settings` — it is used by the existing whisper_base_url field.)

- [ ] **Step 5: Run; verify pass**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_routes_settings.py -v`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/routes/settings.py app/templates/settings.html tests/test_routes_settings.py
git commit -m "feat(thumbnails): Pexels API key setting"
```

---

### Task 6: `generate_image_query` helper in the stock_images service

**Files:**
- Modify: `app/services/stock_images.py` (add `generate_image_query`)
- Test: `tests/test_services_stock_images.py`

**Why this design:** `summarize_with_highlights` returns `(summary, highlights)` and is patched in ~25 existing tests; changing its arity or swapping the pipeline to call `summarize` directly would break all of them. Instead, the `image_query` is derived by a SEPARATE cheap LLM call, used by BOTH the pipeline (Task 7) and the backfill CLI (Task 8) — one shared helper, zero changes to the existing summary path. The extra call only runs for email/web items when a Pexels key is set, so it costs nothing in the common (no-key) case.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_services_stock_images.py`:

```python
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_generate_image_query_from_summary(monkeypatch):
    class Row:
        model = "openai/gpt-4o"
        api_key = "k"
        base_url = ""

    class Msg:
        content = "  wind turbines field  "

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]

    monkeypatch.setattr(
        stock_images.litellm, "acompletion", AsyncMock(return_value=Resp()),
    )
    q = await stock_images.generate_image_query(
        summary="An article about renewable energy.", model_row=Row(),
    )
    assert q == "wind turbines field"


@pytest.mark.asyncio
async def test_generate_image_query_no_summary(monkeypatch):
    class Row:
        model = "m"
        api_key = "k"
        base_url = ""

    called = False

    async def boom(**k):
        nonlocal called
        called = True

    monkeypatch.setattr(stock_images.litellm, "acompletion", boom)
    q = await stock_images.generate_image_query(summary="", model_row=Row())
    assert q is None
    assert called is False


@pytest.mark.asyncio
async def test_generate_image_query_no_model(monkeypatch):
    q = await stock_images.generate_image_query(
        summary="something", model_row=None,
    )
    assert q is None


@pytest.mark.asyncio
async def test_generate_image_query_llm_error_returns_none(monkeypatch):
    class Row:
        model = "m"
        api_key = "k"
        base_url = ""

    monkeypatch.setattr(
        stock_images.litellm, "acompletion",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    q = await stock_images.generate_image_query(
        summary="x", model_row=Row(),
    )
    assert q is None
```

- [ ] **Step 2: Run; verify failure**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_stock_images.py -v -k generate_image_query`
Expected: FAIL (`no attribute 'generate_image_query'` / `litellm`).

- [ ] **Step 3: Implement the helper**

In `app/services/stock_images.py`, add `import litellm` at the top (next to `import httpx`), and append:

```python
_QUERY_PROMPT = (
    "Given this article/newsletter summary, output ONLY 2-4 English "
    "keywords for a fitting stock photo (concrete and visual, no proper "
    "nouns, no quotes, no punctuation).\n\nSUMMARY:\n{summary}"
)


async def generate_image_query(*, summary: str, model_row) -> str | None:
    """Cheap one-off LLM call to derive a stock-photo query from a
    summary. Returns None on empty summary, no model, or any error —
    image queries are cosmetic and must never block."""
    if not summary or not summary.strip() or model_row is None:
        return None
    try:
        kwargs: dict = {
            "model": model_row.model,
            "messages": [
                {"role": "user",
                 "content": _QUERY_PROMPT.format(summary=summary)},
            ],
            "api_key": model_row.api_key,
        }
        if model_row.base_url:
            kwargs["api_base"] = model_row.base_url
        resp = await litellm.acompletion(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:  # pragma: no cover - defensive
        log.info("image-query generation failed: %s", e)
        return None
```

- [ ] **Step 4: Run; verify pass**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_services_stock_images.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/services/stock_images.py tests/test_services_stock_images.py
git commit -m "feat(thumbnails): generate_image_query LLM helper"
```

---

### Task 7: Pipeline integration — generate query + fetch thumbnail

**Files:**
- Modify: `app/pipeline.py` (after the highlights-save block, ~line 250)
- Test: `tests/test_pipeline.py`

The pipeline keeps calling `summarize_with_highlights` exactly as today. After highlights are saved, for email/web items with a Pexels key, it generates the query (shared helper) and fetches the thumbnail. No existing patch target changes — existing pipeline tests stay green untouched.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pipeline.py` (it already imports `patch`, `AsyncMock`, `Config`, `videos_repo`, `llm_models_repo`, `settings_repo`, `TranscriptSource`, `VideoKind`, `process_video` — confirm `VideoKind` import; add `from app.models import VideoKind` to the test file's imports if absent):

```python
async def test_pipeline_sets_stock_thumbnail_for_email(db, tmp_path, monkeypatch):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await videos_repo.upsert_metadata(
        db, video_id="e1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
        kind=VideoKind.EMAIL,
    )
    await videos_repo.set_transcript(db, "e1", "body", TranscriptSource.EMAIL)
    await llm_models_repo.insert(
        db, label="T", provider_id="openai", model="openai/gpt-4o",
        api_key="key", base_url="", make_default=True,
    )
    await settings_repo.set(db, "pexels_api_key", "PKEY")

    async def set_step(s):
        pass

    async def fake_fetch(*, query, api_key, target):
        from pathlib import Path
        Path(target).write_bytes(b"jpeg")
        return True

    async def fake_genq(*, summary, model_row):
        return "city skyline"

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("body", [], TranscriptSource.EMAIL, None)),
        ),
        patch(
            "app.pipeline.summarize_with_highlights",
            AsyncMock(return_value=("SUM", [])),
        ),
        patch(
            "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
        ),
        patch(
            "app.services.stock_images.generate_image_query", fake_genq,
        ),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "e1", set_step)

    v = await videos_repo.get(db, "e1")
    assert v.image_query == "city skyline"
    assert v.thumbnail_path is not None


async def test_pipeline_skips_thumbnail_without_key(db, tmp_path, monkeypatch):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await videos_repo.upsert_metadata(
        db, video_id="e2", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None, kind=VideoKind.EMAIL,
    )
    await videos_repo.set_transcript(db, "e2", "body", TranscriptSource.EMAIL)
    await llm_models_repo.insert(
        db, label="T", provider_id="openai", model="openai/gpt-4o",
        api_key="key", base_url="", make_default=True,
    )
    # no pexels_api_key set

    async def set_step(s):
        pass

    fetch_called = False

    async def fake_fetch(*, query, api_key, target):
        nonlocal fetch_called
        fetch_called = True
        return True

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("body", [], TranscriptSource.EMAIL, None)),
        ),
        patch(
            "app.pipeline.summarize_with_highlights",
            AsyncMock(return_value=("SUM", [])),
        ),
        patch(
            "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
        ),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "e2", set_step)

    assert fetch_called is False
    v = await videos_repo.get(db, "e2")
    assert v.thumbnail_path is None
```

- [ ] **Step 2: Run; verify failure**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_pipeline.py -v -k stock_thumbnail`
Expected: the no-key test may already pass (nothing fetches); the with-key test FAILS (image_query/thumbnail not set).

- [ ] **Step 3: Implement the pipeline step**

In `app/pipeline.py`, confirm imports: `settings_repo` is already imported and used; `VideoKind` is imported (it's used at line 228 `video.kind == VideoKind.EMAIL`). Add right AFTER the existing highlights-save block (after the comment block ending ~line 251, before the language-detection step):

```python
    # Stock thumbnail for email/web items lacking one. Cosmetic — every
    # failure is swallowed inside the stock_images helpers, so this can
    # never break the pipeline. Skipped entirely when no Pexels key is
    # configured for the owning profile.
    if video.kind in (VideoKind.EMAIL, VideoKind.WEB) and not video.thumbnail_path:
        from app.services import stock_images
        pexels_key = await settings_repo.get_for_user(
            db, video.user_id, "pexels_api_key",
        ) or ""
        if pexels_key:
            image_query = await stock_images.generate_image_query(
                summary=summary or "", model_row=model_row,
            )
            if image_query:
                await videos_repo.set_image_query(db, video_id, image_query)
                refreshed = await videos_repo.get(db, video_id)
                if refreshed is not None:
                    await stock_images.ensure_stock_thumbnail(
                        db, refreshed, config=config, api_key=pexels_key,
                        force=False,
                    )
```

VERIFY two names resolve at that point in `process_video`: `summary` (the summary string returned by `summarize_with_highlights`) and `model_row` (the resolved LLM row from earlier in the function — it exists, used to build `model`/`api_key`/`base_url`). If `model_row` is named differently locally, use that name. If `config` isn't the local param name for the Config, use the actual one (the function signature is `process_video(db, config, video_id, set_step, ...)` so `config` is correct).

- [ ] **Step 4: Run; verify pass**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: PASS — including ALL pre-existing pipeline tests, because the summary path is untouched (they patch `summarize_with_highlights`, which still runs as before; the new block no-ops for them since they don't set a Pexels key and most use YouTube kind).

- [ ] **Step 5: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/pipeline.py tests/test_pipeline.py
git commit -m "feat(thumbnails): pipeline fetches stock thumbnail for email/web"
```

---

### Task 8: Backfill CLI

**Files:**
- Create: `app/scripts/__init__.py` (empty), `app/scripts/backfill_thumbnails.py`
- Modify: `app/repos/videos.py` (`list_for_thumbnail_backfill` query)
- Test: `tests/test_scripts_backfill_thumbnails.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scripts_backfill_thumbnails.py`:

```python
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Config
from app.models import VideoKind
from app.repos import videos as videos_repo
from app.scripts import backfill_thumbnails as bf


async def _seed(db, vid, *, kind=VideoKind.EMAIL, thumb=None, iq="cats"):
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=thumb, duration_seconds=None, kind=kind,
    )
    if iq is not None:
        await videos_repo.set_image_query(db, vid, iq)


@pytest.mark.asyncio
async def test_backfill_skips_items_with_thumbnail(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", thumb="thumbnails/v1.jpg")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(
        "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
    )
    summary = await bf.run_backfill(
        db, cfg, api_key="K", force=False, dry_run=False,
    )
    assert summary["fetched"] == 0
    assert summary["skipped"] >= 1


@pytest.mark.asyncio
async def test_backfill_force_processes_all(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", thumb="thumbnails/v1.jpg")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(
        "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
    )
    summary = await bf.run_backfill(
        db, cfg, api_key="K", force=True, dry_run=False,
    )
    assert summary["fetched"] == 1


@pytest.mark.asyncio
async def test_backfill_generates_missing_query(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1", iq=None)
    await videos_repo.set_summary(db, "v1", "A long summary about bridges", "m")

    async def fake_fetch(*, query, api_key, target):
        Path(target).write_bytes(b"jpeg")
        return True

    async def fake_gen(*, summary, model_row):
        return "suspension bridge"

    monkeypatch.setattr(
        "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
    )
    monkeypatch.setattr(
        "app.services.stock_images.generate_image_query", fake_gen,
    )
    summary = await bf.run_backfill(
        db, cfg, api_key="K", force=False, dry_run=False,
    )
    v = await videos_repo.get(db, "v1")
    assert v.image_query == "suspension bridge"
    assert summary["query_generated"] == 1


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await _seed(db, "v1")
    called = False

    async def fake_fetch(*, query, api_key, target):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(
        "app.services.stock_images.fetch_pexels_thumbnail", fake_fetch,
    )
    summary = await bf.run_backfill(
        db, cfg, api_key="K", force=False, dry_run=True,
    )
    assert called is False
    v = await videos_repo.get(db, "v1")
    assert v.thumbnail_path is None
    assert summary["checked"] >= 1
```

- [ ] **Step 2: Run; verify failure**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_scripts_backfill_thumbnails.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Add the backfill candidate query**

In `app/repos/videos.py`, add after `list_archived`/`set_thumbnail_path`:

```python
async def list_for_thumbnail_backfill(
    db: aiosqlite.Connection,
    *,
    user_id: int | None = None,
    only_missing: bool = True,
    limit: int | None = None,
) -> list[Video]:
    """Email/web items eligible for a stock-photo backfill.

    only_missing=True restricts to rows without a thumbnail; False
    returns all (for --force re-runs)."""
    clauses = ["kind IN ('email','web')"]
    params: list = []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if only_missing:
        clauses.append("(thumbnail_path IS NULL OR thumbnail_path = '')")
    sql = "SELECT * FROM videos WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = await db.execute(sql, tuple(params))
    return [_row_to_video(r) for r in await cur.fetchall()]
```

- [ ] **Step 4: Implement the CLI**

Create `app/scripts/__init__.py` (empty). Create `app/scripts/backfill_thumbnails.py`:

```python
"""Backfill Pexels stock thumbnails for email/web items.

Usage:
  python -m app.scripts.backfill_thumbnails [--force] [--dry-run]
                                            [--user-id N] [--limit N]

--force      re-fetch even items that already have a thumbnail
--dry-run    resolve image queries and log them; no Pexels call, no write
--user-id N  restrict to one profile
--limit N    process at most N items
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.config import Config
from app.db import connect
from app.repos import llm_models as llm_models_repo
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services import stock_images

log = logging.getLogger("backfill_thumbnails")


async def run_backfill(
    db, config: Config, *, api_key: str, force: bool, dry_run: bool,
    user_id: int | None = None, limit: int | None = None,
    pause_s: float = 0.3,
) -> dict[str, int]:
    summary = {
        "checked": 0, "query_generated": 0, "fetched": 0,
        "no_result": 0, "skipped": 0, "error": 0,
    }
    model_row = await llm_models_repo.get_default(db)
    videos = await videos_repo.list_for_thumbnail_backfill(
        db, user_id=user_id, only_missing=not force, limit=limit,
    )
    for video in videos:
        summary["checked"] += 1
        query = (video.image_query or "").strip()
        if not query:
            generated = await stock_images.generate_image_query(
                summary=video.summary or "", model_row=model_row,
            )
            if generated:
                query = generated
                if not dry_run:
                    await videos_repo.set_image_query(db, video.id, generated)
                summary["query_generated"] += 1
            else:
                summary["skipped"] += 1
                continue
        if dry_run:
            log.info("[dry-run] %s -> %r", video.id, query)
            continue
        # Re-fetch so ensure_* sees the freshly-written query.
        refreshed = await videos_repo.get(db, video.id)
        if refreshed is None:
            summary["error"] += 1
            continue
        try:
            changed = await stock_images.ensure_stock_thumbnail(
                db, refreshed, config=config, api_key=api_key, force=force,
            )
        except Exception as e:  # pragma: no cover - defensive
            log.info("fetch failed for %s: %s", video.id, e)
            summary["error"] += 1
            continue
        if changed:
            summary["fetched"] += 1
        else:
            summary["no_result"] += 1
        if pause_s:
            await asyncio.sleep(pause_s)
    return summary


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    try:
        api_key = ""
        if args.user_id is not None:
            api_key = await settings_repo.get_for_user(
                db, args.user_id, "pexels_api_key",
            ) or ""
        else:
            api_key = await settings_repo.get(db, "pexels_api_key") or ""
        if not api_key and not args.dry_run:
            log.info("No pexels_api_key configured — nothing to do. "
                     "(Use --dry-run to preview queries.)")
            return
        result = await run_backfill(
            db, config, api_key=api_key, force=args.force,
            dry_run=args.dry_run, user_id=args.user_id, limit=args.limit,
        )
        log.info("Backfill summary: %s", result)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 5: Run; verify pass**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/test_scripts_backfill_thumbnails.py tests/test_repos_videos.py -v`
Expected: PASS.

- [ ] **Step 6: Smoke-test the CLI wiring (dry-run, no network)**

Run: `YTS_DATA_DIR=$TMPDIR/bf-smoke /Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m app.scripts.backfill_thumbnails --dry-run`
Expected: exits 0, logs a summary dict (an empty DB → all zeros). Confirms the module entrypoint imports and runs.

- [ ] **Step 7: Lint + commit**

```bash
/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .
git add app/scripts/__init__.py app/scripts/backfill_thumbnails.py app/repos/videos.py tests/test_scripts_backfill_thumbnails.py
git commit -m "feat(thumbnails): repeatable backfill CLI"
```

---

### Task 9: Full-suite verification

- [ ] **Step 1: Whole suite**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/python -m pytest tests/ -q`
Expected: PASS. The summary path is untouched (Task 7 keeps `summarize_with_highlights`), so pre-existing pipeline tests should stay green. If any straggler appears (e.g. a settings-route test that asserts an exact field set), fix it forward.

- [ ] **Step 2: Lint**

Run: `/Users/stefan/Documents/railsapps/yt-summary/.venv/bin/ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: Commit stragglers**

```bash
git add -A
git commit -m "test(thumbnails): align remaining tests"
```

(Skip if nothing changed.)
