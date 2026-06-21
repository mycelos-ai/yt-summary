# Speaker Chat — PR 2: Chips, Activation & Speaker Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make speakers visible and actionable. Wire the deterministic detection from PR 1 into the pipeline (best-effort), render a speaker chip per detected speaker at the top of the video chat section, let the user activate/deactivate a speaker, ship a basic speaker page (header + confirmed-sources list, **no claims yet**), and add a "Paste text" add-tab that creates a `kind='text'` library item. **No claim extraction, no persona chat, no embeddings** — those are PR 3/4.

**Architecture:** This PR consumes PR 1's schema and repos (`speakers`, `source_speakers`, `chat_threads` tables; `repos/speakers.{resolve_speaker,get_speaker,list_for_user,set_active}`; `services/show_match.identify_from_metadata`; `VideoKind.TEXT`). It adds two thin repos (`repos/source_speakers.py`, `repos/chat_threads.py`), a best-effort pipeline detection hook placed right after `_store_related_links` (mirroring that function's never-fails posture), one new route module `app/routes/speakers.py` (HTMX fragment swaps, ownership-checked → `HTTPException(404)`), one new full-page template `speaker.html`, a chips partial, a chip-panel partial, an `_import_text` helper + a pasted-text branch in the existing `/videos` handler, and the "Paste text" tab in `_add_overlay.html`. The chat section in `video_detail.html` gains a chip strip at the top.

**Tech Stack:** Python 3.12, aiosqlite, FastAPI + Jinja2 + HTMX, pytest + pytest-asyncio. House test style: in-memory SQLite via the `db` fixture, FastAPI `TestClient` for routes, completions/network never hit (no live LLM, no browser).

## Global Constraints

- Python ≥ 3.12; use `StrEnum` for enums and `@dataclass` for records (matches `app/models.py`).
- All repo functions take `db: aiosqlite.Connection` as the first positional arg and default to `user_id=1` (matches `app/repos/chat.py`, `app/repos/speakers.py`). Mutating repo functions `await db.commit()` before returning.
- Idempotency: `link_speaker` is a no-op on the `source_speakers` `UNIQUE(source_id, speaker_id)`; `chat_threads.get_or_create` respects PR 1's per-scope partial unique indexes. Re-running detection on a video must not duplicate links.
- Ownership: every route loads the owning `Video`/`Speaker` and raises `HTTPException(404)` when `row.user_id != current_user_id` (the exact pattern in `app/routes/chat.py` and `app/routes/videos.py`). Foreign profile → 404, never 403.
- No seeded **positions**; nothing in this PR writes claims. Detection links *participants* only (`detection_source='show_rule'` / `'manual'`).
- The pipeline detection hook is **best-effort**: it is gated (YouTube kind + transcript present + LLM/detection viable) and **NEVER fails the job** — any exception is logged and swallowed, exactly like `_store_related_links`.
- `activate` in this PR **only flips `speakers.is_active`** via `repos/speakers.set_active` and returns the refreshed fragment. The library-wide backfill **job** is a PR-4 follow-up — do NOT enqueue it here.
- Commit after every green test. Branch base: the PR 1 branch (or a fresh `feat/speaker-chat-pr2`).
- Source of truth: [`docs/superpowers/specs/2026-06-21-chat-with-speakers-v1_5-design.md`](../specs/2026-06-21-chat-with-speakers-v1_5-design.md).

---

## File Structure

- `app/repos/source_speakers.py` — **create**: `link_speaker` / `list_for_source` / `unlink` over the `source_speakers` table.
- `app/repos/chat_threads.py` — **create**: `get_or_create` returning a `thread_id`, respecting the partial unique indexes.
- `app/repos/chat.py` — **modify**: make `append`/`history` thread-aware (optional `thread_id`; `video_id` optional/`None` for speaker-scope rows). Existing default call sites (positional `video_id`, no `thread_id`) keep working unchanged. **This is the thread-aware persistence PR 3's persona turns rely on** — see the dedicated task below.
- `app/services/speaker_pipeline.py` — **create**: `detect_and_link(db, video)` — the best-effort detection step the pipeline calls.
- `app/pipeline.py` — **modify**: call `speaker_pipeline.detect_and_link` after `_store_related_links`, behind `set_step("identifying speakers")`, swallowing all exceptions.
- `app/routes/speakers.py` — **create**: the video-speaker + speaker-page routes (detect / manual add / unlink / GET page / edit / activate / deactivate).
- `app/routes/videos.py` — **modify**: add the pasted-text branch to `POST /videos` and an `_import_text(...)` helper.
- `app/main.py` — **modify**: `include_router` the new speakers router.
- `app/templates/speaker.html` — **create**: speaker page (header + confirmed-sources list; clearly-marked extension points for claims + candidates, NOT built).
- `app/templates/_speaker_chips.html` — **create**: the chip strip rendered at the top of the chat section + injected by `/detect`.
- `app/templates/_speaker_chip_panel.html` — **create**: the "Activate {Name}?" panel / refreshed-chip fragment returned by activate/deactivate.
- `app/templates/_add_overlay.html` — **modify**: add a "Paste text" tab (textarea + optional title) to the add modal.
- `app/templates/video_detail.html` — **modify**: render `_speaker_chips.html` at the top of `<section class="chat">`.
- `tests/test_repos_source_speakers.py`, `tests/test_repos_chat_threads.py`, `tests/test_speaker_pipeline.py`, `tests/test_routes_speakers.py`, `tests/test_videos_pasted_text.py` — **create**.

---

## Interfaces this PR PRODUCES (PR 3–4 depend on these exact signatures)

```python
# app/repos/source_speakers.py
async def link_speaker(
    db, source_id: str, speaker_id: int, *,
    role: str | None = None,
    detection_source: str,                 # 'show_rule' | 'manual' | 'llm'
    sort_order: int = 0,
) -> int: ...                              # source_speakers.id; idempotent on UNIQUE(source_id, speaker_id)
async def list_for_source(db, source_id: str) -> list[Speaker]: ...   # JOIN speakers, ordered by sort_order, id
async def unlink(db, source_id: str, speaker_id: int) -> None: ...

# app/repos/chat_threads.py
async def get_or_create(
    db, *, user_id: int = 1, scope: str,   # 'source' | 'source_speaker' | 'speaker'
    source_id: str | None = None,
    speaker_id: int | None = None,
) -> int: ...                              # chat_threads.id; respects the per-scope partial unique indexes

# app/repos/chat.py — EXTENDED (backward-compatible)
async def append(
    db, video_id: str | None = None, role: ChatRole = ..., content: str = ...,
    *, user_id: int = 1, thread_id: int | None = None,
) -> ChatMessage: ...                      # video_id may be None for scope='speaker' rows (persisted as NULL)
async def history(
    db, video_id: str | None = None, *, thread_id: int | None = None,
) -> list[ChatMessage]: ...                # when thread_id is given, select WHERE thread_id=?; else legacy WHERE video_id=?
# Existing callers — append(db, video_id, role, content, user_id=...) and
# history(db, video_id) — are unchanged: thread_id defaults to None and the
# legacy video_id path is preserved. PR 3 persona turns call with thread_id set
# and (for scope='speaker') video_id=None.

# app/services/speaker_pipeline.py
async def detect_and_link(db, video) -> list[int]: ...   # resolved speaker_ids linked for this source; never raises

# app/routes/speakers.py  (HTMX fragments; ownership-checked → HTTPException(404))
#   POST /v/{video_id}/speakers/detect
#   POST /v/{video_id}/speakers                      (manual add: form `name`, optional `role`)
#   POST /v/{video_id}/speakers/{speaker_id}/unlink
#   GET  /speaker/{speaker_id}
#   POST /speaker/{speaker_id}/edit                  (name/role/avatar_id/style_note + optional photo upload)
#   POST /speaker/{speaker_id}/activate
#   POST /speaker/{speaker_id}/deactivate
```

> Consumes from PR 1: `app/models.Speaker`, `app/models.VideoKind.TEXT`, `app/repos/speakers.{normalize_name_key,resolve_speaker,get_speaker,list_for_user,set_active}`, `app/services/show_match.identify_from_metadata`. The `source_speakers` and `chat_threads` **tables** already exist (created in PR 1's `SCHEMA`).

---

### Task 1: `source_speakers` repo — link / list / unlink

**Files:**
- Create: `app/repos/source_speakers.py`
- Test: `tests/test_repos_source_speakers.py`

**Interfaces:**
- Consumes: `source_speakers` + `speakers` tables (PR 1), `app/models.Speaker`, `app/repos/speakers.resolve_speaker`.
- Produces: `link_speaker`, `list_for_source`, `unlink` (signatures in the PRODUCES block).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repos_source_speakers.py
import asyncio

from app.repos import source_speakers as ss_repo
from app.repos import speakers as sp_repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _seed_video(db, vid="v1"):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title) "
        "VALUES (?, 1, 'youtube', 'u', 't')",
        (vid,),
    )
    await db.commit()


def test_link_is_idempotent_and_lists_speaker(db):
    async def go():
        await _seed_video(db)
        sid = await sp_repo.resolve_speaker(db, name="Chamath Palihapitiya")
        first = await ss_repo.link_speaker(
            db, "v1", sid, role="host", detection_source="show_rule"
        )
        again = await ss_repo.link_speaker(
            db, "v1", sid, role="host", detection_source="show_rule"
        )
        assert first == again  # UNIQUE(source_id, speaker_id) → same row, no dupe
        people = await ss_repo.list_for_source(db, "v1")
        assert [p.name for p in people] == ["Chamath Palihapitiya"]
        cur = await db.execute(
            "SELECT COUNT(*) FROM source_speakers WHERE source_id='v1'"
        )
        assert (await cur.fetchone())[0] == 1
    _run(go())


def test_unlink_removes_only_that_link(db):
    async def go():
        await _seed_video(db)
        a = await sp_repo.resolve_speaker(db, name="Jason Calacanis")
        b = await sp_repo.resolve_speaker(db, name="David Sacks")
        await ss_repo.link_speaker(db, "v1", a, detection_source="manual")
        await ss_repo.link_speaker(db, "v1", b, detection_source="manual")
        await ss_repo.unlink(db, "v1", a)
        people = await ss_repo.list_for_source(db, "v1")
        assert [p.name for p in people] == ["David Sacks"]
    _run(go())


def test_list_orders_by_sort_order(db):
    async def go():
        await _seed_video(db)
        a = await sp_repo.resolve_speaker(db, name="Beta")
        b = await sp_repo.resolve_speaker(db, name="Alpha")
        await ss_repo.link_speaker(db, "v1", a, detection_source="show_rule", sort_order=0)
        await ss_repo.link_speaker(db, "v1", b, detection_source="show_rule", sort_order=1)
        people = await ss_repo.list_for_source(db, "v1")
        assert [p.name for p in people] == ["Beta", "Alpha"]  # sort_order, then id
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_repos_source_speakers.py -v`
Expected: FAIL — `ModuleNotFoundError: app.repos.source_speakers`.

- [ ] **Step 3: Implement the repo**

```python
# app/repos/source_speakers.py
import aiosqlite

from app.models import Speaker
from app.repos.speakers import _row_to_speaker


async def link_speaker(
    db: aiosqlite.Connection,
    source_id: str,
    speaker_id: int,
    *,
    role: str | None = None,
    detection_source: str,
    sort_order: int = 0,
) -> int:
    """Link a speaker to a library item. Idempotent on
    UNIQUE(source_id, speaker_id): a second call returns the existing
    row id and updates role/sort_order/detection_source in place."""
    await db.execute(
        "INSERT INTO source_speakers "
        "(source_id, speaker_id, role, detection_source, sort_order) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id, speaker_id) DO UPDATE SET "
        "role=excluded.role, detection_source=excluded.detection_source, "
        "sort_order=excluded.sort_order",
        (source_id, speaker_id, role, detection_source, sort_order),
    )
    await db.commit()
    cur = await db.execute(
        "SELECT id FROM source_speakers WHERE source_id=? AND speaker_id=?",
        (source_id, speaker_id),
    )
    row = await cur.fetchone()
    assert row is not None
    return row["id"]


async def list_for_source(db: aiosqlite.Connection, source_id: str) -> list[Speaker]:
    cur = await db.execute(
        "SELECT s.* FROM speakers s "
        "JOIN source_speakers ss ON ss.speaker_id = s.id "
        "WHERE ss.source_id=? "
        "ORDER BY ss.sort_order, ss.id",
        (source_id,),
    )
    return [_row_to_speaker(r) for r in await cur.fetchall()]


async def unlink(db: aiosqlite.Connection, source_id: str, speaker_id: int) -> None:
    await db.execute(
        "DELETE FROM source_speakers WHERE source_id=? AND speaker_id=?",
        (source_id, speaker_id),
    )
    await db.commit()
```

> `_row_to_speaker` is the row-mapper PR 1 defined in `app/repos/speakers.py`; reusing it keeps the `Speaker` mapping in one place. `SELECT s.*` returns every `speakers` column the mapper reads.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_repos_source_speakers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/repos/source_speakers.py tests/test_repos_source_speakers.py
git commit -m "feat(speakers): source_speakers repo (link/list/unlink, idempotent)"
```

---

### Task 2: `chat_threads` repo — `get_or_create`

**Files:**
- Create: `app/repos/chat_threads.py`
- Test: `tests/test_repos_chat_threads.py`

**Interfaces:**
- Consumes: `chat_threads` table + its three partial unique indexes (PR 1).
- Produces: `get_or_create` (signature in the PRODUCES block). PR 3 consumes this for `source_speaker` / `speaker` persona threads.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repos_chat_threads.py
import asyncio

from app.repos import chat_threads as ct_repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _seed(db):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title) "
        "VALUES ('v1', 1, 'youtube', 'u', 't')"
    )
    await db.execute("INSERT INTO speakers (user_id, name, name_key) VALUES (1,'X','x')")
    await db.commit()


def test_source_thread_is_stable(db):
    async def go():
        await _seed(db)
        a = await ct_repo.get_or_create(db, scope="source", source_id="v1")
        b = await ct_repo.get_or_create(db, scope="source", source_id="v1")
        assert a == b  # partial unique index → one thread per (user, source)
    _run(go())


def test_speaker_thread_is_stable(db):
    async def go():
        await _seed(db)
        a = await ct_repo.get_or_create(db, scope="speaker", speaker_id=1)
        b = await ct_repo.get_or_create(db, scope="speaker", speaker_id=1)
        assert a == b
    _run(go())


def test_source_speaker_thread_distinct_from_source(db):
    async def go():
        await _seed(db)
        s = await ct_repo.get_or_create(db, scope="source", source_id="v1")
        sp = await ct_repo.get_or_create(
            db, scope="source_speaker", source_id="v1", speaker_id=1
        )
        assert s != sp
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_repos_chat_threads.py -v`
Expected: FAIL — `ModuleNotFoundError: app.repos.chat_threads`.

- [ ] **Step 3: Implement the repo**

```python
# app/repos/chat_threads.py
import aiosqlite

# The three lookup predicates mirror PR 1's partial unique indexes
# (uq_chat_threads_source / _source_speaker / _speaker). We SELECT with
# the same WHERE shape, then INSERT only when absent — get-or-create
# under the per-scope NULL-safe uniqueness.
_LOOKUP = {
    "source": (
        "SELECT id FROM chat_threads "
        "WHERE user_id=? AND scope='source' AND source_id=?",
        lambda uid, source_id, speaker_id: (uid, source_id),
    ),
    "source_speaker": (
        "SELECT id FROM chat_threads "
        "WHERE user_id=? AND scope='source_speaker' AND source_id=? AND speaker_id=?",
        lambda uid, source_id, speaker_id: (uid, source_id, speaker_id),
    ),
    "speaker": (
        "SELECT id FROM chat_threads "
        "WHERE user_id=? AND scope='speaker' AND speaker_id=?",
        lambda uid, source_id, speaker_id: (uid, speaker_id),
    ),
}


async def get_or_create(
    db: aiosqlite.Connection,
    *,
    user_id: int = 1,
    scope: str,
    source_id: str | None = None,
    speaker_id: int | None = None,
) -> int:
    if scope not in _LOOKUP:
        raise ValueError(f"unknown thread scope: {scope!r}")
    sql, args_fn = _LOOKUP[scope]
    cur = await db.execute(sql, args_fn(user_id, source_id, speaker_id))
    row = await cur.fetchone()
    if row is not None:
        return row["id"]
    cur = await db.execute(
        "INSERT INTO chat_threads (user_id, scope, source_id, speaker_id) "
        "VALUES (?, ?, ?, ?)",
        (user_id, scope, source_id, speaker_id),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_repos_chat_threads.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/repos/chat_threads.py tests/test_repos_chat_threads.py
git commit -m "feat(speakers): chat_threads repo get_or_create (per-scope unique)"
```

---

### Task 2b: Make `chat_repo.append`/`history` thread-aware

PR 3's persona turns persist on a `thread_id` (and, for `scope='speaker'`, with `video_id=NULL` — PR 1 made the column nullable). Extend `repos/chat.py` **backward-compatibly** so existing video-chat callers are untouched.

**Files:**
- Modify: `app/repos/chat.py`
- Test: `tests/test_repos_chat.py` (append — these new cases sit alongside the existing ones, which must stay green)

**Interfaces:**
- Consumes: `chat_messages.thread_id` + nullable `video_id` (PR 1).
- Produces: thread-aware `append`/`history` (signatures in the PRODUCES block). PR 3 consumes both.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repos_chat.py (append — do NOT modify existing tests)
import asyncio
from app.repos import chat as chat_repo


def _run(c): return asyncio.get_event_loop().run_until_complete(c)


def test_append_and_history_by_thread_with_null_video(db):
    async def go():
        await db.execute("INSERT INTO speakers (user_id, name, name_key) VALUES (1,'X','x')")
        await db.execute("INSERT INTO chat_threads (user_id, scope, speaker_id) VALUES (1,'speaker',1)")
        await db.commit()
        await chat_repo.append(db, None, "user", "hi", thread_id=1)
        await chat_repo.append(db, None, "assistant", "yo", thread_id=1)
        msgs = await chat_repo.history(db, thread_id=1)
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[0].video_id is None
    _run(go())
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_repos_chat.py::test_append_and_history_by_thread_with_null_video -v`
Expected: FAIL — `history()` has no `thread_id` kwarg / `append` rejects `None` video.

- [ ] **Step 3: Extend the repo (backward-compatible)**

Edit `app/repos/chat.py`. Update `_row_to_msg` is unchanged. Replace `append` and `history`:

```python
async def append(
    db: aiosqlite.Connection,
    video_id: str | None = None,
    role: ChatRole = "user",
    content: str = "",
    *,
    user_id: int = 1,
    thread_id: int | None = None,
) -> ChatMessage:
    cursor = await db.execute(
        "INSERT INTO chat_messages (user_id, video_id, role, content, thread_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, video_id, role, content, thread_id),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    fetched = await db.execute("SELECT * FROM chat_messages WHERE id=?", (cursor.lastrowid,))
    row = await fetched.fetchone()
    assert row is not None
    return _row_to_msg(row)


async def history(
    db: aiosqlite.Connection,
    video_id: str | None = None,
    *,
    thread_id: int | None = None,
) -> list[ChatMessage]:
    if thread_id is not None:
        cursor = await db.execute(
            "SELECT * FROM chat_messages WHERE thread_id=? ORDER BY created_at ASC, id ASC",
            (thread_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM chat_messages WHERE video_id=? ORDER BY created_at ASC, id ASC",
            (video_id,),
        )
    rows = await cursor.fetchall()
    return [_row_to_msg(r) for r in rows]
```

> `_row_to_msg` reads `row["video_id"]` which is now sometimes `NULL` → `None`; the `ChatMessage.video_id` field must allow `str | None`. If `app/models.py`'s `ChatMessage.video_id` is typed `str`, widen it to `str | None`.

- [ ] **Step 4: Run to verify pass + the existing chat-repo tests**

Run: `.venv/bin/pytest tests/test_repos_chat.py -v`
Expected: PASS — both the new thread test and ALL pre-existing video-id tests.

- [ ] **Step 5: Commit**

```bash
git add app/repos/chat.py app/models.py tests/test_repos_chat.py
git commit -m "feat(speakers): thread-aware chat_repo append/history (nullable video_id)"
```

---

### Task 3: Detection service + best-effort pipeline hook

The pipeline runs `identify_from_metadata` after summarization, resolves each detected name to a `speakers` row, and links it into `source_speakers` with `detection_source='show_rule'`. Gated like the other enrichments (YouTube kind + transcript present) and — like `_store_related_links` — it **never fails the job**. **No claim extraction here** (PR 3 adds the piggyback).

**Files:**
- Create: `app/services/speaker_pipeline.py`
- Modify: `app/pipeline.py` — insert the hook right after the `_store_related_links` call (around line 350).
- Test: `tests/test_speaker_pipeline.py`

**Interfaces:**
- Consumes: `show_match.identify_from_metadata` (PR 1), `repos/speakers.resolve_speaker` (PR 1), `repos/source_speakers.link_speaker` (Task 1), `app/models.VideoKind`.
- Produces: `detect_and_link(db, video) -> list[int]` (never raises).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_speaker_pipeline.py
import asyncio

from app.models import Video, VideoKind
from app.repos import source_speakers as ss_repo
from app.services import speaker_pipeline


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _seed_video_row(db, vid="v1", channel_id="UCchan"):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, channel_id, transcript) "
        "VALUES (?, 1, 'youtube', 'u', 'Elon Musk: Mars | Lex Fridman Podcast #1', ?, 'body')",
        (vid, channel_id),
    )
    await db.execute(
        "INSERT INTO known_shows (user_id, name, channel_id, hosts_json, guest_rule, enabled) "
        "VALUES (NULL, 'Lex Fridman Podcast', 'UCchan', '[\"Lex Fridman\"]', 'before:: ', 1)"
    )
    await db.commit()


def _video(**kw):
    base = dict(
        id="v1", user_id=1, kind=VideoKind.YOUTUBE, url="u",
        title="Elon Musk: Mars | Lex Fridman Podcast #1", description="",
        thumbnail_path=None, duration_seconds=None, transcript="body",
        transcript_source=None, summary="s", summary_model="m",
        created_at=None, updated_at=None, channel_id="UCchan",
    )
    base.update(kw)
    return Video(**base)


def test_detect_and_link_links_host_and_guest(db):
    async def go():
        await _seed_video_row(db)
        ids = await speaker_pipeline.detect_and_link(db, _video())
        assert len(ids) == 2
        people = {p.name for p in await ss_repo.list_for_source(db, "v1")}
        assert "Lex Fridman" in people and "Elon Musk" in people
    _run(go())


def test_detect_and_link_skips_non_youtube(db):
    async def go():
        # A web item must not be detected against show rules.
        v = _video(kind=VideoKind.WEB, channel_id=None)
        assert await speaker_pipeline.detect_and_link(db, v) == []
    _run(go())


def test_detect_and_link_skips_when_no_transcript(db):
    async def go():
        await _seed_video_row(db)
        v = _video(transcript=None)
        assert await speaker_pipeline.detect_and_link(db, v) == []
    _run(go())


def test_detect_and_link_swallows_errors(db):
    async def go():
        # No videos row exists for 'ghost', so link_speaker's FK would
        # raise — detect_and_link must swallow it and return [].
        v = _video(id="ghost")
        # also no known_shows row → identify returns [], so force a match:
        await db.execute(
            "INSERT INTO known_shows (user_id, name, channel_id, hosts_json, enabled) "
            "VALUES (NULL, 'X', 'UCchan', '[\"Host\"]', 1)"
        )
        await db.commit()
        result = await speaker_pipeline.detect_and_link(db, v)
        assert result == []  # FK violation on 'ghost' swallowed
    _run(go())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_speaker_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.speaker_pipeline`.

- [ ] **Step 3: Implement the service**

```python
# app/services/speaker_pipeline.py
"""Best-effort speaker detection for the pipeline.

Deterministic: matches the video's metadata against known_shows
(via show_match) and links the resulting participants into
source_speakers. No LLM, no transcript parsing, no claims — claim
extraction is the PR-3 piggyback. Like pipeline._store_related_links,
this NEVER raises: speaker detection is a nice-to-have and must never
fail the job.
"""
import logging

from app.models import VideoKind
from app.repos import source_speakers as ss_repo
from app.repos import speakers as sp_repo
from app.services import show_match

log = logging.getLogger(__name__)


async def detect_and_link(db, video) -> list[int]:
    """Detect speakers from metadata and link them as source_speakers.

    Returns the resolved speaker_ids linked for this source ([] when
    nothing matched, the video is ineligible, or anything failed)."""
    if video.kind != VideoKind.YOUTUBE or not video.transcript:
        return []
    linked: list[int] = []
    try:
        detected = await show_match.identify_from_metadata(db, video)
        for order, det in enumerate(detected):
            speaker_id = await sp_repo.resolve_speaker(
                db, user_id=video.user_id, name=det.name, role=det.role,
            )
            await ss_repo.link_speaker(
                db, video.id, speaker_id,
                role=det.role, detection_source="show_rule", sort_order=order,
            )
            linked.append(speaker_id)
    except Exception as e:  # noqa: BLE001 — best-effort, must not break the job
        log.warning(
            "speaker detection failed for %s: %s: %s",
            getattr(video, "id", None), type(e).__name__, e,
        )
        return []
    return linked
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_speaker_pipeline.py -v`
Expected: PASS.

- [ ] **Step 5: Wire the hook into the pipeline**

In `app/pipeline.py`, in `process_video`, the related-links block ends around line 350:

```python
    await set_step("finding related summaries")
    refreshed = await videos_repo.get(db, video_id)
    if refreshed is not None:
        await _store_related_links(
            db, video=refreshed, user_id=refreshed.user_id,
            model_row=model_row,
        )
```

Add, immediately after that block:

```python
    # Best-effort speaker detection (PR 2). Deterministic metadata match
    # only — no claims (the claim-extraction piggyback is PR 3). Mirrors
    # _store_related_links: gated, and never fails the job.
    if refreshed is not None:
        await set_step("identifying speakers")
        from app.services import speaker_pipeline
        await speaker_pipeline.detect_and_link(db, refreshed)
```

> `speaker_pipeline.detect_and_link` already swallows every exception, so no extra `try/except` is needed at the call site — the same contract `_store_related_links` relies on. The import is local to keep the pipeline module's import graph unchanged (the codebase already does local `from app.services import ...` imports inside `process_video`, e.g. `stock_images`).

- [ ] **Step 6: Run the pipeline-touching suite to prove no regression**

Run: `.venv/bin/pytest tests/test_speaker_pipeline.py tests/test_pipeline.py -q`
Expected: PASS (the new hook is additive and best-effort).

> If `tests/test_pipeline.py` does not exist under that exact name, run the repo's pipeline test file (discover with `ls tests | grep pipeline`) plus `tests/test_repos_videos.py`.

- [ ] **Step 7: Commit**

```bash
git add app/services/speaker_pipeline.py app/pipeline.py tests/test_speaker_pipeline.py
git commit -m "feat(speakers): best-effort detection hook in pipeline"
```

---

### Task 4: Speaker routes — detect / manual add / unlink

`app/routes/speakers.py` mirrors `app/routes/chat.py`: `Depends(get_current_user_id)` + `Depends(get_db)` from `app.main`, ownership-check → `HTTPException(404)`, `response_class=HTMLResponse`, a `Jinja2Templates` instance like `app/routes/videos.py`. These three routes operate on a video's chip strip and return the re-rendered `_speaker_chips.html` fragment.

**Files:**
- Create: `app/routes/speakers.py` (router + the three video-speaker routes)
- Create: `app/templates/_speaker_chips.html`
- Modify: `app/main.py` — register the router.
- Test: `tests/test_routes_speakers.py`

**Interfaces:**
- Consumes: `repos/videos.get`, `repos/source_speakers.{link_speaker,list_for_source,unlink}`, `repos/speakers.resolve_speaker`, `services/show_match`/`speaker_pipeline`, `services/avatars`.
- Produces: `POST /v/{video_id}/speakers/detect`, `POST /v/{video_id}/speakers`, `POST /v/{video_id}/speakers/{speaker_id}/unlink`.

- [ ] **Step 1: Write the chips partial**

```html
{# app/templates/_speaker_chips.html
   The chip strip at the top of the chat section. One chip per detected
   speaker; active chips deep-link to the speaker page, inactive chips
   open the activate panel. `speakers` is a list[Speaker]; `video` is
   the owning Video. Re-rendered by /detect, /speakers, /unlink. #}
<div id="speaker-chips" class="speaker-chips">
  {% if speakers %}
    <span class="speaker-chips-label">Chat with:</span>
    {% for sp in speakers %}
      <span class="speaker-chip {% if sp.is_active %}is-active{% else %}is-inactive{% endif %}"
            style="--avatar-bg: {{ sp.avatar_id | avatar_bg }};">
        <a class="speaker-chip-name" href="/speaker/{{ sp.id }}">
          {% if sp.avatar_photo_path %}
            <img class="speaker-chip-avatar" src="/speaker/{{ sp.id }}/photo" alt="">
          {% endif %}
          {{ sp.name }}
        </a>
        {% if not sp.is_active %}
          <button type="button" class="speaker-chip-activate"
                  hx-post="/speaker/{{ sp.id }}/activate"
                  hx-target="closest .speaker-chip" hx-swap="outerHTML"
                  title="Activate {{ sp.name }}">＋</button>
        {% endif %}
        <button type="button" class="speaker-chip-unlink"
                hx-post="/v/{{ video.id }}/speakers/{{ sp.id }}/unlink"
                hx-target="#speaker-chips" hx-swap="outerHTML"
                title="Remove {{ sp.name }} from this item">×</button>
      </span>
    {% endfor %}
  {% endif %}
  <form class="speaker-chip-add" hx-post="/v/{{ video.id }}/speakers"
        hx-target="#speaker-chips" hx-swap="outerHTML"
        hx-on::after-request="this.reset()">
    <input name="name" placeholder="+ add a speaker" autocomplete="off" required>
  </form>
</div>
```

> `avatar_bg` is a Jinja filter wrapping `app.services.avatars.bg_color_for`. Register it where the other filters are registered (`app/template_filters.register_filters`, already imported by `routes/videos.py`). If a dedicated filter is heavier than warranted, the template may instead read a precomputed `bg` passed in the context — but the filter keeps the partial reusable. `/speaker/{id}/photo` is the photo-serving route added in Task 6 (only referenced when `avatar_photo_path` is set).

- [ ] **Step 2: Write the failing route tests**

```python
# tests/test_routes_speakers.py
import asyncio

from fastapi.testclient import TestClient

from app.main import create_app


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    return app, TestClient(app)


async def _seed_video(db, vid="vc1", user_id=1):
    from app.repos import videos as videos_repo
    from app.models import TranscriptSource
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="Elon Musk: Mars | Lex Fridman Podcast #1",
        description="", thumbnail_path=None, duration_seconds=None, user_id=user_id,
    )
    await videos_repo.set_transcript(db, vid, "body", TranscriptSource.MANUAL_SUBS)
    await db.execute(
        "UPDATE videos SET channel_id='UCchan' WHERE id=?", (vid,)
    )
    await db.execute(
        "INSERT INTO known_shows (user_id, name, channel_id, hosts_json, guest_rule, enabled) "
        "VALUES (NULL, 'Lex Fridman Podcast', 'UCchan', '[\"Lex Fridman\"]', 'before:: ', 1)"
    )
    await db.commit()


def test_detect_links_and_renders_chips(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        resp = client.post("/v/vc1/speakers/detect")
        assert resp.status_code == 200
        assert "Lex Fridman" in resp.text
        assert "Elon Musk" in resp.text


def test_manual_add_creates_chip(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        resp = client.post("/v/vc1/speakers", data={"name": "Guest Person"})
        assert resp.status_code == 200
        assert "Guest Person" in resp.text


def test_unlink_removes_chip(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Temp Person"})

        async def sid():
            from app.repos import speakers as sp_repo
            s = await sp_repo.resolve_speaker(app.state.db, name="Temp Person")
            return s
        speaker_id = _run(sid())
        resp = client.post(f"/v/vc1/speakers/{speaker_id}/unlink")
        assert resp.status_code == 200
        assert "Temp Person" not in resp.text


def test_detect_foreign_video_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db, vid="vforeign", user_id=999))
        resp = client.post("/v/vforeign/speakers/detect")
        assert resp.status_code == 404
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_routes_speakers.py -v`
Expected: FAIL — router not registered (404 on every path) / module absent.

- [ ] **Step 4: Implement the router + register it**

```python
# app/routes/speakers.py
import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.main import get_current_user_id, get_db
from app.repos import source_speakers as ss_repo
from app.repos import speakers as sp_repo
from app.repos import videos as videos_repo
from app.services import speaker_pipeline
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates)


async def _owned_video(db, video_id: str, current_user_id: int):
    """Load a video or 404 — including foreign-profile rows (404, not 403,
    matching routes/chat.py + routes/videos.py)."""
    video = await videos_repo.get(db, video_id)
    if video is None or video.user_id != current_user_id:
        raise HTTPException(404)
    return video


def _chips_response(request: Request, video, speakers) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_speaker_chips.html", {"video": video, "speakers": speakers}
    )


@router.post("/v/{video_id}/speakers/detect", response_class=HTMLResponse)
async def detect_speakers(
    request: Request,
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await _owned_video(db, video_id, current_user_id)
    await speaker_pipeline.detect_and_link(db, video)
    speakers = await ss_repo.list_for_source(db, video_id)
    return _chips_response(request, video, speakers)


@router.post("/v/{video_id}/speakers", response_class=HTMLResponse)
async def add_speaker(
    request: Request,
    video_id: str,
    name: str = Form(...),
    role: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await _owned_video(db, video_id, current_user_id)
    speaker_id = await sp_repo.resolve_speaker(
        db, user_id=current_user_id, name=name.strip(), role=role.strip() or None,
    )
    await ss_repo.link_speaker(
        db, video_id, speaker_id,
        role=role.strip() or None, detection_source="manual",
    )
    speakers = await ss_repo.list_for_source(db, video_id)
    return _chips_response(request, video, speakers)


@router.post("/v/{video_id}/speakers/{speaker_id}/unlink", response_class=HTMLResponse)
async def unlink_speaker(
    request: Request,
    video_id: str,
    speaker_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await _owned_video(db, video_id, current_user_id)
    await ss_repo.unlink(db, video_id, speaker_id)
    speakers = await ss_repo.list_for_source(db, video_id)
    return _chips_response(request, video, speakers)
```

In `app/main.py`, inside `create_app`, after the `chat_router` registration (around line 268), add:

```python
    from app.routes.speakers import router as speakers_router
    app.include_router(speakers_router)
```

> Register the `avatar_bg` filter in `app/template_filters.register_filters` (the chips partial uses it):
> ```python
>     from app.services import avatars
>     templates.env.filters["avatar_bg"] = avatars.bg_color_for
> ```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_routes_speakers.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routes/speakers.py app/templates/_speaker_chips.html app/main.py app/template_filters.py tests/test_routes_speakers.py
git commit -m "feat(speakers): video chip routes (detect/add/unlink) + chips partial"
```

---

### Task 5: Render chips in the video detail chat section

Render the chip strip at the top of `<section class="chat">` so chips appear on page load (not only after a `/detect` POST). The detail-page handler must pass `speakers = list_for_source(...)` into the template context.

**Files:**
- Modify: `app/templates/video_detail.html` — include `_speaker_chips.html` at the top of `<section class="chat">`.
- Modify: the GET handler that renders `video_detail.html` — add `speakers` to its context. (Find it: `grep -n "video_detail.html" app/routes/*.py` — it lives in `app/routes/home.py` or `app/routes/videos.py`.)
- Test: `tests/test_routes_speakers.py` (append).

**Interfaces:**
- Consumes: `repos/source_speakers.list_for_source`.
- Produces: chips visible on the detail page.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routes_speakers.py (append)
def test_detail_page_shows_chips_after_detection(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers/detect")          # creates the links
        page = client.get("/v/vc1")                     # full detail page
        assert page.status_code == 200
        assert 'id="speaker-chips"' in page.text
        assert "Lex Fridman" in page.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_routes_speakers.py::test_detail_page_shows_chips_after_detection -v`
Expected: FAIL — the chip strip is not in the rendered page.

- [ ] **Step 3: Edit the template**

In `app/templates/video_detail.html`, inside `<section class="chat">`, immediately after `<h2>Chat</h2>` (line ~177), add:

```html
      {% include "_speaker_chips.html" %}
```

- [ ] **Step 4: Pass `speakers` in the detail context**

In the GET handler that renders `video_detail.html`, after the `video` is loaded, add:

```python
    from app.repos import source_speakers as ss_repo
    speakers = await ss_repo.list_for_source(db, video.id)
```

and add `"speakers": speakers` to the `TemplateResponse` context dict.

> The chip strip's `{% include %}` needs `video` and `speakers` in scope. `video` is already there. If the detail handler builds its context without those repos imported, add the import locally as shown. Confirm the handler location first with `grep -n "video_detail.html" app/routes/*.py`.

- [ ] **Step 5: Run to verify pass + chat regression**

Run: `.venv/bin/pytest tests/test_routes_speakers.py tests/test_routes_chat.py -q`
Expected: PASS — chips render; existing chat route tests **unchanged**.

- [ ] **Step 6: Commit**

```bash
git add app/templates/video_detail.html app/routes/*.py tests/test_routes_speakers.py
git commit -m "feat(speakers): render chip strip in the video chat section"
```

---

### Task 6: Speaker page (header + confirmed sources) + edit + photo serve

`GET /speaker/{id}` renders `speaker.html`: header (name, role, avatar/photo, style_note, edit action, activate/deactivate toggle) + a confirmed-sources list (the library items where the speaker appears). **No claims, no candidates** — those are PR 3/4; leave clearly-marked extension-point comments only. `POST /speaker/{id}/edit` updates name/role/avatar_id/style_note and accepts an optional photo upload. `GET /speaker/{id}/photo` serves the uploaded photo (referenced by the chips partial).

**Files:**
- Modify: `app/routes/speakers.py` — add the page, edit, and photo-serve routes.
- Modify: `app/repos/speakers.py` — add `update_fields` + `set_photo_path` (small helpers) and a confirmed-sources lister.
- Create: `app/templates/speaker.html`
- Test: `tests/test_routes_speakers.py` (append)

**Interfaces:**
- Consumes: `repos/speakers.get_speaker`, `repos/source_speakers` (reverse: sources for a speaker), `services/avatars`.
- Produces: `GET /speaker/{id}`, `POST /speaker/{id}/edit`, `GET /speaker/{id}/photo`; `repos/speakers.update_fields`, `repos/speakers.set_photo_path`, `repos/source_speakers.list_sources_for_speaker`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_routes_speakers.py (append)
def test_speaker_page_renders_header_and_sources(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers/detect")

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, name="Lex Fridman")
        speaker_id = _run(sid())

        page = client.get(f"/speaker/{speaker_id}")
        assert page.status_code == 200
        assert "Lex Fridman" in page.text
        # confirmed source (the seeded video title) appears in the sources list
        assert "Lex Fridman Podcast" in page.text


def test_speaker_edit_updates_fields(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Editable Person"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, name="Editable Person")
        speaker_id = _run(sid())

        client.post(
            f"/speaker/{speaker_id}/edit",
            data={"name": "Renamed Person", "role": "guest",
                  "avatar_id": "adult-scientist-m", "style_note": "calm"},
        )

        async def check():
            from app.repos import speakers as sp_repo
            sp = await sp_repo.get_speaker(app.state.db, speaker_id)
            return sp
        sp = _run(check())
        assert sp.name == "Renamed Person"
        assert sp.role == "guest"
        assert sp.style_note == "calm"


def test_speaker_page_foreign_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        async def make_foreign():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(
                app.state.db, user_id=999, name="Not Yours"
            )
        speaker_id = _run(make_foreign())
        page = client.get(f"/speaker/{speaker_id}")
        assert page.status_code == 404
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_routes_speakers.py -k "speaker_page or speaker_edit" -v`
Expected: FAIL — routes absent.

- [ ] **Step 3: Add repo helpers**

In `app/repos/speakers.py`, add:

```python
async def update_fields(
    db: aiosqlite.Connection, speaker_id: int, *,
    name: str, role: str | None, avatar_id: str | None, style_note: str | None,
) -> None:
    """Edit the user-facing identity fields. name_key is re-derived from
    name so a rename stays the identity anchor."""
    await db.execute(
        "UPDATE speakers SET name=?, name_key=?, role=?, avatar_id=?, "
        "style_note=?, updated_at=datetime('now') WHERE id=?",
        (name, normalize_name_key(name), role, avatar_id, style_note, speaker_id),
    )
    await db.commit()


async def set_photo_path(
    db: aiosqlite.Connection, speaker_id: int, path: str | None,
) -> None:
    await db.execute(
        "UPDATE speakers SET avatar_photo_path=?, updated_at=datetime('now') "
        "WHERE id=?",
        (path, speaker_id),
    )
    await db.commit()
```

In `app/repos/source_speakers.py`, add the reverse lister:

```python
async def list_sources_for_speaker(db: aiosqlite.Connection, speaker_id: int):
    """Confirmed library items (videos rows) where the speaker appears.
    Returns the rows ordered newest-first; the route maps them to Video."""
    cur = await db.execute(
        "SELECT v.* FROM videos v "
        "JOIN source_speakers ss ON ss.source_id = v.id "
        "WHERE ss.speaker_id=? AND v.archived_at IS NULL "
        "ORDER BY v.created_at DESC, v.id",
        (speaker_id,),
    )
    return await cur.fetchall()
```

> Returning raw rows (not full `Video` dataclasses) keeps this lister cheap — the template reads `row["id"]` / `row["title"]` / `row["kind"]` directly. The page only needs id + title + kind for a link list.

- [ ] **Step 4: Write the speaker page template**

```html
{# app/templates/speaker.html — PR 2: header + confirmed sources only.
   Claims (dossier) and candidate "possible sources" are PR 3/4 — the
   extension points are marked below but intentionally NOT built. #}
{% extends "base.html" %}
{% block content %}
<article class="speaker-page">
  <header class="speaker-header" style="--avatar-bg: {{ speaker.avatar_id | avatar_bg }};">
    {% if speaker.avatar_photo_path %}
      <img class="speaker-photo" src="/speaker/{{ speaker.id }}/photo" alt="{{ speaker.name }}">
    {% else %}
      <span class="speaker-avatar speaker-avatar-{{ speaker.avatar_id or 'default' }}"></span>
    {% endif %}
    <div class="speaker-id">
      <h1>{{ speaker.name }}</h1>
      {% if speaker.role %}<p class="speaker-role">{{ speaker.role }}</p>{% endif %}
      {% if speaker.style_note %}<p class="speaker-style">{{ speaker.style_note }}</p>{% endif %}
    </div>
    <div class="speaker-actions">
      {% if speaker.is_active %}
        <button hx-post="/speaker/{{ speaker.id }}/deactivate"
                hx-target="closest .speaker-actions" hx-swap="innerHTML">Deactivate</button>
      {% else %}
        <button hx-post="/speaker/{{ speaker.id }}/activate"
                hx-target="closest .speaker-actions" hx-swap="innerHTML">Activate</button>
      {% endif %}
    </div>
  </header>

  <form class="speaker-edit" method="post" action="/speaker/{{ speaker.id }}/edit"
        enctype="multipart/form-data">
    <input name="name" value="{{ speaker.name }}" required>
    <input name="role" value="{{ speaker.role or '' }}" placeholder="role">
    <input name="style_note" value="{{ speaker.style_note or '' }}" placeholder="speaking style">
    <select name="avatar_id">
      <option value="">— avatar —</option>
      {% for av in avatars %}
        <option value="{{ av.id }}" {% if av.id == speaker.avatar_id %}selected{% endif %}>{{ av.label }}</option>
      {% endfor %}
    </select>
    <input type="file" name="photo" accept="image/*">
    <button type="submit">Save</button>
  </form>

  <section class="speaker-sources">
    <h2>Appears in</h2>
    {% if sources %}
      <ul>
        {% for s in sources %}
          <li><a href="/v/{{ s['id'] }}">{{ s['title'] }}</a> <span class="src-kind">{{ s['kind'] }}</span></li>
        {% endfor %}
      </ul>
    {% else %}
      <p class="muted">No confirmed sources yet.</p>
    {% endif %}
  </section>

  {# EXTENSION POINT (PR 3): claims/dossier grouped by topic go here. #}
  {# EXTENSION POINT (PR 4): "Possible sources" (candidates) + whole-dossier chat go here. #}
</article>
{% endblock %}
```

> `base.html` is the project's layout (the other full-page templates extend it — confirm the block name with `grep -n "{% block" app/templates/base.html`; adjust `content` if the project uses a different block name). `avatars` is `app.services.avatars.AVATARS`, passed by the route.

- [ ] **Step 5: Implement the routes**

Append to `app/routes/speakers.py`:

```python
from pathlib import Path

from fastapi import UploadFile
from fastapi.responses import FileResponse

from app.config import Config
from app.main import get_config
from app.repos import source_speakers as ss_repo  # already imported above
from app.services import avatars


async def _owned_speaker(db, speaker_id: int, current_user_id: int):
    sp = await sp_repo.get_speaker(db, speaker_id)
    if sp is None or sp.user_id != current_user_id:
        raise HTTPException(404)
    return sp


@router.get("/speaker/{speaker_id}", response_class=HTMLResponse)
async def speaker_page(
    request: Request,
    speaker_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await _owned_speaker(db, speaker_id, current_user_id)
    sources = await ss_repo.list_sources_for_speaker(db, speaker_id)
    return templates.TemplateResponse(
        request, "speaker.html",
        {"speaker": speaker, "sources": sources, "avatars": avatars.AVATARS},
    )


@router.post("/speaker/{speaker_id}/edit", response_class=HTMLResponse)
async def edit_speaker(
    request: Request,
    speaker_id: int,
    name: str = Form(...),
    role: str = Form(""),
    avatar_id: str = Form(""),
    style_note: str = Form(""),
    photo: UploadFile | None = None,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
    current_user_id: int = Depends(get_current_user_id),
):
    await _owned_speaker(db, speaker_id, current_user_id)
    clean_avatar = avatar_id if avatars.is_valid_id(avatar_id) else None
    await sp_repo.update_fields(
        db, speaker_id, name=name.strip(), role=role.strip() or None,
        avatar_id=clean_avatar, style_note=style_note.strip() or None,
    )
    if photo is not None and photo.filename:
        dest_dir = Path(config.data_dir) / "speaker_photos"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{speaker_id}.jpg"
        dest.write_bytes(await photo.read())
        await sp_repo.set_photo_path(db, speaker_id, str(dest))
    # Plain browser submit → redirect back to the page; HTMX → re-render page.
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/speaker/{speaker_id}", status_code=303)


@router.get("/speaker/{speaker_id}/photo")
async def speaker_photo(
    speaker_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    speaker = await _owned_speaker(db, speaker_id, current_user_id)
    if not speaker.avatar_photo_path or not Path(speaker.avatar_photo_path).exists():
        raise HTTPException(404)
    return FileResponse(speaker.avatar_photo_path)
```

> `config.data_dir` is the `Config` attribute used throughout (`tests/conftest.py` builds `Config(data_dir=tmp_path)`). Writing photos under `data_dir/speaker_photos/` keeps them out of git and beside the other generated artifacts.

- [ ] **Step 6: Run to verify pass**

Run: `.venv/bin/pytest tests/test_routes_speakers.py -k "speaker_page or speaker_edit" -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/routes/speakers.py app/repos/speakers.py app/repos/source_speakers.py app/templates/speaker.html tests/test_routes_speakers.py
git commit -m "feat(speakers): speaker page (header + sources) + edit + photo serve"
```

---

### Task 7: Activate / deactivate (flip the flag only)

Activate flips `speakers.is_active = 1` via `repos/speakers.set_active` and returns the refreshed chip/actions fragment. Deactivate is the mirror. **The library-wide backfill JOB is a PR-4 follow-up** — PR 2 only flips the flag (note this in code).

**Files:**
- Modify: `app/routes/speakers.py` — add `activate` / `deactivate`.
- Create: `app/templates/_speaker_chip_panel.html` — the fragment returned to a chip's `hx-swap="outerHTML"`.
- Test: `tests/test_routes_speakers.py` (append)

**Interfaces:**
- Consumes: `repos/speakers.set_active`, `repos/speakers.get_speaker`.
- Produces: `POST /speaker/{id}/activate`, `POST /speaker/{id}/deactivate`.

- [ ] **Step 1: Write the chip-panel partial**

```html
{# app/templates/_speaker_chip_panel.html
   Returned by activate/deactivate. Re-renders a single chip in place
   (hx-swap="outerHTML" from the chip's activate button) so the chip
   reflects the flipped is_active state. When called from the speaker
   page's actions block, the same speaker is shown with the opposite
   toggle. #}
<span class="speaker-chip {% if speaker.is_active %}is-active{% else %}is-inactive{% endif %}"
      style="--avatar-bg: {{ speaker.avatar_id | avatar_bg }};">
  <a class="speaker-chip-name" href="/speaker/{{ speaker.id }}">{{ speaker.name }}</a>
  {% if speaker.is_active %}
    <button type="button" class="speaker-chip-deactivate"
            hx-post="/speaker/{{ speaker.id }}/deactivate"
            hx-target="closest .speaker-chip" hx-swap="outerHTML"
            title="Deactivate {{ speaker.name }}">✓</button>
  {% else %}
    <button type="button" class="speaker-chip-activate"
            hx-post="/speaker/{{ speaker.id }}/activate"
            hx-target="closest .speaker-chip" hx-swap="outerHTML"
            title="Activate {{ speaker.name }}">＋</button>
  {% endif %}
</span>
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_routes_speakers.py (append)
def test_activate_flips_flag(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        _run(_seed_video(app.state.db))
        client.post("/v/vc1/speakers", data={"name": "Activatable"})

        async def sid():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(app.state.db, name="Activatable")
        speaker_id = _run(sid())

        resp = client.post(f"/speaker/{speaker_id}/activate")
        assert resp.status_code == 200

        async def check(expected):
            from app.repos import speakers as sp_repo
            sp = await sp_repo.get_speaker(app.state.db, speaker_id)
            assert sp.is_active is expected
        _run(check(True))

        client.post(f"/speaker/{speaker_id}/deactivate")
        _run(check(False))


def test_activate_foreign_is_404(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client:
        async def make_foreign():
            from app.repos import speakers as sp_repo
            return await sp_repo.resolve_speaker(
                app.state.db, user_id=999, name="Foreign Activatable"
            )
        speaker_id = _run(make_foreign())
        resp = client.post(f"/speaker/{speaker_id}/activate")
        assert resp.status_code == 404
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_routes_speakers.py -k "activate" -v`
Expected: FAIL — routes absent.

- [ ] **Step 4: Implement the routes**

Append to `app/routes/speakers.py`:

```python
def _chip_panel(request: Request, speaker) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_speaker_chip_panel.html", {"speaker": speaker}
    )


@router.post("/speaker/{speaker_id}/activate", response_class=HTMLResponse)
async def activate_speaker(
    request: Request,
    speaker_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    await _owned_speaker(db, speaker_id, current_user_id)
    # PR 2 only flips the flag. Enqueuing the library-wide backfill job
    # is a PR-4 follow-up (services/speaker_backfill.py) — intentionally
    # NOT done here, so activation stays cheap and claim-free in PR 2.
    await sp_repo.set_active(db, speaker_id, True)
    speaker = await sp_repo.get_speaker(db, speaker_id)
    return _chip_panel(request, speaker)


@router.post("/speaker/{speaker_id}/deactivate", response_class=HTMLResponse)
async def deactivate_speaker(
    request: Request,
    speaker_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    await _owned_speaker(db, speaker_id, current_user_id)
    await sp_repo.set_active(db, speaker_id, False)
    speaker = await sp_repo.get_speaker(db, speaker_id)
    return _chip_panel(request, speaker)
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_routes_speakers.py -k "activate" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routes/speakers.py app/templates/_speaker_chip_panel.html tests/test_routes_speakers.py
git commit -m "feat(speakers): activate/deactivate (flip is_active; backfill is PR 4)"
```

---

### Task 8: Pasted-text add-tab → `kind='text'` library item

Extend the `/videos` handler with a pasted-text branch: a new optional form field `pasted_text` (+ optional `title`) creates a `kind='text'` videos row directly (no fetch — analogous to `_import_web` but the body is already present), then enqueues the normal pipeline job. Add an `_import_text(...)` helper and a "Paste text" tab in `_add_overlay.html`.

**Files:**
- Modify: `app/routes/videos.py` — make `url` optional, add the `pasted_text` branch + `_import_text` helper.
- Modify: `app/templates/_add_overlay.html` — add the "Paste text" tab.
- Test: `tests/test_videos_pasted_text.py`

**Interfaces:**
- Consumes: `repos/videos.upsert_metadata`, `repos/videos.set_transcript`, `repos/jobs.enqueue`, `app/models.VideoKind.TEXT`, `app/services/url_classify.web_id_from_url` (reused for id derivation via a synthetic key — see helper).
- Produces: pasted text becomes a `kind='text'` item that the pipeline summarizes with no fetch.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_videos_pasted_text.py
import asyncio

from fastapi.testclient import TestClient

from app.main import create_app


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_pasted_text_creates_text_item(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/videos",
            data={"pasted_text": "This is a transcribed interview body.",
                  "title": "My Interview"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        async def check():
            from app.models import VideoKind
            from app.repos import videos as videos_repo
            cur = await app.state.db.execute(
                "SELECT id FROM videos WHERE kind=? AND title=?",
                (VideoKind.TEXT.value, "My Interview"),
            )
            row = await cur.fetchone()
            assert row is not None
            v = await videos_repo.get(app.state.db, row["id"])
            assert v.kind == VideoKind.TEXT
            assert v.transcript == "This is a transcribed interview body."
            return row["id"]
        item_id = _run(check())

        async def has_job():
            from app.repos import jobs as jobs_repo
            job = await jobs_repo.latest_for_video(app.state.db, item_id)
            assert job is not None  # enqueued for the normal pipeline
        _run(has_job())


def test_pasted_text_requires_a_body(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Neither url nor pasted_text → friendly error, not a crash.
        resp = client.post("/videos", data={}, headers={"HX-Request": "true"})
        assert resp.status_code in (200, 400)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_videos_pasted_text.py -v`
Expected: FAIL — `url` is currently a required form field (422), and there is no text branch.

- [ ] **Step 3: Add the `_import_text` helper**

In `app/routes/videos.py`, add near `_import_web`:

```python
async def _import_text(
    raw_text: str,
    title: str,
    db: aiosqlite.Connection,
    config: Config,
    user_id: int,
) -> str:
    """Create a kind='text' library item from pasted text.

    Mechanically '_import_web without the fetch': the body is already
    in hand, so we store it as the transcript directly and enqueue the
    normal pipeline (summary + embedding + Pexels thumbnail). This is
    the 'transcribed interview that exists nowhere as a URL' path."""
    import hashlib
    from app.models import TranscriptSource

    digest = hashlib.sha1(raw_text.encode("utf-8")).hexdigest()[:16]
    base_id = f"text-{digest}"
    item_id = _composite_id(user_id, base_id)
    clean_title = title.strip() or "Pasted text"

    await videos_repo.upsert_metadata(
        db,
        video_id=item_id,
        url="",
        title=clean_title,
        description="",
        thumbnail_path=None,
        duration_seconds=None,
        kind=VideoKind.TEXT,
        user_id=user_id,
    )
    await videos_repo.set_transcript(
        db, item_id, raw_text, TranscriptSource.WEB,
    )
    await jobs_repo.enqueue(db, item_id)
    return item_id
```

> `TranscriptSource.WEB` is reused (no `TEXT` transcript-source member exists, and `kind` already records "this is pasted text"). The id is content-hashed so re-pasting the same text upserts the same row rather than duplicating. `url=""` is accepted by the `videos` schema (`_import_web` also relies on a real URL, but the column is plain `TEXT NOT NULL` with no format check; empty string satisfies it).

- [ ] **Step 4: Add the pasted-text branch to `POST /videos`**

Change the handler signature so `url` is optional and add the new fields:

```python
@router.post("/videos")
async def submit_video(
    request: Request,
    url: str = Form(""),
    pasted_text: str = Form(""),
    title: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
    current_user_id: int = Depends(get_current_user_id),
):
    # Pasted-text branch: no URL, body already in hand → kind='text'.
    if pasted_text.strip() and not url.strip():
        item_id = await _import_text(
            pasted_text, title, db, config, current_user_id
        )
        if request.headers.get("HX-Request"):
            video = await videos_repo.get(db, item_id)
            return templates.TemplateResponse(
                request, "video_card.html", {"video": video}
            )
        return RedirectResponse(f"/v/{item_id}", status_code=303)

    if not url.strip() and not pasted_text.strip():
        return _import_error_response(
            request,
            submitted_url="",
            error_title="Nothing to add",
            error_message="Paste a URL, a curl command, or some text.",
        )

    submitted = url
    # ... existing curl / url branching unchanged from here ...
```

> Keep the rest of the existing handler body exactly as it is below the new branch. The only edits are: (1) `url` default `""`, (2) two new `Form("")` params, (3) the pasted-text branch + the both-empty guard inserted at the top.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_videos_pasted_text.py -v`
Expected: PASS.

- [ ] **Step 6: Add the "Paste text" tab to the add overlay**

In `app/templates/_add_overlay.html`, replace the single `<form>` with a two-tab form (URL tab + Paste-text tab) using Alpine's existing `x-data`. Add a `tab` flag and a textarea that posts `pasted_text` + `title`:

```html
      <div x-data="{ open: false, tab: 'url' }" x-init="$watch('open', v => v || (tab='url'))">
        <div class="add-tabs">
          <button type="button" :class="tab==='url' && 'is-on'" x-on:click="tab='url'">Link / cURL</button>
          <button type="button" :class="tab==='text' && 'is-on'" x-on:click="tab='text'">Paste text</button>
        </div>

        <form method="post" action="/videos" class="add-overlay-form" x-show="tab==='url'">
          <textarea name="url" rows="2" x-ref="addinput"
                    placeholder="Paste a YouTube link, a playlist URL, a website, or a curl command"></textarea>
          <button type="submit" class="btn btn-accent">Summarize</button>
        </form>

        <form method="post" action="/videos" class="add-overlay-form" x-show="tab==='text'" x-cloak>
          <input name="title" placeholder="Title (optional)" autocomplete="off">
          <textarea name="pasted_text" rows="6" required
                    placeholder="Paste an interview transcript or any text to summarize…"></textarea>
          <p class="add-overlay-hint">No URL needed — the text is summarized as-is and added to your library.</p>
          <button type="submit" class="btn btn-accent">Summarize text</button>
        </form>
      </div>
```

> This is a template edit with no Python contract; the route tests above already prove the `pasted_text` path. Keep the surrounding modal markup (`add-overlay`, head, close button) intact — only the inner form region changes. The two-tab markup must live inside the existing `x-data="{ open: false }"` panel; merge the `tab` key into that single `x-data` rather than nesting a second component if the existing structure makes that cleaner.

- [ ] **Step 7: Run to verify pass + full suite**

Run: `.venv/bin/pytest tests/test_videos_pasted_text.py -q && .venv/bin/pytest -q`
Expected: PASS; full suite green.

- [ ] **Step 8: Commit**

```bash
git add app/routes/videos.py app/templates/_add_overlay.html tests/test_videos_pasted_text.py
git commit -m "feat(speakers): pasted-text add-tab → kind='text' library item"
```

---

### Task 9b: Pipeline handles `kind='text'` (Finding 4)

The spec says a `text` item flows through summary + embedding + Pexels "like web/email". Today `pipeline.py` doesn't know `text`: the WEB **fetch** branch is `== VideoKind.WEB` (so `text` correctly skips fetching — good), the summary `content_kind` falls into the `youtube` default (correct — pasted material wants the plain prompt, not the newsletter triage), but the **Pexels thumbnail** branch is `kind in (EMAIL, WEB)` and so **excludes** `text`. This task adds `text` there and pins the no-fetch + summary + embedding behaviour with a test.

**Files:**
- Modify: `app/pipeline.py:261` (the stock-thumbnail eligibility check).
- Test: `tests/test_pipeline_text_kind.py`

**Interfaces:**
- Consumes: `VideoKind.TEXT` (PR 1), the existing pipeline.
- Produces: `text` items get summarized (no fetch) + are thumbnail-eligible like web/email.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pipeline_text_kind.py
import asyncio
from unittest.mock import patch, AsyncMock
from app.models import VideoKind


def _run(c): return asyncio.get_event_loop().run_until_complete(c)


def test_text_kind_is_thumbnail_eligible():
    # The eligibility predicate must include TEXT (mirrors EMAIL/WEB).
    import app.pipeline as p
    # The predicate lives inline at pipeline.py:261; assert via a tiny helper
    # we extract in Step 3. After refactor, `p._wants_stock_thumbnail(kind)`:
    assert p._wants_stock_thumbnail(VideoKind.TEXT) is True
    assert p._wants_stock_thumbnail(VideoKind.WEB) is True
    assert p._wants_stock_thumbnail(VideoKind.EMAIL) is True
    assert p._wants_stock_thumbnail(VideoKind.YOUTUBE) is False


def test_text_kind_uses_standard_summary_prompt():
    # content_kind for TEXT must be the plain 'youtube' path, NOT 'email'.
    import app.pipeline as p
    assert p._content_kind_for(VideoKind.TEXT) == "youtube"
    assert p._content_kind_for(VideoKind.EMAIL) == "email"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_pipeline_text_kind.py -v`
Expected: FAIL — helpers don't exist yet.

- [ ] **Step 3: Extract the two predicates and include `text`**

In `app/pipeline.py`, add two tiny module-level helpers and use them at the existing sites:

```python
def _content_kind_for(kind: VideoKind) -> str:
    # Email gets the newsletter-tuned prompt; everything else (incl. TEXT and
    # WEB) uses the standard path.
    return "email" if kind == VideoKind.EMAIL else "youtube"


def _wants_stock_thumbnail(kind: VideoKind) -> bool:
    # Items with no native thumbnail source get a Pexels fallback.
    return kind in (VideoKind.EMAIL, VideoKind.WEB, VideoKind.TEXT)
```

Replace line ~229 `content_kind = "email" if video.kind == VideoKind.EMAIL else "youtube"` with `content_kind = _content_kind_for(video.kind)`, and line ~261's `if video.kind in (VideoKind.EMAIL, VideoKind.WEB) and not video.thumbnail_path:` with `if _wants_stock_thumbnail(video.kind) and not video.thumbnail_path:`.

> Note the WEB fetch branch (`pipeline.py:127`, `elif video.kind == VideoKind.WEB:`) is deliberately left untouched — `text` already skips it, which is exactly the "no fetch" behaviour the body needs (the pasted text is already in `transcript`).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_pipeline_text_kind.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/pipeline.py tests/test_pipeline_text_kind.py
git commit -m "feat(speakers): pipeline treats kind='text' like web/email (no fetch, thumbnail-eligible)"
```

---

## PR 2 done-criteria

- `.venv/bin/pytest -q` is fully green (new + existing).
- The pipeline's best-effort detection hook links detected speakers into `source_speakers` (`detection_source='show_rule'`) after summarization, gated to YouTube + transcript-present, and **never fails the job** (proven by `test_detect_and_link_swallows_errors` + the pipeline regression run).
- Speaker chips render at the top of the video chat section on page load and re-render after `/detect`, manual add, and unlink (one chip per detected speaker; name deep-links to `/speaker/{id}`).
- `POST /speaker/{id}/activate` flips `speakers.is_active` to 1 (and `/deactivate` back to 0); **no backfill job is enqueued** — that is the PR-4 follow-up.
- `GET /speaker/{id}` renders the header (name, role, avatar/photo, style_note, edit form, activate/deactivate toggle) + the confirmed-sources list; **no claims, no candidates** (extension points marked only).
- Pasting text into the new add-tab creates a `kind='text'` library item that the pipeline summarizes with **no fetch** (proven by `test_pasted_text_creates_text_item`).
- All speaker routes are ownership-checked → `HTTPException(404)` on a foreign profile.
- Existing chat tests (`tests/test_routes_chat.py`, `tests/test_services_chat.py`) pass **unchanged** — the chip strip and new routes do not touch the `scope='source'` chat path.
