# Related Summaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After each summary is generated, compute a curated "Related Summaries" block once (KNN candidates → LLM curation) and store it as a JSON column; render it on the detail page with a fallback to the existing live-KNN strip.

**Architecture:** A new `related_links_json` column on `videos` (sibling of `highlights_json`). A new service `app/services/related_links.py` pre-filters candidates with the existing `related_video_ids()` KNN, then makes one defensive `litellm.acompletion` call (model-row driven, mirroring `stock_images.generate_image_query`) to pick relevant links + reasons, validating IDs against the candidate set. A pipeline hook runs it after embedding, wrapped in try/except so it never breaks generation. The detail template renders the curated block when present, else falls back to the existing `related-fragment` HTMX strip.

**Tech Stack:** Python 3.11+, FastAPI, aiosqlite (SQLite + sqlite-vec), litellm, Jinja2, HTMX, pytest.

## Global Constraints

- Summary text is plain Markdown stored in `videos.summary`; rendered via `markdown-it` at view time. Do NOT change generation of summary body text.
- Related links are a nice-to-have: a failure path must NEVER block or break summary generation. Catch, log, leave column `NULL`.
- Anti-hallucination: only persist `video_id`s that were in the KNN candidate set; titles come from the candidate set, never from the LLM.
- Forward-only by design: a new video links to older videos; older videos are not retroactively updated. Reindex refreshes a video's block as a side effect (it runs through the same pipeline path).
- Column semantics: `NULL` = not yet computed (UI falls back to KNN). `"[]"` = computed, nothing relevant (UI falls back to KNN, same as NULL per the rendering rule).
- LLM model is the same config-selected model as summarization (the resolved `model_row`); introduce no new model config.
- Migrations are additive and idempotent, gated by a column-existence check via the existing `_ensure_column` helper in `app/db.py`.
- Run tests with `pytest` from the repo root. The project uses `asyncio.get_event_loop().run_until_complete(...)` in DB-migration tests and `pytest.mark.asyncio`-style async tests elsewhere — match the neighbouring test file's style.

---

### Task 1: DB column `related_links_json`

**Files:**
- Modify: `app/db.py` — add column to `SCHEMA` `videos` CREATE TABLE (after `image_query`, line ~49) and to the `videos` migration block (after the `image_query` `_ensure_column` call, line ~371)
- Test: `tests/test_db_migration_related_links.py` (create)

**Interfaces:**
- Produces: `videos.related_links_json TEXT` column (nullable).

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_related_links.py` (mirrors `tests/test_db_migration_image_query.py`):

```python
import asyncio

import aiosqlite

from app.config import Config
from app.db import connect, init_schema


def test_videos_gains_related_links_json_column(tmp_path):
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
    assert "related_links_json" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db_migration_related_links.py -v`
Expected: FAIL — `assert 'related_links_json' in cols` is False.

- [ ] **Step 3: Add the column to SCHEMA and the migration**

In `app/db.py`, in the `videos` CREATE TABLE inside `SCHEMA`, add after the `image_query TEXT` line (currently line ~49, the last column before the closing `);`):

```python
    image_query TEXT,
    -- JSON array of {video_id, title, reason} curated related summaries,
    -- computed once after generation (KNN pre-filter + LLM curation).
    -- NULL = not yet computed (UI falls back to live-KNN strip).
    -- "[]" = computed, nothing relevant.
    related_links_json TEXT
```

In `_run_migrations`, in the `if await _table_exists(conn, "videos"):` block, add after the `image_query` `_ensure_column` line (line ~371):

```python
        await _ensure_column(conn, "videos", "related_links_json", "TEXT")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db_migration_related_links.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db_migration_related_links.py
git commit -m "feat(related-summaries): add related_links_json column"
```

---

### Task 2: Video model field + repo read/write

**Files:**
- Modify: `app/models.py:73` — add `related_links_json` field to `Video` dataclass
- Modify: `app/repos/videos.py` — `_row_to_video` mapping (after `image_query`, ~line 49/71) and add `set_related_links` + `get_related_links` (near `set_highlights`/`get_highlights`, ~line 547–626)
- Test: `tests/test_repos_videos.py` (extend)

**Interfaces:**
- Consumes: `videos.related_links_json` column (Task 1).
- Produces:
  - `Video.related_links_json: str | None` (raw JSON string, like `highlights_json`).
  - `async def set_related_links(db, video_id: str, related_links_json: str) -> None`
  - `async def get_related_links(db, video_id: str) -> str | None`

- [ ] **Step 1: Write the failing repo test**

Find an existing test in `tests/test_repos_videos.py` that inserts a video (look for `upsert_metadata` usage) and copy its setup. Add:

```python
async def test_set_and_get_related_links(tmp_db):
    # tmp_db: copy whatever async DB fixture the neighbouring tests use.
    await videos_repo.upsert_metadata(
        tmp_db, video_id="v1", url="https://x/1", title="One",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    assert await videos_repo.get_related_links(tmp_db, "v1") is None

    blob = '[{"video_id": "v2", "title": "Two", "reason": "same topic"}]'
    await videos_repo.set_related_links(tmp_db, "v1", blob)

    assert await videos_repo.get_related_links(tmp_db, "v1") == blob
    v = await videos_repo.get(tmp_db, "v1")
    assert v.related_links_json == blob
```

Match the surrounding file's fixture/async style exactly (sync wrapper vs `@pytest.mark.asyncio`). If the file uses a helper to build/insert a video, reuse it instead of `upsert_metadata` literally.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_repos_videos.py -k related_links -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'set_related_links'` (or `Video` has no `related_links_json`).

- [ ] **Step 3: Add the model field**

In `app/models.py`, add after the `image_query` field (line ~73):

```python
    # JSON-encoded list of {video_id, title, reason} curated related
    # summaries. NULL = not yet computed (UI falls back to live-KNN).
    # "[]" = computed, nothing relevant found.
    related_links_json: str | None = None
```

- [ ] **Step 4: Map the column in `_row_to_video`**

In `app/repos/videos.py`, after the `image_query` fallback block (line ~46-49), add:

```python
    try:
        related_links_json = row["related_links_json"]
    except (IndexError, KeyError):
        related_links_json = None
```

And in the `Video(...)` constructor (after `image_query=image_query,`, line ~71):

```python
        related_links_json=related_links_json,
```

- [ ] **Step 5: Add `set_related_links` / `get_related_links`**

In `app/repos/videos.py`, after `get_highlights` (line ~626), add:

```python
async def set_related_links(
    db: aiosqlite.Connection, video_id: str, related_links_json: str,
) -> None:
    """Set the curated related-links JSON blob.

    Pass `"[]"` for "nothing relevant found". A NULL means "not yet
    computed" — leave it by simply not calling this function.
    """
    await db.execute(
        "UPDATE videos SET related_links_json=? WHERE id=?",
        (related_links_json, video_id),
    )
    await db.commit()


async def get_related_links(
    db: aiosqlite.Connection, video_id: str,
) -> str | None:
    cur = await db.execute(
        "SELECT related_links_json FROM videos WHERE id=?", (video_id,)
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return row[0]
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_repos_videos.py -k related_links -v`
Expected: PASS.

- [ ] **Step 7: Run the full repo test file to confirm no regression**

Run: `pytest tests/test_repos_videos.py -v`
Expected: PASS (all).

- [ ] **Step 8: Commit**

```bash
git add app/models.py app/repos/videos.py tests/test_repos_videos.py
git commit -m "feat(related-summaries): Video.related_links_json + repo read/write"
```

---

### Task 3: `compute_related_links` service

**Files:**
- Create: `app/services/related_links.py`
- Test: `tests/test_related_links.py` (create)

**Interfaces:**
- Consumes:
  - `related_svc.related_video_ids(db, video, *, user_id, limit) -> list[str]` (existing, `app/services/related.py`).
  - `videos_repo.get_many(db, ids) -> dict[str, Video]` (existing).
  - `model_row` object with `.model`, `.api_key`, `.base_url` (the resolved `llm_models` row; may be `None`).
- Produces:
  - `async def compute_related_links(db, *, video, user_id, model_row, candidate_limit=10) -> list[dict]`
    returns a list of `{"video_id": str, "title": str, "reason": str}`; possibly empty. Raises only on programming errors — LLM/JSON failures are NOT swallowed here (the pipeline boundary swallows them, Task 4), EXCEPT the empty-candidate short-circuit which returns `[]` with no LLM call.

Design notes for the implementer:
- The LLM call mirrors `app/services/stock_images.py::generate_image_query` (a self-contained `litellm.acompletion` driven by `model_row`).
- Reuse the JSON-blob extraction from `app/services/highlight_parser.py::_extract_json_blob` (import it) so we tolerate code-fenced JSON.
- Compact candidate context: title + highlights text if present, else summary truncated to 500 chars. Keep prompts small.

- [ ] **Step 1: Write the failing service tests**

Create `tests/test_related_links.py`:

```python
import json
from dataclasses import dataclass

import pytest

from app.services import related_links


@dataclass
class _FakeModelRow:
    model: str = "test/model"
    api_key: str = ""
    base_url: str = ""


def _video(vid, title, summary="some summary text"):
    # Minimal stand-in; compute_related_links only reads .id/.title/
    # .summary/.highlights_json on candidates and .id/.summary on the
    # subject. Use the real Video if the helper exists in conftest.
    from app.models import Video
    from datetime import datetime, UTC
    return Video(
        id=vid, url=f"https://x/{vid}", title=title, description="",
        thumbnail_path=None, duration_seconds=None, transcript=None,
        transcript_source=None, summary=summary, summary_model="m",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_empty_candidates_returns_empty_without_llm(monkeypatch):
    async def fake_related_ids(*a, **k):
        return []
    monkeypatch.setattr(related_links.related_svc, "related_video_ids",
                        fake_related_ids)
    called = {"llm": False}
    async def fake_llm(*a, **k):
        called["llm"] = True
        return ""
    monkeypatch.setattr(related_links, "_llm_select", fake_llm)

    out = await related_links.compute_related_links(
        db=None, video=_video("v1", "Subject"), user_id=1,
        model_row=_FakeModelRow(),
    )
    assert out == []
    assert called["llm"] is False


@pytest.mark.asyncio
async def test_hallucinated_ids_are_dropped(monkeypatch):
    async def fake_related_ids(*a, **k):
        return ["v2", "v3"]
    monkeypatch.setattr(related_links.related_svc, "related_video_ids",
                        fake_related_ids)

    async def fake_get_many(db, ids):
        return {
            "v2": _video("v2", "Two"),
            "v3": _video("v3", "Three"),
        }
    monkeypatch.setattr(related_links.videos_repo, "get_many", fake_get_many)

    # LLM returns one real id (v2) and one hallucinated id (v999).
    async def fake_llm(*a, **k):
        return json.dumps({"links": [
            {"video_id": "v2", "reason": "same topic"},
            {"video_id": "v999", "reason": "made up"},
        ]})
    monkeypatch.setattr(related_links, "_llm_select", fake_llm)

    out = await related_links.compute_related_links(
        db=None, video=_video("v1", "Subject"), user_id=1,
        model_row=_FakeModelRow(),
    )
    assert out == [
        {"video_id": "v2", "title": "Two", "reason": "same topic"},
    ]


@pytest.mark.asyncio
async def test_invalid_json_raises(monkeypatch):
    async def fake_related_ids(*a, **k):
        return ["v2"]
    monkeypatch.setattr(related_links.related_svc, "related_video_ids",
                        fake_related_ids)
    async def fake_get_many(db, ids):
        return {"v2": _video("v2", "Two")}
    monkeypatch.setattr(related_links.videos_repo, "get_many", fake_get_many)
    async def fake_llm(*a, **k):
        return "not json at all"
    monkeypatch.setattr(related_links, "_llm_select", fake_llm)

    with pytest.raises(Exception):
        await related_links.compute_related_links(
            db=None, video=_video("v1", "Subject"), user_id=1,
            model_row=_FakeModelRow(),
        )
```

If `conftest.py` already exposes a `Video` factory, prefer it over the local `_video` helper. Confirm the project's async-test convention (`pytest.mark.asyncio` is used in e.g. `tests/test_pipeline.py` — verify and match).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_related_links.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.related_links`.

- [ ] **Step 3: Implement the service**

Create `app/services/related_links.py`:

```python
"""Curated related-summaries computation (block-at-end feature).

Two-stage hybrid: the existing 384-d KNN (`related_video_ids`) pre-filters
candidates, then ONE LLM call picks the genuinely relevant ones and gives a
one-line reason. Validated against the candidate set (anti-hallucination).

Called once from the pipeline after the summary is embedded. The pipeline
wraps the call in try/except — failures here leave related_links_json NULL,
and the detail page falls back to the live-KNN strip.
"""
from __future__ import annotations

import json
import logging

import aiosqlite
import litellm

from app.models import Video
from app.repos import videos as videos_repo
from app.services import related as related_svc
from app.services.highlight_parser import _extract_json_blob

log = logging.getLogger(__name__)

_MAX_CONTEXT_CHARS = 500

_SELECT_PROMPT = """\
You are linking one summary to other summaries in the same personal library.

THIS summary's title: {subject_title}

Candidate summaries (each has an id, title, and a short context):
{candidates}

Pick ONLY the candidates that a reader of THIS summary would genuinely
benefit from following — same topic, direct follow-up, opposing view, or
shared key entity. Linking nothing is fine.

Return a single JSON object, no prose, with this exact shape:

{{"links": [{{"video_id": "<id from the list above>",
             "reason": "<one short sentence on why it's related>"}}]}}
"""


def _candidate_context(cand: Video) -> str:
    """Compact context for one candidate: highlights if present, else a
    truncated summary."""
    if cand.highlights_json:
        try:
            hl = json.loads(cand.highlights_json)
            texts = [h.get("text", "") for h in hl if isinstance(h, dict)]
            joined = "; ".join(t for t in texts if t)
            if joined:
                return joined[:_MAX_CONTEXT_CHARS]
        except (json.JSONDecodeError, TypeError):
            pass
    return (cand.summary or "")[:_MAX_CONTEXT_CHARS]


async def _llm_select(
    *, prompt: str, model_row,
) -> str:
    """One self-contained completion (mirrors stock_images)."""
    kwargs: dict = {
        "model": model_row.model,
        "messages": [{"role": "user", "content": prompt}],
        "api_key": model_row.api_key,
    }
    if model_row.base_url:
        kwargs["api_base"] = model_row.base_url
    resp = await litellm.acompletion(**kwargs)
    return resp.choices[0].message.content or ""


async def compute_related_links(
    db: aiosqlite.Connection,
    *,
    video: Video,
    user_id: int,
    model_row,
    candidate_limit: int = 10,
) -> list[dict]:
    """Curated related links for `video`. Returns a list of
    {video_id, title, reason}; possibly empty.

    Short-circuits to [] (no LLM call) when there are no KNN candidates or
    no usable model. Otherwise raises on LLM / JSON failure — the pipeline
    boundary is responsible for swallowing those.
    """
    if model_row is None:
        return []
    candidate_ids = await related_svc.related_video_ids(
        db, video, user_id=user_id, limit=candidate_limit,
    )
    if not candidate_ids:
        return []
    cands = await videos_repo.get_many(db, candidate_ids)
    # preserve KNN order, keep only ids we actually loaded
    ordered = [cands[i] for i in candidate_ids if i in cands]
    if not ordered:
        return []

    by_id = {c.id: c for c in ordered}
    block = "\n".join(
        f"- id={c.id} | title={c.title} | context={_candidate_context(c)}"
        for c in ordered
    )
    prompt = _SELECT_PROMPT.format(
        subject_title=video.title, candidates=block,
    )
    raw = await _llm_select(prompt=prompt, model_row=model_row)

    blob = _extract_json_blob(raw)
    if blob is None:
        raise ValueError("related-links LLM returned no JSON object")
    payload = json.loads(blob)  # raises JSONDecodeError on bad JSON
    links_raw = payload.get("links") if isinstance(payload, dict) else None
    if not isinstance(links_raw, list):
        raise ValueError("related-links JSON missing 'links' list")

    out: list[dict] = []
    seen: set[str] = set()
    for entry in links_raw:
        if not isinstance(entry, dict):
            continue
        vid = entry.get("video_id")
        reason = entry.get("reason", "")
        # anti-hallucination: id must be a real candidate
        if vid not in by_id or vid in seen:
            continue
        if not isinstance(reason, str):
            reason = ""
        seen.add(vid)
        out.append({
            "video_id": vid,
            "title": by_id[vid].title,  # trusted title, never the LLM's
            "reason": reason.strip(),
        })
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_related_links.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add app/services/related_links.py tests/test_related_links.py
git commit -m "feat(related-summaries): compute_related_links service (KNN + LLM curation)"
```

---

### Task 4: Pipeline hook

**Files:**
- Modify: `app/pipeline.py` — add import; call `compute_related_links` + `set_related_links` after `_try_embed_summary` (line ~338), wrapped in try/except
- Test: `tests/test_pipeline.py` (extend)

**Interfaces:**
- Consumes: `related_links.compute_related_links(...)` (Task 3), `videos_repo.set_related_links(...)` (Task 2), `model_row` (already resolved at pipeline line ~57-64).
- Produces: side effect — `related_links_json` populated (or left NULL on failure).

- [ ] **Step 1: Write the failing pipeline test**

In `tests/test_pipeline.py`, find how existing tests drive `process_video` (they monkeypatch summarizer/embeddings). Add a test asserting the hook is called and a failure is swallowed. Minimal version targeting the helper directly:

```python
@pytest.mark.asyncio
async def test_related_links_failure_does_not_break(monkeypatch):
    # compute_related_links raising must NOT propagate out of the hook.
    from app import pipeline
    from app.services import related_links

    async def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(related_links, "compute_related_links", boom)

    # _store_related_links is the small wrapper added in Step 3; it must
    # swallow. (If implemented inline instead, test via process_video.)
    await pipeline._store_related_links(
        db=None, video=None, user_id=1, model_row=object(),
    )  # must not raise
```

Adjust to the actual helper name/signature chosen in Step 3. If the team prefers a full `process_video` integration test, mirror the existing summarize-monkeypatch test in the file and assert `set_related_links` was called once.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline.py -k related_links -v`
Expected: FAIL — `AttributeError: module 'app.pipeline' has no attribute '_store_related_links'`.

- [ ] **Step 3: Add the hook**

In `app/pipeline.py`, add to the imports near the other service imports (top of file, alongside `from app.services.embeddings import embed_text`):

```python
from app.services import related_links
```

Add a small helper near `_try_embed_summary` (after it, ~line 399):

```python
async def _store_related_links(
    db, *, video, user_id: int, model_row,
) -> None:
    """Best-effort: compute + persist the curated related-links block.

    Never raises — related links are a nice-to-have and must not break
    the pipeline. On any failure the column stays NULL and the detail
    page falls back to the live-KNN strip.
    """
    try:
        links = await related_links.compute_related_links(
            db, video=video, user_id=user_id, model_row=model_row,
        )
        await videos_repo.set_related_links(
            db, video.id, json.dumps(links, ensure_ascii=False),
        )
    except Exception as e:  # noqa: BLE001 — best-effort, must not break
        log.warning(
            "related-links computation failed for %s: %s: %s",
            video.id, type(e).__name__, e,
        )
```

Then call it at the end of the summarize path, immediately after the `await _try_embed_summary(...)` line (~338). The embedding must exist first (KNN reads it):

```python
    await _try_embed_summary(db, video_id, summary, settings, set_step)

    # Curated related-summaries block (KNN pre-filter + LLM curation).
    # Runs AFTER embedding so this video's own vector is searchable, and
    # is best-effort: failure leaves related_links_json NULL.
    await set_step("finding related summaries")
    refreshed = await videos_repo.get(db, video_id)
    if refreshed is not None:
        await _store_related_links(
            db, video=refreshed, user_id=refreshed.user_id,
            model_row=model_row,
        )
```

Note: re-load the video (`refreshed`) so its just-written summary/embedding state is current; `model_row` is already in scope from line ~57.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pipeline.py -k related_links -v`
Expected: PASS.

- [ ] **Step 5: Run the full pipeline test file**

Run: `pytest tests/test_pipeline.py -v`
Expected: PASS (no regression).

- [ ] **Step 6: Commit**

```bash
git add app/pipeline.py tests/test_pipeline.py
git commit -m "feat(related-summaries): pipeline hook computes block after embedding"
```

---

### Task 5: Rendering — curated block with KNN fallback

**Files:**
- Create: `app/templates/related_summaries_section.html`
- Modify: `app/templates/video_detail.html:120-126` — render curated block when present, else keep the lazy KNN fragment
- Test: extend an existing route test that renders the detail page (look in `tests/` for a test hitting `GET /v/{id}`); if none, add a focused template-render assertion.

**Interfaces:**
- Consumes: `video.related_links_json` (Task 2). The detail route already passes `video` to the template — confirm and reuse; no route change needed if the template parses the JSON itself via a Jinja filter, OR parse in the route and pass `related_links`. Prefer parsing in the route (templates shouldn't `json.loads`).

- [ ] **Step 1: Decide the data hand-off (parse in route)**

In the detail route handler (`app/routes/videos.py`, `video_detail`, ~line 510), after loading `video`, add:

```python
    import json as _json
    related_links = []
    if video.related_links_json:
        try:
            related_links = _json.loads(video.related_links_json)
        except (ValueError, TypeError):
            related_links = []
```

and add `"related_links": related_links` to the template context dict passed to `TemplateResponse`. (Match the existing context-dict style in that handler.)

- [ ] **Step 2: Write the failing render test**

Find the existing detail-page route test (grep `tests/ -e "/v/"`). Add a case: a video with `related_links_json` set renders a link to the related video's id; a video with NULL renders the lazy `related-fragment` div. Example assertion shape (adapt to the file's client fixture):

```python
def test_detail_shows_curated_related_block(client_with_video):
    # video v1 has related_links_json = [{"video_id":"v2","title":"Two","reason":"r"}]
    resp = client_with_video.get("/v/v1")
    assert resp.status_code == 200
    assert "/v/v2" in resp.text
    assert "Two" in resp.text


def test_detail_falls_back_to_knn_fragment_when_null(client_with_video):
    # video v3 has related_links_json IS NULL
    resp = client_with_video.get("/v/v3")
    assert "/v/v3/related-fragment" in resp.text
```

Adapt fixtures to the existing test's setup (it likely seeds a DB + uses FastAPI `TestClient`).

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/ -k "related_block or related_fragment_when_null" -v`
Expected: FAIL (curated block not rendered).

- [ ] **Step 4: Create the curated-block partial**

Create `app/templates/related_summaries_section.html`:

```html
{# Curated related-summaries block (computed once in the pipeline).
   Rendered only when `related_links` is non-empty; the include site in
   video_detail.html falls back to the lazy KNN strip otherwise. #}
{% if related_links %}
  <section class="related-strip">
    <h3 class="section-heading">Related in your library</h3>
    <ul class="related-summaries-list">
      {% for link in related_links %}
        <li class="related-summary-item">
          <a class="related-summary-link" href="/v/{{ link.video_id }}">{{ link.title }}</a>
          {% if link.reason %}
            <span class="related-summary-reason">{{ link.reason }}</span>
          {% endif %}
        </li>
      {% endfor %}
    </ul>
  </section>
{% endif %}
```

- [ ] **Step 5: Wire the fallback in `video_detail.html`**

Replace the block at `app/templates/video_detail.html:120-126`:

```html
  {% if video.summary %}
    {# Related items load lazily once this placeholder is revealed, so the
       KNN cost stays off the detail-page render path (Part C.1). #}
    <div hx-get="/v/{{ video.id }}/related-fragment"
         hx-trigger="revealed"
         hx-swap="outerHTML"></div>
  {% endif %}
```

with:

```html
  {% if video.summary %}
    {% if related_links %}
      {# Curated block, computed once in the pipeline (forward-only). #}
      {% include "related_summaries_section.html" %}
    {% else %}
      {# No curated block yet (older video or compute failed): fall back to
         the lazy live-KNN strip. #}
      <div hx-get="/v/{{ video.id }}/related-fragment"
           hx-trigger="revealed"
           hx-swap="outerHTML"></div>
    {% endif %}
  {% endif %}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/ -k "related_block or related_fragment_when_null" -v`
Expected: PASS.

- [ ] **Step 7: Add minimal styling**

Append to the main stylesheet (find it: `grep -rl "related-strip" app/static`). Reuse `.related-strip` / `.section-heading` (already styled). Add:

```css
.related-summaries-list { list-style: none; margin: 0; padding: 0; }
.related-summary-item { margin: 0.4rem 0; }
.related-summary-reason { display: block; color: var(--muted, #888); font-size: 0.85em; }
```

(Use the existing muted-text variable name found in the stylesheet; adjust the fallback.)

- [ ] **Step 8: Commit**

```bash
git add app/templates/related_summaries_section.html app/templates/video_detail.html app/routes/videos.py app/static tests/
git commit -m "feat(related-summaries): render curated block with KNN fallback"
```

---

### Task 6: Manual verification

**Files:** none (verification only).

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 2: Verify in the running app**

Start the app (per project `run` conventions). Submit/reindex a video while the library already has a few related videos. Open its detail page; confirm a "Related in your library" block with titles + reasons appears. Open an OLDER video (one predating the feature, `related_links_json` NULL) and confirm the lazy KNN strip still loads.

- [ ] **Step 3: Final commit (if any verification fixups were needed)**

```bash
git add -A
git commit -m "chore(related-summaries): verification fixups"
```

---

## Self-Review

**Spec coverage:**
- DB column `related_links_json` → Task 1. ✓
- Service `compute_related_links` (KNN pre-filter + LLM + anti-hallucination + empty short-circuit) → Task 3. ✓
- Pipeline hook after embedding, best-effort try/except → Task 4. ✓
- Repo `set_related_links` + `Video.related_links_json` mapping → Task 2. ✓
- Rendering: single "Related" section, curated when present else KNN fallback → Task 5. ✓
- Forward-only / reindex-refreshes → satisfied by hooking the shared pipeline path (Task 4); no separate code path. ✓
- Error handling: LLM/JSON failure → NULL + KNN fallback (Task 4 swallow); empty candidates → `[]` no LLM (Task 3). ✓
- Testing: migration (Task 1), service incl. hallucination/empty/invalid-JSON (Task 3), repo round-trip (Task 2), render+fallback (Task 5). ✓

**Type consistency:** `compute_related_links` returns `list[dict]` of `{video_id, title, reason}` throughout (Task 3 produces, Task 4 serializes, Task 5 renders `link.video_id`/`link.title`/`link.reason`). `set_related_links(db, video_id, related_links_json: str)` consistent in Tasks 2 and 4. `related_links_json` column/field name consistent across Tasks 1, 2, 4, 5.

**Placeholder scan:** No TBD/TODO; every code step shows concrete code. The few "match the neighbouring test style / find the stylesheet" notes are deliberate discovery steps (the repo's exact fixtures vary by file), each with a concrete grep to resolve them — not deferred work.
