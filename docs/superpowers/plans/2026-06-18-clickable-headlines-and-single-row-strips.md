# Clickable Headlines, Single-Row Strips & Library Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the home page's three section headlines (Queues & playlists, Digests, Library) clickable to listing pages, cap the Queues and Digests strips to a single row (8 cells desktop / 4 mobile, last cell = "+" tile), and add a dedicated `/library` page.

**Architecture:** Pure FastAPI + Jinja2 + plain CSS change. Headlines become anchor links. Strip row-capping is done with a CSS grid change (fixed columns, no wrapping) plus a server-side card limit raised to 7; mobile trimming hides cards past the 3rd via `nth-child`. The new `/library` route lives in `app/routes/home.py` and reuses the existing video-card and load-more machinery.

**Tech Stack:** FastAPI, Jinja2, aiosqlite, pytest + pytest-asyncio, FastAPI `TestClient`.

## Global Constraints

- Mobile breakpoint for the strip rules: `max-width: 768px` (matches the existing strip/responsive section in `app/static/app.css`; do NOT use 640px here).
- The "+" add tile must always be the last cell in each strip and must survive the mobile trim. Add tiles are NOT wrapped in `.playlist-card-wrap` / `.digest-card-wrap`; real cards always are. Trim selectors therefore target the wrappers, never the add tiles.
- `/library` reuses `HOME_VIDEO_PAGE_SIZE = 25` and the existing `GET /videos/load-more` fragment route unchanged.
- Run tests with `python -m pytest` from the repo root. Tests set `YTS_DATA_DIR` to a tmp path and use `create_app()` + `TestClient`.
- Keep the existing `All questions →` link under the strips. Remove only the `More →` (playlists) and `All digests →` links, since their headlines now link to the same targets.

---

### Task 1: Raise home strip limits to 7 and update the affected route tests

Raise the playlist and digest counts the home route sends so each strip can show 7 real cards + the add tile = 8 cells. Three existing tests assert the old cap of 5 / the "More" link behavior and must be updated in the same task (they'd otherwise fail).

**Files:**
- Modify: `app/routes/home.py` (the `HOME_PLAYLIST_LIMIT` constant and the `recent_digests` limit in the `home` handler)
- Test: `tests/test_routes_home.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: home route renders up to 7 playlist cards (`.playlist-card-wrap`) and up to 7 digest cards (`.digest-card-wrap`).

- [ ] **Step 1: Update the playlist-cap test to expect 7**

In `tests/test_routes_home.py`, replace the body of `test_home_caps_playlists_at_five` (rename to `test_home_caps_playlists_at_seven`). It seeds more than 7 playlists and expects exactly 7 cards:

```python
def test_home_caps_playlists_at_seven(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import playlists as playlists_repo
            for i in range(9):
                await playlists_repo.create(
                    app.state.db, playlist_id=f"PL{i}", user_id=1, url="u",
                    title=f"Playlist {i}", description="",
                    thumbnail_path=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    # Exactly 7 playlist cards rendered (excluding the add card)
    assert resp.text.count('class="playlist-card-wrap"') == 7
    # The add-playlist tile is still visible
    assert 'class="playlist-card playlist-card-add"' in resp.text
```

- [ ] **Step 2: Rewrite the two "More link" tests to not depend on `href="/playlists"`**

Because Task 2 makes the **headline** link to `/playlists`, the bare `href="/playlists"` assertion is no longer a unique signal for the (now-removed) "More →" link. Replace both `test_home_shows_more_link_when_over_five_playlists` and `test_home_no_more_link_when_five_or_fewer_playlists` with a single test that just confirms the headline link is present (the dedicated "More" link is removed in Task 2):

```python
def test_home_playlists_headline_links_to_playlists_page(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLx", user_id=1, url="u",
                title="Playlist X", description="", thumbnail_path=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    # The clickable headline links to the full playlists page.
    assert 'href="/playlists"' in resp.text
    assert "Queues" in resp.text
```

Delete the old `test_home_shows_more_link_when_over_five_playlists` and `test_home_no_more_link_when_five_or_fewer_playlists` functions entirely.

- [ ] **Step 3: Run the updated tests to verify they FAIL**

Run: `python -m pytest tests/test_routes_home.py::test_home_caps_playlists_at_seven tests/test_routes_home.py::test_home_playlists_headline_links_to_playlists_page -v`
Expected: `test_home_caps_playlists_at_seven` FAILS (currently caps at 5, so only 5 wrappers render). The headline test may already pass via the old "More" link or fail — either is fine at this step; Task 2 makes it pass deterministically.

- [ ] **Step 4: Raise the limits in the home route**

In `app/routes/home.py`, change the module constant:

```python
HOME_PLAYLIST_LIMIT = 7
```

And in the `home` handler, change the digests fetch from `limit=4` to `limit=7`:

```python
    recent_digests = await digests_repo.list_for_user(
        db, user_id=current_user_id, limit=7,
    )
```

- [ ] **Step 5: Run the cap test to verify it PASSES**

Run: `python -m pytest tests/test_routes_home.py::test_home_caps_playlists_at_seven -v`
Expected: PASS (7 wrappers rendered).

- [ ] **Step 6: Commit**

```bash
git add app/routes/home.py tests/test_routes_home.py
git commit -m "feat(home): show up to 7 playlist and digest cards per strip"
```

---

### Task 2: Make the three section headlines clickable and remove redundant strip links

Convert the `Queues & playlists`, `Digests`, and `Library` headlines into links. Remove the now-redundant `More →` (playlists) and `All digests →` links. Update the digest test that asserted on the archive link.

**Files:**
- Modify: `app/templates/home.html` (three `.section-title` headlines + remove two `.playlist-strip-more` blocks)
- Modify: `app/static/app.css` (add `.section-title-link` style)
- Test: `tests/test_routes_home.py`

**Interfaces:**
- Consumes: the `/playlists`, `/digest`, and `/library` routes (the last is created in Task 4; the link is harmless before then — clicking 404s until Task 4 lands, but the home page renders fine).
- Produces: home page contains `href="/playlists"`, `href="/digest"`, and `href="/library"` headline anchors.

- [ ] **Step 1: Update the digest archive-link test**

In `tests/test_routes_home.py`, `test_home_shows_recent_digest_cards` asserts `'href="/digest"' in resp.text` for the "All digests →" link. That standalone link is being removed, but the **Digests headline** now carries `href="/digest"`, so the assertion still holds. Change only the comment to reflect the new source, leaving the assertion:

```python
    # The Digests headline links to the archive.
    assert 'href="/digest"' in resp.text
```

Add a focused test for all three headline links:

```python
def test_home_section_headlines_are_clickable(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/playlists"' in resp.text
    assert 'href="/digest"' in resp.text
    assert 'href="/library"' in resp.text
```

- [ ] **Step 2: Run the new headline test to verify it FAILS**

Run: `python -m pytest tests/test_routes_home.py::test_home_section_headlines_are_clickable -v`
Expected: FAIL — `href="/library"` is not present yet (no Library headline link).

- [ ] **Step 3: Convert the headlines in `home.html`**

In `app/templates/home.html`:

Replace the Queues headline (currently `<p class="section-title">Queues &amp; playlists</p>`):

```html
  <p class="section-title">
    <a href="/playlists" class="section-title-link">Queues &amp; playlists</a>
  </p>
```

Replace the Digests headline (currently `<p class="section-title">Digests</p>`):

```html
<p class="section-title">
  <a href="/digest" class="section-title-link">Digests</a>
</p>
```

Replace the Library headline (currently `<p class="section-title">Library</p>`):

```html
<p class="section-title">
  <a href="/library" class="section-title-link">Library</a>
</p>
```

- [ ] **Step 4: Remove the two redundant strip links in `home.html`**

Delete the playlists "More →" block:

```html
  {% if has_more_playlists %}
    <p class="playlist-strip-more">
      <a href="/playlists">More →</a>
    </p>
  {% endif %}
```

Delete the digests "All digests →" block:

```html
{% if recent_digests %}
  <p class="playlist-strip-more">
    <a href="/digest">All digests →</a>
  </p>
{% endif %}
```

Leave the `All questions →` block intact:

```html
<p class="playlist-strip-more">
  <a href="/ask">All questions →</a>
</p>
```

- [ ] **Step 5: Add the `.section-title-link` style**

In `app/static/app.css`, immediately after the `.section-title { ... }` rule (ends at the line with `margin-bottom: 12px; }` around line 1357), add:

```css
.section-title-link {
  color: inherit;
  text-decoration: none;
}
.section-title-link::after {
  content: " →";
  color: var(--muted);
  transition: color 150ms ease;
}
.section-title-link:hover {
  color: var(--ink);
  text-decoration: none;
}
.section-title-link:hover::after { color: var(--ink); }
```

- [ ] **Step 6: Run the headline + digest tests to verify they PASS**

Run: `python -m pytest tests/test_routes_home.py::test_home_section_headlines_are_clickable tests/test_routes_home.py::test_home_shows_recent_digest_cards -v`
Expected: both PASS.

- [ ] **Step 7: Commit**

```bash
git add app/templates/home.html app/static/app.css tests/test_routes_home.py
git commit -m "feat(home): clickable section headlines; drop redundant strip links"
```

---

### Task 3: Cap the strips to a single row (8 desktop / 4 mobile)

Change the strip grid from a wrapping `auto-fill` layout to a fixed single-row grid: 8 columns on desktop, 4 on mobile, hiding real cards past the 3rd on mobile so the row reads 3 cards + add tile.

**Files:**
- Modify: `app/static/app.css` (the `.playlist-strip` rule and the `@media (max-width: 768px)` block)

**Interfaces:**
- Consumes: home strips now render up to 7 real cards (Task 1) + 1 add tile.
- Produces: a CSS-only single-row layout. No template or route change.

This task is CSS-only and is verified visually in Task 5 (preview), so it has no unit-test step — there is no DOM-count assertion that can detect CSS column/visibility changes via `TestClient`.

- [ ] **Step 1: Make the desktop strip a fixed 8-column single row**

In `app/static/app.css`, replace the `.playlist-strip` rule (currently `grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));`) with:

```css
.playlist-strip {
  display: grid;
  grid-template-columns: repeat(8, 1fr);
  gap: 16px;
  margin-bottom: 32px;
}
```

- [ ] **Step 2: Add the mobile rules to the 768px breakpoint**

Inside the existing `@media (max-width: 768px) { ... }` block in `app/static/app.css`, add these rules (place them before the closing `}` of that block):

```css
  .playlist-strip { grid-template-columns: repeat(4, 1fr); }
  /* Show at most 3 real cards on mobile so the row reads 3 cards + the
     "+" add tile = 4 cells. Add tiles aren't wrapped in
     .playlist-card-wrap / .digest-card-wrap, so this never hides them. */
  .playlist-strip .playlist-card-wrap:nth-of-type(n + 4) { display: none; }
```

Note: `:nth-of-type` counts among siblings of the same element type (`div.playlist-card-wrap`); both playlist and digest real cards use a `div` wrapper with the `playlist-card-wrap` class, and the add tiles are an `<a>` (playlists) and a `<form>` (digests), so they are excluded from the count and never hidden.

- [ ] **Step 3: Commit**

```bash
git add app/static/app.css
git commit -m "feat(home): cap strips to a single row (8 desktop / 4 mobile)"
```

---

### Task 4: Add the `/library` route and template

Create a dedicated full-library page that mirrors the home Library block (header, tag-filter banner, video-card grid, load-more) and is reachable from the Library headline.

**Files:**
- Modify: `app/routes/home.py` (add a `GET /library` handler)
- Create: `app/templates/library.html`
- Test: `tests/test_routes_home.py`

**Interfaces:**
- Consumes: `videos_repo.list_recent(db, limit, tag, user_id[, offset])`, `playlists_repo.playlists_for_videos`, `tags_repo.tags_for_videos`, `HOME_VIDEO_PAGE_SIZE`, and the existing `GET /videos/load-more` fragment (same `_video_load_more.html`).
- Produces: `GET /library` returning HTML with `id="video-list"` and video cards; honors `?tag=`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes_home.py`:

```python
def test_library_page_lists_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="lib1", url="u",
                title="LibraryVideo", description="d",
                thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/library")
    assert resp.status_code == 200
    assert "LibraryVideo" in resp.text
    assert 'id="video-list"' in resp.text


def test_library_page_filters_by_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            from app.repos import tags as tags_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="lpy", url="u", title="LibPython",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="lcook", url="u", title="LibCooking",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await tags_repo.set_tags_for_video(app.state.db, "lpy", ["python"])
            await tags_repo.set_tags_for_video(app.state.db, "lcook", ["cooking"])
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/library?tag=python")
    assert resp.status_code == 200
    assert "LibPython" in resp.text
    assert "LibCooking" not in resp.text
    assert "filter-banner" in resp.text


def test_library_page_paginates_over_25(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            for i in range(26):
                await videos_repo.upsert_metadata(
                    app.state.db, video_id=f"lv{i:04d}", url="u",
                    title=f"LVideo {i}", description="d",
                    thumbnail_path=None, duration_seconds=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/library")
    assert resp.status_code == 200
    assert resp.text.count('id="video-v') == 25
    assert "Load more" in resp.text
    assert "/videos/load-more?offset=25" in resp.text
```

- [ ] **Step 2: Run the new tests to verify they FAIL**

Run: `python -m pytest tests/test_routes_home.py::test_library_page_lists_videos tests/test_routes_home.py::test_library_page_filters_by_tag tests/test_routes_home.py::test_library_page_paginates_over_25 -v`
Expected: FAIL with 404 (no `/library` route) — assertion errors on status_code == 200.

- [ ] **Step 3: Add the `/library` route handler**

In `app/routes/home.py`, add this handler after the `home` function (before `load_more_videos`). It reuses the exact same fetch logic as the non-search home branch:

```python
@router.get("/library", response_class=HTMLResponse)
async def library(
    request: Request,
    tag: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
    current_user=Depends(get_current_user),
):
    """Full library listing — the page behind the home 'Library'
    headline. Mirrors the home Library block (tag filter + video cards
    + load-more) without the hero or the Queues/Digests strips."""
    tag = tag.strip() if tag else None
    # +1 over-fetch so we know whether to show the load-more button,
    # mirroring the home route.
    rows = await videos_repo.list_recent(
        db, limit=HOME_VIDEO_PAGE_SIZE + 1, tag=tag,
        user_id=current_user_id,
    )
    has_more_videos = len(rows) > HOME_VIDEO_PAGE_SIZE
    videos = rows[:HOME_VIDEO_PAGE_SIZE]

    video_ids = [v.id for v in videos]
    playlist_links = await playlists_repo.playlists_for_videos(db, video_ids)
    video_tags = await tags_repo.tags_for_videos(db, video_ids)

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "videos": videos,
            "active_tag": tag,
            "playlist_links": playlist_links,
            "video_tags": video_tags,
            "has_more_videos": has_more_videos,
            "video_page_size": HOME_VIDEO_PAGE_SIZE,
            "current_user": current_user,
        },
    )
```

- [ ] **Step 4: Create `app/templates/library.html`**

Create `app/templates/library.html`. The load-more button must point at `/videos/load-more` (the shared fragment route), and the filter banner's clear link goes back to `/library` (not `/`):

```html
{% extends "base.html" %}
{% block title %}Library — yt-summary{% endblock %}
{% block content %}
<h1>Library</h1>

{% if active_tag %}
  <div class="filter-banner">
    <span>{{ icon('tag') }} <strong>{{ active_tag }}</strong> — showing {{ videos|length }} video{{ '' if videos|length == 1 else 's' }}</span>
    <a href="/library" class="clear">✕ Clear filter</a>
  </div>
{% endif %}

<section id="video-list">
  {% for video in videos %}
    {% include "video_card.html" %}
  {% else %}
    {% if active_tag %}
      <p class="empty">No videos with tag "{{ active_tag }}".</p>
    {% else %}
      <p class="empty">No videos yet — use the + Add button to get started.</p>
    {% endif %}
  {% endfor %}
</section>
{% if has_more_videos %}
  <div class="load-more-wrap">
    <button class="btn btn-secondary load-more-btn"
            hx-get="/videos/load-more?offset={{ video_page_size }}{% if active_tag %}&tag={{ active_tag|urlencode }}{% endif %}"
            hx-target="this"
            hx-swap="outerHTML"
            hx-disabled-elt="this">
      Load more
    </button>
  </div>
{% endif %}

<style>
  .load-more-wrap { text-align: center; margin: 24px 0 8px; }
  .load-more-btn {
    display: inline-block;
    padding: 10px 20px;
    border: 1px solid var(--hairline);
    border-radius: var(--rounded-md);
    background: var(--canvas);
    color: var(--ink);
    font-size: 14px;
    font-weight: 500;
  }
  .load-more-btn:hover { background: var(--surface); }
  .load-more-btn:disabled { opacity: 0.6; cursor: wait; }
</style>
{% endblock %}
```

Note: the `video_card.html` partial's tag pills link back to `/?tag=...` (home). That is acceptable — clicking a tag pill from `/library` filters via the home page, same as everywhere else. Do not change `video_card.html`.

- [ ] **Step 5: Run the new tests to verify they PASS**

Run: `python -m pytest tests/test_routes_home.py::test_library_page_lists_videos tests/test_routes_home.py::test_library_page_filters_by_tag tests/test_routes_home.py::test_library_page_paginates_over_25 -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routes/home.py app/templates/library.html tests/test_routes_home.py
git commit -m "feat(library): dedicated /library page behind the Library headline"
```

---

### Task 5: Full-suite check and visual verification

Confirm the whole change is green and verify the visual behavior in the preview.

**Files:** none (verification only).

- [ ] **Step 1: Run the full home-route test file**

Run: `python -m pytest tests/test_routes_home.py -v`
Expected: all PASS, including the renamed/rewritten tests from Tasks 1–2 and the new Library tests from Task 4. No remaining references to `test_home_caps_playlists_at_five`, `test_home_shows_more_link_when_over_five_playlists`, or `test_home_no_more_link_when_five_or_fewer_playlists`.

- [ ] **Step 2: Run the broader route + playlist + digest suites for regressions**

Run: `python -m pytest tests/test_routes_home.py tests/test_routes_playlists.py tests/test_routes_digest.py -q`
Expected: all PASS.

- [ ] **Step 3: Visual verification in the preview**

Using the preview tools:
1. `preview_start` (or reuse a running server), then load `/`.
2. Confirm the Queues and Digests strips render as a single row ending in the "+" tile at desktop width.
3. `preview_resize` to a mobile width (~390px) and confirm each strip shows 4 cells (3 cards + add tile).
4. Click each headline (`preview_click`) and confirm navigation to `/playlists`, `/digest`, and `/library` respectively (use `preview_snapshot` after each).
5. On `/library`, confirm video cards render and the "Load more" button is present when there are >25 videos.
6. `preview_screenshot` of the home strips (desktop + mobile) and of `/library` to share as proof.

- [ ] **Step 4: Final commit (only if verification surfaced fixes)**

If steps 1–3 surfaced no issues, there is nothing to commit. If a fix was needed, commit it with a descriptive message, e.g.:

```bash
git add -A
git commit -m "fix(home): <describe the fix found during verification>"
```
