# MCP Sync Export Implementation Plan

> **DONE — DO NOT EXECUTE.** This feature shipped to `main` on 2026-08-14 from a
> parallel plan written independently the same week
> ([`2026-08-13-mcp-sync-export.md`](2026-08-13-mcp-sync-export.md)), commits
> `cc71c47`..`57dfd8d`. Executing the tasks below would re-implement working code.
> Kept as a record. See
> [`../specs/2026-W33-export-since-design.md`](../specs/2026-W33-export-since-design.md)
> for the three points where this document and the shipped code deliberately differ.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** yt-summary exposes one generic MCP tool, `export_since`, that lets any MCP-speaking system incrementally sync summaries — cursor-paginated, ordered by `updated_at`, summaries and metadata only.

**Architecture:** Three thin additions following the codebase's existing layering: a new repo query (`list_updated_since`), a new pure renderer (`render_item_okf`), and a new MCP tool that wires them together. Field names follow the Open Knowledge Format (OKF v0.1) vocabulary so consumers need no translation table. Nothing existing changes behavior — one two-line addition to `render_item_md` closes an idempotency gap in the file export.

**Tech Stack:** Python 3.12+, FastAPI, aiosqlite, FastMCP (`mcp>=1.0`), pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `../../../mycelos/docs/superpowers/specs/2026-W33-yt-summary-sync-design.md` (Part A). The consumer side lives in the Mycelos repo and is a separate plan.

## Global Constraints

- **`updated_at`, not `created_at`.** Summaries are updated in place (resummarize, highlight extraction, related-links backfill) without a new row. The existing bulk export filters `created_at` (`app/routes/export.py:209-214`) and therefore never re-emits an updated summary. Sync must use `updated_at`.
- **Every query is scoped by `user_id`.** Multi-profile isolation is absolute — no "all users" default anywhere.
- **No transcripts in sync output.** Keeps MCP payloads paginatable and consumers' semantic search sharp. The transcript stays reachable via the source URL and the existing per-item export.
- **Cursor ordering is `(updated_at ASC, id ASC)`** — the `id` tiebreaker is required for correct resume when several items share a timestamp.
- `limit` is clamped server-side (max 100) regardless of what the caller passes — MCP payload safety.
- Layer discipline (established in this codebase): repo = raw parametrized SQL + `_row_to_video`; service = pure functions, no I/O; MCP tool = thin wrapper delegating to repo + service.
- Code, comments, and docstrings in English. Commit messages English, conventional style, NO Co-Authored-By / Generated-with footers.
- Tests: `pytest` with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed). Run with `python -m pytest <target> -v` from the repo root.

## Repo state note

At the time of writing, this repo is on branch `agent/harden-runtime-safety` with untracked files present (`.codex/`, `.localdata/`). Create a fresh branch off the intended base before starting, and do not commit those untracked directories.

## File structure

| File | Responsibility | Change |
|---|---|---|
| `app/repos/videos.py` | SQL for the `videos` table | Add `list_updated_since` |
| `app/services/export.py` | Pure render functions | Add `render_item_okf`; add `id` + `updated_at` to `render_item_md`'s frontmatter |
| `app/routes/mcp.py` | MCP tool surface | Add `_tool_export_since` + the `@mcp.tool()` wrapper |
| `tests/test_repos_videos_sync.py` | Repo query tests | Create |
| `tests/test_services_export.py` | Renderer tests | Extend |
| `tests/test_routes_mcp.py` | Tool tests | Extend |

---

### Task 1: `list_updated_since` repo query

**Files:**
- Modify: `app/repos/videos.py` (add after `list_recent`, ~line 358)
- Test: `tests/test_repos_videos_sync.py` (create)

**Interfaces:**
- Consumes: `_row_to_video(row)` (existing, `app/repos/videos.py:9-87`), the `videos` table.
- Produces: `list_updated_since(db, *, user_id: int, since: str | None, cursor: str | None, limit: int) -> list[Video]`. Tasks 3 consumes it. `cursor` format is `"<updated_at>|<id>"`; `since` is an ISO 8601 string or `None` for "from the beginning".

- [ ] **Step 1: Write the failing tests**

Create `tests/test_repos_videos_sync.py`. Read an existing repo test first (e.g. `tests/test_repos_videos.py`) and reuse its DB fixture and row-insertion helpers verbatim rather than inventing new ones — the fixture name and insert helper below must be replaced with whatever that file actually uses:

```python
"""list_updated_since — the incremental-sync query."""
from __future__ import annotations

from app.repos import videos as videos_repo


async def _insert(db, *, id: str, updated_at: str, user_id: int = 1,
                  title: str = "T", archived: bool = False) -> None:
    """Insert a minimal video row with a controlled updated_at."""
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, created_at, "
        "updated_at, archived_at) VALUES (?, ?, 'youtube', 'https://x', ?, "
        "?, ?, ?)",
        (id, user_id, title, updated_at, updated_at,
         "2026-01-01T00:00:00" if archived else None),
    )
    await db.commit()


async def test_returns_only_items_at_or_after_since(db) -> None:
    await _insert(db, id="1:a", updated_at="2026-08-01T10:00:00")
    await _insert(db, id="1:b", updated_at="2026-08-05T10:00:00")
    got = await videos_repo.list_updated_since(
        db, user_id=1, since="2026-08-03T00:00:00", cursor=None, limit=50)
    assert [v.id for v in got] == ["1:b"]


async def test_since_none_returns_everything_oldest_first(db) -> None:
    await _insert(db, id="1:b", updated_at="2026-08-05T10:00:00")
    await _insert(db, id="1:a", updated_at="2026-08-01T10:00:00")
    got = await videos_repo.list_updated_since(
        db, user_id=1, since=None, cursor=None, limit=50)
    assert [v.id for v in got] == ["1:a", "1:b"]


async def test_cursor_resumes_after_the_given_pair(db) -> None:
    await _insert(db, id="1:a", updated_at="2026-08-01T10:00:00")
    await _insert(db, id="1:b", updated_at="2026-08-02T10:00:00")
    await _insert(db, id="1:c", updated_at="2026-08-03T10:00:00")
    got = await videos_repo.list_updated_since(
        db, user_id=1, since=None,
        cursor="2026-08-01T10:00:00|1:a", limit=50)
    assert [v.id for v in got] == ["1:b", "1:c"]


async def test_cursor_disambiguates_items_sharing_a_timestamp(db) -> None:
    """The id tiebreaker: same updated_at must not re-emit or skip."""
    ts = "2026-08-01T10:00:00"
    await _insert(db, id="1:a", updated_at=ts)
    await _insert(db, id="1:b", updated_at=ts)
    await _insert(db, id="1:c", updated_at=ts)
    got = await videos_repo.list_updated_since(
        db, user_id=1, since=None, cursor=f"{ts}|1:a", limit=50)
    assert [v.id for v in got] == ["1:b", "1:c"]


async def test_limit_caps_the_page(db) -> None:
    for n in "abc":
        await _insert(db, id=f"1:{n}", updated_at=f"2026-08-0{ord(n)-96}T10:00:00")
    got = await videos_repo.list_updated_since(
        db, user_id=1, since=None, cursor=None, limit=2)
    assert len(got) == 2


async def test_other_users_items_are_never_returned(db) -> None:
    await _insert(db, id="1:a", updated_at="2026-08-01T10:00:00", user_id=1)
    await _insert(db, id="2:b", updated_at="2026-08-02T10:00:00", user_id=2)
    got = await videos_repo.list_updated_since(
        db, user_id=1, since=None, cursor=None, limit=50)
    assert [v.id for v in got] == ["1:a"]


async def test_archived_items_are_excluded(db) -> None:
    await _insert(db, id="1:a", updated_at="2026-08-01T10:00:00")
    await _insert(db, id="1:z", updated_at="2026-08-02T10:00:00", archived=True)
    got = await videos_repo.list_updated_since(
        db, user_id=1, since=None, cursor=None, limit=50)
    assert [v.id for v in got] == ["1:a"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_repos_videos_sync.py -v`
Expected: FAIL with `AttributeError: module 'app.repos.videos' has no attribute 'list_updated_since'`

- [ ] **Step 3: Implement**

In `app/repos/videos.py`, after `list_recent`:

```python
async def list_updated_since(
    db: aiosqlite.Connection,
    *,
    user_id: int = 1,
    since: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> list[Video]:
    """Items changed at or after `since`, oldest first — for incremental sync.

    Ordered by (updated_at ASC, id ASC) so a consumer can resume exactly
    where it stopped: `cursor` is the last seen "<updated_at>|<id>" pair
    and the next page starts strictly after it. The id tiebreaker matters
    because several items can share an updated_at.

    Deliberately keyed on updated_at, not created_at: summaries are
    updated in place (resummarize, highlights, related-links backfill)
    without a new row, so created_at would never re-emit them.
    """
    sql = [
        "SELECT * FROM videos WHERE user_id = ? AND archived_at IS NULL",
    ]
    params: list = [user_id]
    if since:
        sql.append("AND updated_at >= ?")
        params.append(since)
    if cursor:
        cur_ts, _, cur_id = cursor.partition("|")
        sql.append("AND (updated_at > ? OR (updated_at = ? AND id > ?))")
        params += [cur_ts, cur_ts, cur_id]
    sql.append("ORDER BY updated_at ASC, id ASC LIMIT ?")
    params.append(limit)

    db_cursor = await db.execute(" ".join(sql), tuple(params))
    rows = await db_cursor.fetchall()
    return [_row_to_video(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_repos_videos_sync.py -v`
Expected: 7/7 PASS

- [ ] **Step 5: Commit**

```bash
git add app/repos/videos.py tests/test_repos_videos_sync.py
git commit -m "feat(repos): list_updated_since for incremental sync"
```

---

### Task 2: `render_item_okf` + id/updated_at in markdown frontmatter

**Files:**
- Modify: `app/services/export.py` (add `render_item_okf` after `render_item_json`, ~line 168; extend `render_item_md`'s frontmatter, ~line 91-108)
- Test: `tests/test_services_export.py`

**Interfaces:**
- Consumes: `Video` (`app/models.py:40-86`), `rewrite_timestamp_links` (existing, same module).
- Produces: `render_item_okf(video: Video, *, tags: list[str], playlists: list[str], highlights: list[dict] | None = None) -> dict`. Task 3 consumes it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_services_export.py` (reuse the file's existing `Video`-building helper — read it first and replace `_video(...)` below with the real one):

```python
def test_okf_carries_the_stable_id_and_sync_timestamp() -> None:
    v = _video(id="1:abc", updated_at=datetime(2026, 8, 13, 9, 12))
    doc = export_svc.render_item_okf(v, tags=[], playlists=[])
    assert doc["id"] == "1:abc"
    assert doc["timestamp"].startswith("2026-08-13T09:12")


def test_okf_uses_okf_vocabulary() -> None:
    v = _video(title="Retrieval 101", url="https://youtu.be/abc")
    doc = export_svc.render_item_okf(v, tags=["ai"], playlists=[])
    assert doc["type"] == "note"
    assert doc["title"] == "Retrieval 101"
    assert doc["resource"] == "https://youtu.be/abc"
    assert doc["tags"] == ["ai"]


def test_okf_never_includes_a_transcript() -> None:
    v = _video(transcript="a very long transcript")
    doc = export_svc.render_item_okf(v, tags=[], playlists=[])
    assert "transcript" not in doc


def test_okf_description_is_the_first_summary_paragraph() -> None:
    v = _video(summary="# Heading\n\nFirst para here.\n\nSecond para.")
    doc = export_svc.render_item_okf(v, tags=[], playlists=[])
    assert doc["description"] == "First para here."


def test_okf_content_has_timestamp_links_rewritten() -> None:
    v = _video(summary="See [01:23](#t=83) for detail.", youtube_id="abc")
    doc = export_svc.render_item_okf(v, tags=[], playlists=[])
    assert "youtube.com/watch?v=abc&t=83s" in doc["content"]
    assert "(#t=83)" not in doc["content"]


def test_markdown_frontmatter_carries_id_and_updated() -> None:
    """Without the id in frontmatter an importer cannot dedupe re-exports."""
    v = _video(id="1:abc", updated_at=datetime(2026, 8, 13, 9, 12))
    md = export_svc.render_item_md(v, tags=[], playlists=[])
    assert "id: " in md
    assert "1:abc" in md.split("---")[1]
    assert "updated: " in md.split("---")[1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_services_export.py -v -k "okf or frontmatter_carries"`
Expected: FAIL — `render_item_okf` does not exist; the frontmatter assertion fails

- [ ] **Step 3: Implement**

In `app/services/export.py`, add a description helper and the renderer after `render_item_json`:

```python
def _first_paragraph(md: str) -> str:
    """First non-heading, non-empty paragraph, collapsed to one line."""
    for block in (md or "").split("\n\n"):
        line = block.strip()
        if not line or line.startswith("#"):
            continue
        return " ".join(p.strip() for p in line.splitlines() if p.strip())
    return ""


def render_item_okf(
    video: Video,
    *,
    tags: list[str],
    playlists: list[str],
    highlights: list[dict] | None = None,
) -> dict:
    """One sync item in Open Knowledge Format (OKF v0.1) vocabulary.

    Summary and metadata only — never the transcript, so a page of these
    stays small enough to travel over MCP. `timestamp` is updated_at,
    which is what a consumer uses both as the sync cursor and to decide
    whether its copy is stale.
    """
    doc: dict = {
        "id": video.id,
        "type": "note",
        "title": video.title,
        "resource": video.url,
        "timestamp": video.updated_at.isoformat(),
        "created": video.created_at.isoformat(),
        "kind": video.kind.value,
        "content": rewrite_timestamp_links(
            video.summary or "", youtube_id=video.youtube_id
        ),
    }
    description = _first_paragraph(video.summary or "")
    if description:
        doc["description"] = description
    if tags:
        doc["tags"] = list(tags)
    if playlists:
        doc["playlists"] = list(playlists)
    lang = video.summary_language or video.source_language
    if lang:
        doc["language"] = lang
    if video.summary_model:
        doc["summary_model"] = video.summary_model
    if video.duration_seconds is not None:
        doc["duration_seconds"] = video.duration_seconds
    if highlights:
        doc["highlights"] = [
            {"text": h.get("text", ""), "reason": h.get("reason", "")}
            for h in highlights
        ]
    return doc
```

In `render_item_md`, add two frontmatter lines right after the opening `["---"]` (before the `title:` line), so an exported file can be re-imported idempotently:

```python
    fm.append(f"id: {_yaml_quote(video.id)}")
    fm.append(f"updated: {video.updated_at.isoformat()}")
```

- [ ] **Step 4: Run tests to verify they pass, then the whole export suite**

Run: `python -m pytest tests/test_services_export.py tests/test_routes_export.py -v`
Expected: all PASS — existing export tests must stay green; if one asserts an exact frontmatter block, update it to include the two new lines (the addition is intentional).

- [ ] **Step 5: Commit**

```bash
git add app/services/export.py tests/test_services_export.py tests/test_routes_export.py
git commit -m "feat(export): OKF item renderer; id and updated in markdown frontmatter"
```

---

### Task 3: `export_since` MCP tool

**Files:**
- Modify: `app/routes/mcp.py` (add `_tool_export_since` next to the other `_tool_*` functions; register the `@mcp.tool()` wrapper inside `build_mcp_server`, before the closing `return mcp` at ~line 370)
- Test: `tests/test_routes_mcp.py`

**Interfaces:**
- Consumes: `list_updated_since` (Task 1), `render_item_okf` (Task 2), `tags_repo.tags_for_video`, `playlists_repo.playlists_for_videos` (existing helpers used by `_gather_item` in `app/routes/export.py:52-60`).
- Produces: MCP tool `export_since(since: str = "", cursor: str = "", limit: int = 50) -> dict` returning `{"items": list[dict], "next_cursor": str, "has_more": bool}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routes_mcp.py` (read the file's existing helper for calling `_tool_*` functions and its DB fixture, and mirror them — the `_insert` helper is the same shape as Task 1's):

```python
async def test_export_since_returns_okf_items_oldest_first(db) -> None:
    await _insert(db, id="1:b", updated_at="2026-08-05T10:00:00")
    await _insert(db, id="1:a", updated_at="2026-08-01T10:00:00")
    result = await mcp_routes._tool_export_since(db, since="", cursor="", limit=50)
    assert [i["id"] for i in result["items"]] == ["1:a", "1:b"]
    assert result["has_more"] is False
    assert all("transcript" not in i for i in result["items"])


async def test_export_since_paginates_with_next_cursor(db) -> None:
    for n, day in (("a", "01"), ("b", "02"), ("c", "03")):
        await _insert(db, id=f"1:{n}", updated_at=f"2026-08-{day}T10:00:00")

    first = await mcp_routes._tool_export_since(db, since="", cursor="", limit=2)
    assert [i["id"] for i in first["items"]] == ["1:a", "1:b"]
    assert first["has_more"] is True
    assert first["next_cursor"] == "2026-08-02T10:00:00|1:b"

    second = await mcp_routes._tool_export_since(
        db, since="", cursor=first["next_cursor"], limit=2)
    assert [i["id"] for i in second["items"]] == ["1:c"]
    assert second["has_more"] is False
    assert second["next_cursor"] == ""


async def test_export_since_clamps_limit(db) -> None:
    """A caller cannot demand an unbounded page."""
    for n in range(5):
        await _insert(db, id=f"1:{n}", updated_at=f"2026-08-0{n + 1}T10:00:00")
    result = await mcp_routes._tool_export_since(
        db, since="", cursor="", limit=100_000)
    assert len(result["items"]) <= 100


async def test_export_since_filters_by_since(db) -> None:
    await _insert(db, id="1:old", updated_at="2026-07-01T10:00:00")
    await _insert(db, id="1:new", updated_at="2026-08-10T10:00:00")
    result = await mcp_routes._tool_export_since(
        db, since="2026-08-01T00:00:00", cursor="", limit=50)
    assert [i["id"] for i in result["items"]] == ["1:new"]


async def test_export_since_empty_library_is_not_an_error(db) -> None:
    result = await mcp_routes._tool_export_since(db, since="", cursor="", limit=50)
    assert result == {"items": [], "next_cursor": "", "has_more": False}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_routes_mcp.py -v -k export_since`
Expected: FAIL — `_tool_export_since` does not exist

- [ ] **Step 3: Implement**

In `app/routes/mcp.py`, add the plain async function alongside the other `_tool_*` helpers:

```python
MAX_SYNC_PAGE = 100


async def _tool_export_since(
    db: aiosqlite.Connection,
    since: str = "",
    cursor: str = "",
    limit: int = 50,
    *,
    user_id: int = 1,
) -> dict[str, Any]:
    """One page of items changed since `since`, in OKF shape."""
    page_size = max(1, min(int(limit or 50), MAX_SYNC_PAGE))
    videos = await videos_repo.list_updated_since(
        db,
        user_id=user_id,
        since=since or None,
        cursor=cursor or None,
        limit=page_size + 1,       # one extra row answers has_more
    )
    has_more = len(videos) > page_size
    videos = videos[:page_size]

    items: list[dict[str, Any]] = []
    for video in videos:
        tags = await tags_repo.tags_for_video(db, video.id)
        pls = await playlists_repo.playlists_for_videos(db, [video.id])
        highlights = json.loads(video.highlights_json or "[]")
        items.append(
            export_svc.render_item_okf(
                video,
                tags=tags,
                playlists=[title for _, title in pls.get(video.id, [])],
                highlights=highlights,
            )
        )

    next_cursor = ""
    if has_more and videos:
        last = videos[-1]
        next_cursor = f"{last.updated_at.isoformat()}|{last.id}"
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
```

Add the imports this needs at the top of the module if absent (`json`, `videos_repo`, `tags_repo`, `playlists_repo`, `export_svc` — match the module's existing import style). If `highlights_json` can hold malformed JSON, wrap the `json.loads` in a try/except returning `[]` — check how other call sites in the codebase read that column and do the same thing.

Register the tool inside `build_mcp_server`, immediately before `return mcp`:

```python
    @mcp.tool()
    async def export_since(
        since: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Items created or updated since `since` (ISO 8601), for
        incremental sync into another knowledge system.

        Returns {items, next_cursor, has_more}. Each item carries Open
        Knowledge Format fields (id, type, title, description, resource,
        timestamp, tags, content) — summaries and metadata only, never
        transcripts. Pass an empty `since` for a full initial sync, then
        call again with the returned `next_cursor` until has_more is
        false. Use the newest `timestamp` you saw as the `since` of your
        next sync run.
        """
        return await _tool_export_since(
            app_state.db, since=since, cursor=cursor, limit=limit,
        )
```

- [ ] **Step 4: Run tests to verify they pass, then the MCP and export suites**

Run: `python -m pytest tests/test_routes_mcp.py tests/test_routes_mcp_host_check.py tests/test_services_export.py tests/test_routes_export.py tests/test_repos_videos_sync.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/routes/mcp.py tests/test_routes_mcp.py
git commit -m "feat(mcp): export_since tool for incremental knowledge sync"
```

---

### Task 4: Documentation + full-suite verification

**Files:**
- Modify: `README.md` (the MCP tools section — locate it first; if the repo documents tools elsewhere, e.g. `docs/`, put it there instead and say so in the commit message)

- [ ] **Step 1: Document the tool**

Add `export_since` to the documented MCP tool list, in the same style as the neighbouring entries:

```markdown
- `export_since(since, cursor, limit)` — incremental sync: items created or
  updated since an ISO 8601 timestamp, oldest first, cursor-paginated.
  Returns `{items, next_cursor, has_more}` with Open Knowledge Format
  fields (id, type, title, description, resource, timestamp, tags,
  content). Summaries and metadata only — no transcripts. Call repeatedly
  with `next_cursor` until `has_more` is false.
```

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q`
Expected: all PASS. If anything unrelated fails, check whether it also fails on the base commit before treating it as caused by this work.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document the export_since MCP tool"
```
