# Latest-Video Thumbnail in Playlist Overviews — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each playlist's newest-video thumbnail (not the playlist's own image) on the `/playlists` overview and the home-page playlist cards, falling back to the playlist image, then a placeholder.

**Architecture:** Add one repo helper `latest_video_ids(db, playlist_ids)` returning `{playlist_id: newest_video_id}` in a single window-function query. Both routes (`home`, `list_playlists`) call it and pass the dict to their templates. Both templates resolve the image in precedence order: newest video → playlist image → `▣` placeholder.

**Tech Stack:** Python / FastAPI / aiosqlite / Jinja2 / pytest (async).

---

## Background the engineer needs

- **"Newest video"** = the row in `playlist_videos` with the greatest `added_at` for that playlist. The column defaults to `datetime('now')` — **1-second resolution** (`app/db.py:90`). Within a fast test, several links share the same `added_at`, so ordering **must** tie-break on `video_id DESC`. This matches the existing `videos_for_playlist` query (`app/repos/playlists.py:155`).
- Video thumbnails are already downloaded during sync and served at `GET /thumbnails/{video_id}.jpg` (`app/main.py:253`). **No fetching is added.**
- Repo tests use an injected `db` fixture (`tests/conftest.py`) and are plain `async def test_...(db)`. Helpers `_make_playlist` / `_make_video` already exist in `tests/test_repos_playlists.py`.
- Run a single test: `pytest tests/test_repos_playlists.py::test_name -v`
- Run the suite for a file: `pytest tests/test_repos_playlists.py -v`

## File structure

- **Modify** `app/repos/playlists.py` — add `latest_video_ids()` (new helper, sits near `playlists_for_videos`).
- **Modify** `tests/test_repos_playlists.py` — tests for the helper.
- **Modify** `app/routes/playlists.py` — `list_playlists()` passes `latest_video_ids` to the template.
- **Modify** `app/routes/home.py` — `home()` passes `latest_video_ids` to the template.
- **Modify** `app/templates/playlists.html` — image precedence in the `playlist-row-thumb` block.
- **Modify** `app/templates/playlist_card.html` — image precedence in the card.

---

## Task 1: Repo helper `latest_video_ids`

**Files:**
- Modify: `app/repos/playlists.py` (add helper after `playlists_for_videos`, ends at line 189)
- Test: `tests/test_repos_playlists.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repos_playlists.py`:

```python
async def test_latest_video_ids_returns_most_recent_per_playlist(
    db: aiosqlite.Connection,
):
    await _make_playlist(db, "p1")
    await _make_video(db, "v1")
    await _make_video(db, "v2")
    await playlists_repo.link_video(db, "p1", "v1")
    await playlists_repo.link_video(db, "p1", "v2")
    # Same-second added_at → tie-break video_id DESC → "v2" wins.
    result = await playlists_repo.latest_video_ids(db, ["p1"])
    assert result == {"p1": "v2"}


async def test_latest_video_ids_uses_added_at_over_tiebreak(
    db: aiosqlite.Connection,
):
    await _make_playlist(db, "p1")
    await _make_video(db, "v_aaa")
    await _make_video(db, "v_bbb")
    await playlists_repo.link_video(db, "p1", "v_bbb")
    await playlists_repo.link_video(db, "p1", "v_aaa")
    # Force v_aaa to be the newest by added_at even though its id sorts lower.
    await db.execute(
        "UPDATE playlist_videos SET added_at='2099-01-01 00:00:00' "
        "WHERE playlist_id='p1' AND video_id='v_aaa'"
    )
    await db.commit()
    result = await playlists_repo.latest_video_ids(db, ["p1"])
    assert result == {"p1": "v_aaa"}


async def test_latest_video_ids_omits_playlists_without_videos(
    db: aiosqlite.Connection,
):
    await _make_playlist(db, "p1")
    await _make_playlist(db, "p2")
    await _make_video(db, "v1")
    await playlists_repo.link_video(db, "p1", "v1")
    result = await playlists_repo.latest_video_ids(db, ["p1", "p2"])
    assert result == {"p1": "v1"}
    assert "p2" not in result


async def test_latest_video_ids_empty_input(db: aiosqlite.Connection):
    assert await playlists_repo.latest_video_ids(db, []) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_repos_playlists.py -k latest_video_ids -v`
Expected: FAIL — `AttributeError: module 'app.repos.playlists' has no attribute 'latest_video_ids'`

- [ ] **Step 3: Implement the helper**

Add to `app/repos/playlists.py` (after `playlists_for_videos`, at end of file):

```python
async def latest_video_ids(
    db: aiosqlite.Connection, playlist_ids: list[str]
) -> dict[str, str]:
    """Map each playlist id to the id of its most-recently-added video.

    "Newest" = greatest ``playlist_videos.added_at`` (tie-break
    ``video_id DESC``, matching ``videos_for_playlist``). Playlists with no
    linked videos are absent from the result. Single window-function query,
    no N+1.
    """
    if not playlist_ids:
        return {}
    placeholders = ",".join("?" * len(playlist_ids))
    cursor = await db.execute(
        f"""
        SELECT playlist_id, video_id FROM (
            SELECT
                playlist_id,
                video_id,
                ROW_NUMBER() OVER (
                    PARTITION BY playlist_id
                    ORDER BY added_at DESC, video_id DESC
                ) AS rn
            FROM playlist_videos
            WHERE playlist_id IN ({placeholders})
        )
        WHERE rn = 1
        """,
        tuple(playlist_ids),
    )
    rows = await cursor.fetchall()
    return {row["playlist_id"]: row["video_id"] for row in rows}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_repos_playlists.py -k latest_video_ids -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/repos/playlists.py tests/test_repos_playlists.py
git commit -m "feat(playlists): latest_video_ids repo helper"
```

---

## Task 2: Pass `latest_video_ids` from the `/playlists` route

**Files:**
- Modify: `app/routes/playlists.py` — `list_playlists()` (around lines 86–103)

- [ ] **Step 1: Read the current handler**

Run: `sed -n '86,103p' app/routes/playlists.py`
Confirm it ends by rendering `playlists.html` with context including `rows`.

- [ ] **Step 2: Add the helper call and template arg**

After the line `rows = await playlists_repo.list_with_stats(db, current_user_id)`, insert:

```python
    latest = await playlists_repo.latest_video_ids(
        db, [p.id for p, _ in rows]
    )
```

Then add `"latest_video_ids": latest,` to the dict passed to the template
response (the same context dict that already contains `"rows": rows`).

- [ ] **Step 3: Verify import / no syntax errors**

Run: `python -c "import app.routes.playlists"`
Expected: no output (no error).

- [ ] **Step 4: Commit**

```bash
git add app/routes/playlists.py
git commit -m "feat(playlists): supply latest_video_ids to /playlists view"
```

---

## Task 3: Pass `latest_video_ids` from the home route

**Files:**
- Modify: `app/routes/home.py` — `home()` (playlists loaded around lines 109–113, rendered around line 138)

- [ ] **Step 1: Read the current handler**

Run: `sed -n '105,145p' app/routes/home.py`
Confirm `playlists = playlists_plus_one[:HOME_PLAYLIST_LIMIT]` exists and the
template context dict contains `"playlists": playlists`.

- [ ] **Step 2: Add the helper call and template arg**

After the line `playlists = playlists_plus_one[:HOME_PLAYLIST_LIMIT]`, insert:

```python
    latest_video_ids = await playlists_repo.latest_video_ids(
        db, [p.id for p in playlists]
    )
```

Then add `"latest_video_ids": latest_video_ids,` to the template context dict
that already contains `"playlists": playlists`.

- [ ] **Step 3: Verify import / no syntax errors**

Run: `python -c "import app.routes.home"`
Expected: no output (no error).

- [ ] **Step 4: Commit**

```bash
git add app/routes/home.py
git commit -m "feat(home): supply latest_video_ids to home view"
```

---

## Task 4: Update `playlists.html` image precedence

**Files:**
- Modify: `app/templates/playlists.html` (lines 14–20, the `playlist-row-thumb` anchor)

- [ ] **Step 1: Replace the image block**

Replace lines 14–20 (the `<a class="playlist-row-thumb"> … </a>` inner block):

```html
        <a href="/p/{{ playlist.id }}" class="playlist-row-thumb" aria-label="{{ playlist.title }}">
          {% set latest = latest_video_ids.get(playlist.id) %}
          {% if latest %}
            <img src="/thumbnails/{{ latest }}.jpg" alt="">
          {% elif playlist.thumbnail_path %}
            <img src="/thumbnails/playlist_{{ playlist.id }}.jpg" alt="">
          {% else %}
            <div class="playlist-row-placeholder">▣</div>
          {% endif %}
        </a>
```

- [ ] **Step 2: Manual smoke test**

Start the app, open `/playlists`. A playlist with synced videos shows its
newest video's thumbnail; an empty playlist shows the playlist image or `▣`.
(How to run the app: see project README / `run` skill.)

- [ ] **Step 3: Commit**

```bash
git add app/templates/playlists.html
git commit -m "feat(playlists): show newest-video thumbnail on /playlists"
```

---

## Task 5: Update `playlist_card.html` image precedence

**Files:**
- Modify: `app/templates/playlist_card.html` (lines 3–7, the image/placeholder block)

- [ ] **Step 1: Replace the image block**

Replace lines 3–7:

```html
    {% set latest = latest_video_ids.get(playlist.id) %}
    {% if latest %}
      <img src="/thumbnails/{{ latest }}.jpg" alt="">
    {% elif playlist.thumbnail_path %}
      <img src="/thumbnails/playlist_{{ playlist.id }}.jpg" alt="">
    {% else %}
      <div class="playlist-card-placeholder">▣</div>
    {% endif %}
```

Note: `playlist_card.html` is `{% include %}`-d per iteration from
`home.html`; `latest_video_ids` is available from the parent template
context, so no extra wiring is needed.

- [ ] **Step 2: Manual smoke test**

Start the app, open `/` (home). Playlist cards with synced videos show the
newest video's thumbnail; otherwise the playlist image or `▣`.

- [ ] **Step 3: Commit**

```bash
git add app/templates/playlist_card.html
git commit -m "feat(home): show newest-video thumbnail on playlist cards"
```

---

## Task 6: Full regression run

- [ ] **Step 1: Run the repo + route test files**

Run: `pytest tests/test_repos_playlists.py tests/test_main.py -v`
Expected: all pass.

- [ ] **Step 2: Run the full suite**

Run: `pytest -q`
Expected: green (no regressions).

---

## Self-review notes (for the executor)

- **Spec coverage:** helper (Task 1) ✓; both routes wired (Tasks 2–3) ✓; both templates with newest→playlist→placeholder precedence (Tasks 4–5) ✓; tests for newest-by-added_at, tie-break, empty-playlist omission, empty-input (Task 1) ✓.
- **Type consistency:** helper name `latest_video_ids` and return shape `dict[str, str]` identical across repo, routes, and templates. Template key `latest_video_ids` identical in both routes and both templates.
- **Tie-break:** ordering `added_at DESC, video_id DESC` matches `videos_for_playlist`; required because `added_at` has 1-second resolution.
