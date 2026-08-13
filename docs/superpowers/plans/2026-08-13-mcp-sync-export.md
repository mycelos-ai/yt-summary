# MCP Sync Export + Item Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every item leaving yt-summary a `(source, id, updated_at)` identity, and add one generic MCP tool `export_since` for incremental sync by any MCP consumer.

**Architecture:** All export output already routes through three pure renderers in `app/services/export.py`, so identity fields land in one module and propagate to single-item export, bulk ZIP, web UI and API. The sync surface is a thin stack: a new repo query (`list_updated_since`), a new pure renderer (`render_item_okf`), and a thin MCP wrapper (`export_since`) that delegates to both — matching how every existing tool delegates.

**Tech Stack:** Python 3.12, FastAPI, `aiosqlite` (raw parametrized SQL), FastMCP, pytest + pytest-asyncio.

**Spec:** [`docs/superpowers/specs/2026-08-13-mcp-sync-export-design.md`](../specs/2026-08-13-mcp-sync-export-design.md)

## Global Constraints

- **Every change is additive.** No existing field is renamed or removed. Existing tests assert on key presence, not dict equality.
- **`videos.id` keeps its value and, in MCP tools, its field name `video_id`.** Renaming breaks existing hosts.
- **Every repo query is scoped by `user_id`.** No exceptions.
- **Code, comments, docstrings, commit messages in English.** (Global CLAUDE.md rule.)
- **`source` is a module constant**, not an env var: `SOURCE = "yt-summary"` in `app/services/export.py`.
- **Services stay pure** — no DB, no network, no I/O in `app/services/export.py`.
- **Run tests with:** `python -m pytest <path> -q` from the repo root.
- **Baseline:** `python -m pytest tests/test_services_export.py -q` is green (12 passed) before Task 1.

## Critical: the `updated_at` string format

**Read this before Task 3.** SQLite stores timestamps via `datetime('now')` as:

```
'2026-08-13 17:37:05'     <- space separator, naive, UTC
```

But `datetime.isoformat()` renders:

```
'2026-08-13T17:37:05'     <- T separator
```

`' ' < 'T'` in string comparison. The cursor and the `since` bound are compared **as strings inside SQL**, against the raw column. If a cursor were built from `.isoformat()`, the comparison `updated_at > '...T...'` would skip every row at that same second — including the row the cursor points at. Silent data loss, no error.

**Therefore:**
- The **cursor** uses the raw DB string form (space separator). `list_updated_since` never calls `.isoformat()` to build or compare it.
- The `since` parameter is **normalized** from whatever the caller sends (`T` or space) to the space form before it reaches SQL.
- The outgoing **`timestamp`/`updated_at` payload fields** are ISO-8601 with a `Z` suffix, because the stored value is UTC but parsed naive (`tzinfo=None`). A consumer must not read it as local time.

Task 3 has a dedicated regression test for this.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `app/services/export.py` | Pure renderers; now also owns the `SOURCE` constant and OKF rendering | Modify + add `render_item_okf` |
| `app/repos/videos.py` | Raw SQL data access | Add `list_updated_since` |
| `app/routes/mcp.py` | Thin MCP wrappers | Add `source` to 3 tools; add `_tool_export_since` + `export_since` |
| `tests/test_services_export.py` | Pure renderer tests | Extend |
| `tests/test_repos_videos_sync.py` | Sync query tests | Create |
| `tests/test_routes_mcp.py` | MCP dispatch tests | Extend |

## Task Order

1. **Task 1** — `SOURCE` constant + identity in the three existing renderers (no new behavior, pure additive)
2. **Task 2** — `source` in the three existing MCP tools
3. **Task 3** — `list_updated_since` repo query (the cursor hazard lives here)
4. **Task 4** — `render_item_okf` pure renderer
5. **Task 5** — `export_since` MCP tool (consumes Tasks 3 + 4)

Tasks 1–2 are independently shippable and useful on their own. Tasks 3–5 build the sync surface.

---

### Task 1: Identity fields in the existing renderers

**Files:**
- Modify: `app/services/export.py` (add `SOURCE` constant near top; `render_item_md:91-108`; `render_item_json:149-164`; `build_export_zip:232-237`)
- Test: `tests/test_services_export.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `app.services.export.SOURCE: str == "yt-summary"` — used by Tasks 2, 4, 5. Markdown frontmatter keys `id`, `source`, `updated`. JSON keys `source`, `updated_at`. ZIP manifest key `source`.

**Context for the implementer:** `render_item_md` builds a list of frontmatter lines `fm` between `"---"` markers. `_yaml_quote` wraps a value as a double-quoted YAML scalar with escaping. Existing frontmatter has no identifier at all — the ID appears only in the filename. `video.updated_at` is a naive `datetime` holding UTC.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_services_export.py`, after `test_render_item_md_has_frontmatter_and_summary`:

```python
def test_render_item_md_frontmatter_carries_identity():
    from app.services.export import SOURCE, render_item_md
    v = _video()
    out = render_item_md(v, tags=[], playlists=[])
    # An exported note must say what it is and where it came from,
    # so a re-import can match it to the existing item.
    assert f'id: "{v.id}"' in out
    assert f'source: "{SOURCE}"' in out
    assert "updated: 2026-06-10T12:00:00Z" in out


def test_render_item_json_carries_identity():
    from app.services.export import SOURCE, render_item_json
    v = _video()
    doc = render_item_json(v, tags=[], playlists=[])
    assert doc["id"] == v.id
    assert doc["source"] == SOURCE
    assert doc["updated_at"] == "2026-06-10T12:00:00Z"
```

Add after `test_build_export_zip_md_has_manifest_and_one_file_per_item`:

```python
def test_build_export_zip_manifest_carries_source():
    import io
    import json
    import zipfile

    from app.services.export import SOURCE, build_export_zip
    items = [{"video": _video(), "tags": [], "playlists": []}]
    raw = build_export_zip(items, fmt="md")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest[0]["source"] == SOURCE
    assert manifest[0]["id"] == "1:abc12345678"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_services_export.py -q -k "identity or manifest_carries_source"`
Expected: FAIL — `ImportError: cannot import name 'SOURCE'`

- [ ] **Step 3: Add the SOURCE constant**

In `app/services/export.py`, directly after the `_TS_LINK_RE` definition (~line 27):

```python
# Provenance stamped onto every outgoing item. A consumer keys items by
# the pair (source, id): `id` is unique within this instance, `source`
# says which instance it came from. A module constant on purpose — if a
# second instance ever exists, this is the single line an
# YTS_INSTANCE_ID env var would replace.
SOURCE = "yt-summary"


def _utc_iso(value: datetime) -> str:
    """ISO-8601 with an explicit `Z`.

    Stored timestamps are UTC but parse back naive (tzinfo=None), so a
    consumer would otherwise be free to read them as local time.
    """
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
```

Add the import at the top of the file, after `from __future__ import annotations`:

```python
from datetime import datetime
```

- [ ] **Step 4: Add identity to the Markdown frontmatter**

In `render_item_md`, replace the opening frontmatter lines (currently
`fm: list[str] = ["---"]` followed by `fm.append(f"title: ...")`) so that
identity comes first:

```python
    fm: list[str] = ["---"]
    fm.append(f"id: {_yaml_quote(video.id)}")
    fm.append(f"source: {_yaml_quote(SOURCE)}")
    fm.append(f"title: {_yaml_quote(video.title)}")
```

Then, immediately after the existing `created:` line, add:

```python
    fm.append(f"updated: {_utc_iso(video.updated_at)}")
```

- [ ] **Step 5: Add identity to the JSON document**

In `render_item_json`, inside the `doc: dict = {...}` literal, add `"source"`
directly after the existing `"id"` entry, and `"updated_at"` directly after
the existing `"created_at"` entry:

```python
        "id": video.id,
        "source": SOURCE,
```

```python
        "created_at": video.created_at.isoformat(),
        "updated_at": _utc_iso(video.updated_at),
```

- [ ] **Step 6: Add source to the ZIP manifest**

In `build_export_zip`, in the `manifest.append({...})` call, add `"source"`
after `"id"`:

```python
            manifest.append({
                "id": video.id,
                "source": SOURCE,
                "title": video.title,
                "url": video.url,
                "file": fname,
            })
```

- [ ] **Step 7: Run the new tests**

Run: `python -m pytest tests/test_services_export.py -q -k "identity or manifest_carries_source"`
Expected: PASS (3 passed)

- [ ] **Step 8: Run the whole export suite for regressions**

Run: `python -m pytest tests/test_services_export.py tests/test_routes_export.py tests/test_routes_export_menu.py -q`
Expected: PASS, no failures. The changes are additive; existing assertions check key presence, not dict equality.

- [ ] **Step 9: Commit**

```bash
git add app/services/export.py tests/test_services_export.py
git commit -m "feat(export): stamp id, source and updated_at on exported items

Markdown frontmatter carried no identifier at all, so an exported note
could not be matched back to its item on re-import. JSON and the ZIP
manifest gain source and updated_at for the same reason."
```

---

### Task 2: `source` in the existing MCP tools

**Files:**
- Modify: `app/routes/mcp.py` (`_tool_submit_url:43-48`, `_tool_search:75-80`, `_tool_list_recent:127-135`)
- Test: `tests/test_routes_mcp.py`

**Interfaces:**
- Consumes: `app.services.export.SOURCE` from Task 1
- Produces: a `"source"` key on each item dict returned by `submit_url`, `search`, `list_recent`

**Context for the implementer:** These three tools each build a small item dict. The identifier field is named `video_id` here, **not** `id` — leave that name alone, existing hosts depend on it. Only add `source`. The tools under test are the plain `_tool_*` async functions; tests call them directly rather than going over the SSE wire.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes_mcp.py`, after `test_mcp_search`:

```python
async def test_mcp_search_items_carry_source(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_search
    from app.services.export import SOURCE
    hits = await _tool_search(db, query="MCP", limit=5)
    assert hits, "expected at least one hit to check"
    assert all(h["source"] == SOURCE for h in hits)
    # The id field keeps its existing name — renaming breaks hosts.
    assert all("video_id" in h for h in hits)


async def test_mcp_list_recent_items_carry_source(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_list_recent
    from app.services.export import SOURCE
    rows = await _tool_list_recent(db, limit=5)
    assert rows, "expected at least one row to check"
    assert all(r["source"] == SOURCE for r in rows)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_routes_mcp.py -q -k "carry_source"`
Expected: FAIL with `KeyError: 'source'`

- [ ] **Step 3: Add the import**

In `app/routes/mcp.py`, add to the imports at the top (after `from app.services import api as api_svc`):

```python
from app.services.export import SOURCE
```

- [ ] **Step 4: Add source to the three item dicts**

In `_tool_submit_url`, in the `out = {...}` literal, add after `"video_id"`:

```python
        "source": SOURCE,
```

In `_tool_search`, in the `out.append({...})` call, add after `"video_id"`:

```python
            "source": SOURCE,
```

In `_tool_list_recent`, in the returned dict comprehension, add after `"video_id"`:

```python
            "source": SOURCE,
```

- [ ] **Step 5: Run the new tests**

Run: `python -m pytest tests/test_routes_mcp.py -q -k "carry_source"`
Expected: PASS (2 passed)

- [ ] **Step 6: Run the whole MCP suite for regressions**

Run: `python -m pytest tests/test_routes_mcp.py tests/test_routes_mcp_host_check.py -q`
Expected: PASS, no failures.

- [ ] **Step 7: Commit**

```bash
git add app/routes/mcp.py tests/test_routes_mcp.py
git commit -m "feat(mcp): stamp source on items returned by submit_url, search, list_recent

A consumer that uses search today and syncs tomorrow must not see two
different identity models. The video_id field name is unchanged."
```

---

### Task 3: `list_updated_since` repo query

**Files:**
- Modify: `app/repos/videos.py` (add after `list_recent`, which ends ~line 357)
- Test: `tests/test_repos_videos_sync.py` (create)

**Interfaces:**
- Consumes: `_row_to_video` (existing, `app/repos/videos.py:9-87`)
- Produces:
  ```python
  async def list_updated_since(
      db: aiosqlite.Connection,
      *,
      user_id: int = 1,
      since: str | None = None,
      cursor: str | None = None,
      limit: int = 50,
  ) -> list[Video]
  ```
  and the module-level helpers `_normalize_ts(value: str | None) -> str | None`
  and `make_cursor(video: Video) -> str`. Task 5 calls `list_updated_since` and
  `make_cursor`.

**Context for the implementer:** Read the "Critical: the `updated_at` string format" section at the top of this plan before starting. It explains why this query must not use `.isoformat()`. `list_recent` (line ~336) is the neighbouring query to match for style: raw parametrized SQL, `user_id` scoping, `archived_at IS NULL`, `_row_to_video` mapping.

The cursor is the last-seen `"<updated_at>|<id>"` pair, using the **raw** DB string. Resuming means "strictly after this pair" in `(updated_at, id)` order, expressed as a tuple comparison:

```sql
(updated_at > ?) OR (updated_at = ? AND id > ?)
```

That second clause is what makes two items sharing an `updated_at` resume correctly instead of one being skipped or repeated forever.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_repos_videos_sync.py`:

```python
"""Tests for the incremental-sync query (repos/videos.list_updated_since).

Ordering, cursor resumption, profile scoping and archive exclusion.
The cursor is a raw "<updated_at>|<id>" pair; see the plan's note on
the space-vs-T timestamp hazard.
"""

import pytest

from app.repos import videos as videos_repo


async def _add(db, vid, *, ts, user_id=1, archived=False):
    """Insert one video and force its updated_at to a known value."""
    await videos_repo.upsert_metadata(
        db, video_id=vid, url=f"https://youtu.be/{vid}", title=vid,
        description="", thumbnail_path=None, duration_seconds=None,
        user_id=user_id,
    )
    await db.execute(
        "UPDATE videos SET updated_at=?, archived_at=? WHERE id=?",
        (ts, "2026-01-01 00:00:00" if archived else None, vid),
    )
    await db.commit()


async def test_orders_by_updated_at_then_id(db):
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:c", ts="2026-01-03 10:00:00")
    rows = await videos_repo.list_updated_since(db, user_id=1, limit=10)
    assert [v.id for v in rows] == ["1:a", "1:b", "1:c"]


async def test_since_is_inclusive_of_the_boundary(db):
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    rows = await videos_repo.list_updated_since(
        db, user_id=1, since="2026-01-02 10:00:00", limit=10,
    )
    assert [v.id for v in rows] == ["1:b"]


async def test_since_accepts_iso_t_separator(db):
    """A caller sending ISO-8601 with `T` must not silently lose rows.

    SQLite stores '2026-01-02 10:00:00' (space). Since ' ' < 'T',
    comparing a raw T-string in SQL would skip the boundary row.
    """
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    rows = await videos_repo.list_updated_since(
        db, user_id=1, since="2026-01-02T10:00:00Z", limit=10,
    )
    assert [v.id for v in rows] == ["1:b"]


async def test_cursor_resumes_exactly(db):
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    await _add(db, "1:c", ts="2026-01-03 10:00:00")
    first = await videos_repo.list_updated_since(db, user_id=1, limit=2)
    assert [v.id for v in first] == ["1:a", "1:b"]
    cursor = videos_repo.make_cursor(first[-1])
    rest = await videos_repo.list_updated_since(
        db, user_id=1, cursor=cursor, limit=2,
    )
    assert [v.id for v in rest] == ["1:c"]


async def test_cursor_handles_shared_updated_at(db):
    """Two items on the same timestamp must not be skipped or repeated."""
    same = "2026-01-01 10:00:00"
    await _add(db, "1:a", ts=same)
    await _add(db, "1:b", ts=same)
    await _add(db, "1:c", ts=same)
    first = await videos_repo.list_updated_since(db, user_id=1, limit=2)
    assert [v.id for v in first] == ["1:a", "1:b"]
    cursor = videos_repo.make_cursor(first[-1])
    rest = await videos_repo.list_updated_since(
        db, user_id=1, cursor=cursor, limit=2,
    )
    assert [v.id for v in rest] == ["1:c"]


async def test_scopes_to_the_requesting_profile(db):
    await _add(db, "1:mine", ts="2026-01-01 10:00:00", user_id=1)
    await _add(db, "2:theirs", ts="2026-01-02 10:00:00", user_id=2)
    rows = await videos_repo.list_updated_since(db, user_id=1, limit=10)
    assert [v.id for v in rows] == ["1:mine"]


async def test_excludes_archived_items(db):
    await _add(db, "1:live", ts="2026-01-01 10:00:00")
    await _add(db, "1:gone", ts="2026-01-02 10:00:00", archived=True)
    rows = await videos_repo.list_updated_since(db, user_id=1, limit=10)
    assert [v.id for v in rows] == ["1:live"]


async def test_limit_bounds_the_page(db):
    for i in range(5):
        await _add(db, f"1:v{i}", ts=f"2026-01-0{i + 1} 10:00:00")
    rows = await videos_repo.list_updated_since(db, user_id=1, limit=3)
    assert len(rows) == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_repos_videos_sync.py -q`
Expected: FAIL — `AttributeError: module 'app.repos.videos' has no attribute 'list_updated_since'`

- [ ] **Step 3: Implement the query**

In `app/repos/videos.py`, add directly after `list_recent` (which ends with
`return [_row_to_video(r) for r in rows]`, ~line 357):

```python
def _normalize_ts(value: str | None) -> str | None:
    """Coerce a caller-supplied timestamp to the stored string form.

    SQLite writes `datetime('now')` as '2026-08-13 17:37:05' — space
    separator, naive UTC. Callers send ISO-8601, often with a 'T' and a
    'Z'. These are compared as strings inside SQL, and ' ' < 'T', so an
    un-normalized 'T' bound silently skips every row on that second.
    Returns None for empty input, meaning "no lower bound".
    """
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
    return cleaned.replace("T", " ", 1)


def make_cursor(video: Video) -> str:
    """The opaque resume token for `list_updated_since`.

    Deliberately built from the raw stored form rather than
    `datetime.isoformat()` — see `_normalize_ts`.
    """
    stamp = video.updated_at.strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp}|{video.id}"


async def list_updated_since(
    db: aiosqlite.Connection,
    *,
    user_id: int = 1,
    since: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> list[Video]:
    """Active items changed at or after `since`, oldest change first.

    For incremental sync: ordered by (updated_at ASC, id ASC) so a
    cursor can resume exactly, including across items that share an
    `updated_at`. `cursor` is the last seen "<updated_at>|<id>" pair
    and wins over `since` when both are given.

    Filters `archived_at IS NULL`, matching `list_recent`: archiving an
    item removes it from the sync feed. An item archived *after* it
    synced is not signalled to the consumer — deletion propagation is
    out of scope.

    Unlike `list_recent`, this orders by `updated_at` rather than
    `created_at`: summaries are updated in place (resummarize,
    highlights, language backfill) without a new row, and a
    created_at-ordered feed would never re-emit them.
    """
    where = ["user_id = ?", "archived_at IS NULL"]
    params: list = [user_id]

    resume = _parse_cursor(cursor)
    if resume is not None:
        stamp, last_id = resume
        where.append("(updated_at > ? OR (updated_at = ? AND id > ?))")
        params += [stamp, stamp, last_id]
    else:
        bound = _normalize_ts(since)
        if bound is not None:
            where.append("updated_at >= ?")
            params.append(bound)

    params.append(limit)
    cur = await db.execute(
        "SELECT * FROM videos WHERE " + " AND ".join(where)
        + " ORDER BY updated_at ASC, id ASC LIMIT ?",
        tuple(params),
    )
    rows = await cur.fetchall()
    return [_row_to_video(r) for r in rows]
```

Add `_parse_cursor` directly above `make_cursor`:

```python
def _parse_cursor(cursor: str | None) -> tuple[str, str] | None:
    """Split "<updated_at>|<id>" into its parts.

    Returns None for anything unparseable, which the caller treats as
    "start from the beginning". A resync is cheap; a crashed sync loop
    is not.
    """
    if not cursor or "|" not in cursor:
        return None
    stamp, _, last_id = cursor.partition("|")
    stamp = _normalize_ts(stamp)
    if not stamp or not last_id:
        return None
    return stamp, last_id
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_repos_videos_sync.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Run the repo suite for regressions**

Run: `python -m pytest tests/ -q -k "repos_videos or services_export"`
Expected: PASS, no failures.

- [ ] **Step 6: Commit**

```bash
git add app/repos/videos.py tests/test_repos_videos_sync.py
git commit -m "feat(repos): add list_updated_since for incremental sync

Orders by (updated_at, id) so a cursor resumes exactly, including
across items sharing a timestamp. Cursor and since bound use the raw
stored string form: SQLite writes a space separator, ISO-8601 uses T,
and ' ' < 'T' would silently skip rows at the boundary second."
```

---

### Task 4: `render_item_okf` renderer

**Files:**
- Modify: `app/services/export.py` (add after `render_item_json`, which ends ~line 167)
- Test: `tests/test_services_export.py`

**Interfaces:**
- Consumes: `SOURCE`, `_utc_iso`, `rewrite_timestamp_links` (Task 1 + existing)
- Produces:
  ```python
  def render_item_okf(
      video: Video,
      *,
      tags: list[str],
      playlists: list[str],
      highlights: list[dict] | None = None,
  ) -> dict
  ```
  Task 5 calls this.

**Context for the implementer:** This is a pure function — no DB, no network, matching the rest of the module. It emits the OKF vocabulary (`type`, `title`, `description`, `resource`, `timestamp`, `tags`) so a consumer maps it without a translation table, plus the identity fields and yt-summary's own metadata.

`description` is the item's stored `video.description`, **not** a derived first paragraph of the summary. The spec deliberately left the derivation open; using the stored field is the simpler choice and needs no heuristic.

**There must be no `transcript` key.** The test asserts its absence so a later refactor cannot reintroduce it silently.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_services_export.py`, after the JSON-doc tests
(before the `# ----- bulk zip` section):

```python
# ------------------------------------------------------------ okf doc

def test_render_item_okf_maps_the_okf_vocabulary():
    from app.services.export import SOURCE, render_item_okf
    v = _video()
    doc = render_item_okf(
        v, tags=["ai"], playlists=["AI"],
        highlights=[{"text": "h", "reason": "r"}],
    )
    # OKF's own field names, so a consumer needs no translation table.
    assert doc["type"] == "note"
    assert doc["title"] == v.title
    assert doc["description"] == v.description
    assert doc["resource"] == v.url
    assert doc["timestamp"] == "2026-06-10T12:00:00Z"
    assert doc["tags"] == ["ai"]
    # Identity.
    assert doc["id"] == v.id
    assert doc["source"] == SOURCE
    # yt-summary metadata.
    assert doc["kind"] == "youtube"
    assert doc["language"] == "en"
    assert doc["summary_model"] == "anthropic/claude-sonnet-4-6"
    assert doc["playlists"] == ["AI"]
    assert doc["duration_seconds"] == 3841
    assert doc["highlights"] == [{"text": "h", "reason": "r"}]


def test_render_item_okf_never_includes_the_transcript():
    from app.services.export import render_item_okf
    v = _video()
    assert v.transcript, "fixture must have a transcript for this to mean anything"
    doc = render_item_okf(v, tags=[], playlists=[])
    assert "transcript" not in doc
    assert v.transcript not in str(doc)


def test_render_item_okf_content_has_rewritten_timestamp_links():
    from app.services.export import render_item_okf
    doc = render_item_okf(_video(), tags=[], playlists=[])
    # Same treatment as the Markdown export: in-app links are useless
    # outside the app.
    assert "(#t=94)" not in doc["content"]
    assert "youtube.com/watch?v=abc12345678&t=94s" in doc["content"]


def test_render_item_okf_tolerates_missing_optional_fields():
    from app.services.export import render_item_okf
    v = _video(
        summary=None, summary_model=None, duration_seconds=None,
        source_language=None, summary_language=None,
    )
    doc = render_item_okf(v, tags=[], playlists=[])
    assert doc["content"] == ""
    assert doc["summary_model"] is None
    assert doc["duration_seconds"] is None
    assert doc["language"] is None
    assert doc["highlights"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_services_export.py -q -k okf`
Expected: FAIL — `ImportError: cannot import name 'render_item_okf'`

- [ ] **Step 3: Implement the renderer**

In `app/services/export.py`, add after `render_item_json` (~line 167):

```python
def render_item_okf(
    video: Video,
    *,
    tags: list[str],
    playlists: list[str],
    highlights: list[dict] | None = None,
) -> dict:
    """One sync item: OKF vocabulary + summary body, no transcript.

    Field names follow OKF (`type`, `title`, `description`, `resource`,
    `timestamp`, `tags`) so a consumer maps them without a translation
    table. `timestamp` is `updated_at` — the same value the sync cursor
    orders by.

    Deliberately carries no transcript: it would bloat every MCP page
    and blunt semantic search on the consumer side. The transcript stays
    reachable through the `get_transcript` tool and the `resource` URL.
    """
    return {
        # Identity: a consumer keys on (source, id).
        "id": video.id,
        "source": SOURCE,
        # OKF vocabulary.
        "type": "note",
        "title": video.title,
        "description": video.description,
        "resource": video.url,
        "timestamp": _utc_iso(video.updated_at),
        "created": _utc_iso(video.created_at),
        "tags": list(tags),
        # yt-summary metadata.
        "kind": video.kind.value,
        "language": video.summary_language or video.source_language,
        "summary_model": video.summary_model,
        "playlists": list(playlists),
        "duration_seconds": video.duration_seconds,
        "highlights": highlights or [],
        "content": rewrite_timestamp_links(
            video.summary or "", youtube_id=video.youtube_id,
        ),
    }
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_services_export.py -q -k okf`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the full export suite**

Run: `python -m pytest tests/test_services_export.py -q`
Expected: PASS, no failures.

- [ ] **Step 6: Commit**

```bash
git add app/services/export.py tests/test_services_export.py
git commit -m "feat(export): add render_item_okf for sync payloads

OKF field names so a consumer maps items without a translation table.
No transcript key, asserted by test: it would bloat every MCP page and
blunt semantic search downstream."
```

---

### Task 5: `export_since` MCP tool

**Files:**
- Modify: `app/routes/mcp.py` (add `_tool_export_since` after `_tool_ask_library:190-213`; register `export_since` in `build_mcp_server` after the `ask_library` tool, ~line 368)
- Test: `tests/test_routes_mcp.py`

**Interfaces:**
- Consumes: `videos_repo.list_updated_since`, `videos_repo.make_cursor` (Task 3); `export_svc.render_item_okf` (Task 4)
- Produces:
  ```python
  async def _tool_export_since(
      db: aiosqlite.Connection,
      *,
      since: str = "",
      cursor: str = "",
      limit: int = 50,
      user_id: int = 1,
  ) -> dict[str, Any]
  ```
  returning `{"items": list[dict], "next_cursor": str, "has_more": bool}`

**Context for the implementer:** Thin wrapper, exactly like the other tools — fetch, render, return. The tags and playlists for each item come from the same repos the export route uses: `tags_repo.tags_for_video(db, video.id)` and `playlists_repo.playlists_for_videos(db, [ids])`, the latter returning a dict keyed by video id with `[(id, title), ...]` values. `render_item_okf` wants plain playlist **names**, so map to the title element.

Fetch `limit + 1` rows to decide `has_more` without a second count query, then trim.

`MAX_PAGE = 100` is a hard clamp: a caller asking for more gets a valid smaller page, never an error.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes_mcp.py`, at the end of the file:

```python
async def _seed_for_sync(db, count):
    """`count` active videos with distinct, ascending updated_at values."""
    from app.repos import videos as videos_repo
    for i in range(count):
        vid = f"1:sync{i}"
        await videos_repo.upsert_metadata(
            db, video_id=vid, url=f"https://youtu.be/{vid}",
            title=f"Sync {i}", description="d",
            thumbnail_path=None, duration_seconds=None,
        )
        await videos_repo.set_summary(db, vid, f"summary {i}", "model")
        await db.execute(
            "UPDATE videos SET updated_at=? WHERE id=?",
            (f"2026-01-0{i + 1} 10:00:00", vid),
        )
    await db.commit()


async def test_export_since_returns_okf_items(db):
    from app.routes.mcp import _tool_export_since
    from app.services.export import SOURCE
    await _seed_for_sync(db, 2)
    out = await _tool_export_since(db, limit=10)
    assert out["has_more"] is False
    assert len(out["items"]) == 2
    first = out["items"][0]
    assert first["type"] == "note"
    assert first["source"] == SOURCE
    assert "transcript" not in first


async def test_export_since_paginates_with_cursor(db):
    from app.routes.mcp import _tool_export_since
    await _seed_for_sync(db, 5)
    page1 = await _tool_export_since(db, limit=2)
    assert [i["id"] for i in page1["items"]] == ["1:sync0", "1:sync1"]
    assert page1["has_more"] is True
    assert page1["next_cursor"]

    page2 = await _tool_export_since(db, cursor=page1["next_cursor"], limit=2)
    assert [i["id"] for i in page2["items"]] == ["1:sync2", "1:sync3"]
    assert page2["has_more"] is True

    page3 = await _tool_export_since(db, cursor=page2["next_cursor"], limit=2)
    assert [i["id"] for i in page3["items"]] == ["1:sync4"]
    assert page3["has_more"] is False
    assert page3["next_cursor"] == ""


async def test_export_since_clamps_an_oversized_limit(db):
    from app.routes.mcp import MAX_PAGE, _tool_export_since
    await _seed_for_sync(db, 3)
    # An over-large request is served, not rejected.
    out = await _tool_export_since(db, limit=10_000)
    assert len(out["items"]) == 3
    assert MAX_PAGE == 100


async def test_export_since_honours_the_since_bound(db):
    from app.routes.mcp import _tool_export_since
    await _seed_for_sync(db, 3)
    out = await _tool_export_since(db, since="2026-01-02T10:00:00Z", limit=10)
    assert [i["id"] for i in out["items"]] == ["1:sync1", "1:sync2"]


async def test_export_since_empty_library_is_not_an_error(db):
    from app.routes.mcp import _tool_export_since
    out = await _tool_export_since(db, limit=10)
    assert out == {"items": [], "next_cursor": "", "has_more": False}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_routes_mcp.py -q -k export_since`
Expected: FAIL — `ImportError: cannot import name '_tool_export_since'`

- [ ] **Step 3: Implement the tool function**

In `app/routes/mcp.py`, add after `_tool_ask_library` (~line 213):

```python
# Hard ceiling on one sync page, regardless of what a caller asks for.
# MCP responses go through the model's context; an unbounded page is a
# denial-of-service on the host, not just a slow query.
MAX_PAGE = 100


async def _tool_export_since(
    db: aiosqlite.Connection,
    *,
    since: str = "",
    cursor: str = "",
    limit: int = 50,
    user_id: int = 1,
) -> dict[str, Any]:
    """One page of items changed at or after `since`, for incremental sync.

    Delegates to repos.videos.list_updated_since + services.export.
    render_item_okf. Fetches limit+1 rows so `has_more` needs no second
    query.
    """
    from app.repos import playlists as playlists_repo
    from app.repos import tags as tags_repo
    from app.repos import videos as videos_repo
    from app.services import export as export_svc

    page = max(1, min(limit, MAX_PAGE))
    rows = await videos_repo.list_updated_since(
        db, user_id=user_id,
        since=since or None, cursor=cursor or None,
        limit=page + 1,
    )
    has_more = len(rows) > page
    rows = rows[:page]

    items: list[dict[str, Any]] = []
    for v in rows:
        tags = await tags_repo.tags_for_video(db, v.id)
        pls = await playlists_repo.playlists_for_videos(db, [v.id])
        names = [title for _, title in pls.get(v.id, [])]
        items.append(
            export_svc.render_item_okf(v, tags=tags, playlists=names)
        )

    next_cursor = videos_repo.make_cursor(rows[-1]) if (rows and has_more) else ""
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }
```

- [ ] **Step 4: Register the MCP tool**

In `build_mcp_server`, after the `ask_library` tool definition and before
`return mcp` (~line 368):

```python
    @mcp.tool()
    async def export_since(
        since: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Items created or updated since `since` (ISO 8601), for
        incremental sync into another system.

        Returns {items, next_cursor, has_more}. Each item carries its
        `id` and `source` so a consumer can tell a re-export from a new
        item, and `timestamp` (the item's last change) for change
        detection.

        Summaries and metadata only — no transcripts; use
        ``get_transcript`` for those. Pass an empty `since` for a full
        first sync, then call repeatedly with the returned
        `next_cursor` until `has_more` is false.
        """
        return await _tool_export_since(
            app_state.db, since=since, cursor=cursor, limit=limit,
        )
```

- [ ] **Step 5: Run the new tests**

Run: `python -m pytest tests/test_routes_mcp.py -q -k export_since`
Expected: PASS (5 passed)

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS. Compare against the pre-change baseline — no new failures.

- [ ] **Step 7: Lint**

Run: `python -m ruff check app/ tests/`
Expected: clean. CI lint must pass (see commit `c7438eb`).

- [ ] **Step 8: Commit**

```bash
git add app/routes/mcp.py tests/test_routes_mcp.py
git commit -m "feat(mcp): add export_since for incremental library sync

One generic sync surface for any MCP consumer, rather than a
consumer-specific REST route. Pages are clamped to MAX_PAGE=100:
an unbounded page is a denial-of-service on the host's context."
```

---

## Verification

After Task 5, confirm the whole change set:

- [ ] `python -m pytest tests/ -q` — full suite green
- [ ] `python -m ruff check app/ tests/` — clean
- [ ] Manual smoke via Docker (the real run path — **not** local uvicorn, which
      collides on port 8000 with a separate container and writes a different
      `YTS_DATA_DIR`):

```bash
docker compose up -d --build
```

The `:latest` image is stale; `--build` is required to pick up local changes.
The app is then on `http://localhost:8200`, MCP at `/mcp/sse`.

- [ ] Export one item as Markdown from the UI and confirm the frontmatter now
      opens with `id:` and `source:` and carries `updated:`.

## Out of Scope

Stated here so a reviewer does not flag them as gaps:

- **Deletion / archive propagation.** An item archived after it synced stays
  as a note on the consumer side. Accepted for v1 per the spec.
- **The Mycelos ingest side** (OKF import mapper, `yt_summary` ingest source,
  scheduling). Separate repo, separate plan.
- **Renaming `video_id` to `id` in the MCP tools.** Would break existing hosts.
- **`YTS_INSTANCE_ID` env var.** One instance today; `SOURCE` is the single
  line that would change.
