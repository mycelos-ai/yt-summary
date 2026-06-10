# Unified Export Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scattered per-section `.md`/`.json` download buttons with one reusable "Export ▾" menu offering download, copy-Markdown, and copy-link actions.

**Architecture:** A Jinja macro renders the trigger plus the dropdown markup; Alpine.js (already loaded globally) drives open/close (mirroring the profile dropdown in `base.html`); a tiny `export-menu.js` provides the two clipboard helpers. The Obsidian export (`/v/{id}/export.md`) becomes the single Markdown per item; the plain `/summary.md` link leaves the UI. No route or schema changes.

**Tech Stack:** FastAPI + Jinja2 templates, Alpine.js (dropdown state), vanilla JS (clipboard), pytest + Starlette TestClient.

---

## File Structure

- **Create** `app/templates/macros/export_menu.html` — the `export_menu(md_url, json_url, label)` macro: trigger + Alpine dropdown.
- **Create** `app/static/export-menu.js` — two global helpers: `ytsCopyMarkdown(mdUrl, btn)` and `ytsCopyLink(mdUrl, btn)`.
- **Modify** `app/templates/video_summary_section.html:15-21` — replace the three summary buttons with one macro call.
- **Modify** `app/templates/video_detail.html:129-132` — replace the transcript `↓ .md` with one macro call; add the `<script>` include for `export-menu.js`.
- **Test** `tests/test_routes_export_menu.py` — macro render assertions via TestClient.

Each task below produces a self-contained, committable change.

---

## Task 1: The export_menu macro

**Files:**
- Create: `app/templates/macros/export_menu.html`
- Test: `tests/test_routes_export_menu.py`
- Modify (to exercise the macro): `app/templates/video_summary_section.html:15-21`

- [ ] **Step 1: Write the failing test**

Create `tests/test_routes_export_menu.py`:

```python
"""Render tests for the unified export menu macro (no live network)."""

from fastapi.testclient import TestClient

from app.main import create_app


def _seed_video_with_summary(app, vid="em1", title="Export Me"):
    import asyncio

    async def setup():
        from app.repos import videos as videos_repo
        await videos_repo.upsert_metadata(
            app.state.db, video_id=vid, url="u", title=title,
            description="d", thumbnail_path=None, duration_seconds=None,
        )
        await videos_repo.set_summary(app.state.db, vid, "## TL;DR\nhi", "m")
    asyncio.get_event_loop().run_until_complete(setup())


def test_summary_renders_one_export_menu(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "em1")
        resp = client.get("/v/em1")
    assert resp.status_code == 200
    # Exactly one export menu for the summary, pointing at the Obsidian
    # export .md and the .json.
    assert resp.text.count('data-export-menu') >= 1
    assert 'data-md-url="/v/em1/export.md"' in resp.text
    assert 'data-json-url="/v/em1/export.json"' in resp.text


def test_summary_drops_legacy_buttons(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "em2")
        resp = client.get("/v/em2")
    # The three old separate buttons are gone from the summary controls.
    assert "/v/em2/summary.md" not in resp.text
    assert "⬇ Export .md" not in resp.text
    assert "⬇ Export .json" not in resp.text


def test_export_menu_has_nojs_download_link(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "em3")
        resp = client.get("/v/em3")
    # The trigger is a real <a href> to the .md — download works w/o JS.
    assert 'href="/v/em3/export.md"' in resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_routes_export_menu.py -q`
Expected: FAIL — `data-export-menu` / `data-md-url` absent (macro not built yet), and the legacy-button assertions fail because the old buttons are still present.

- [ ] **Step 3: Create the macro**

Create `app/templates/macros/export_menu.html`:

```jinja
{% macro export_menu(md_url, json_url=None, label="Export") %}
<span class="export-menu" data-export-menu
      data-md-url="{{ md_url }}"
      {% if json_url %}data-json-url="{{ json_url }}"{% endif %}
      x-data="{ open: false }" x-on:click.outside="open = false"
      x-on:keydown.escape="open = false">
  {# No-JS fallback: a real download link. Alpine upgrades it into a
     dropdown trigger when JS is on. #}
  <a class="section-download export-menu-trigger" href="{{ md_url }}"
     x-on:click.prevent="open = !open"
     onclick="event.stopPropagation()">⬇ {{ label }} ▾</a>
  <span class="export-menu-dropdown" x-show="open" x-cloak x-transition.opacity>
    <a class="export-menu-item" href="{{ md_url }}"
       x-on:click="open = false">⬇ Download .md</a>
    <button type="button" class="export-menu-item"
            x-on:click="ytsCopyMarkdown('{{ md_url }}', $event.target); open = false">
      📋 Copy Markdown</button>
    <button type="button" class="export-menu-item"
            x-on:click="ytsCopyLink('{{ md_url }}', $event.target); open = false">
      🔗 Copy link</button>
    {% if json_url %}
      <a class="export-menu-item" href="{{ json_url }}"
         x-on:click="open = false">⬇ Download .json</a>
    {% endif %}
  </span>
</span>
{% endmacro %}
```

- [ ] **Step 4: Use the macro in the summary section**

In `app/templates/video_summary_section.html`, at the very top of the file (before any other content) add the import:

```jinja
{% import "macros/export_menu.html" as exp %}
```

Then replace lines 16-21 (the three `<a class="section-download">` buttons: `summary.md`, `export.md`, `export.json`) with a single call, keeping the `🎧 Audio` button that follows untouched:

```jinja
        {{ exp.export_menu("/v/" ~ video.id ~ "/export.md", "/v/" ~ video.id ~ "/export.json") }}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_routes_export_menu.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Run the existing video-route tests (no regressions)**

Run: `.venv/bin/python -m pytest tests/test_routes_videos.py -q`
Expected: PASS. Note: a Part A test (`test_video_summary_markdown_section_only` etc.) exercises the `/summary.md` *endpoint* directly, not the UI button — it must still pass because the endpoint stays.

- [ ] **Step 7: Lint**

Run: `.venv/bin/ruff check tests/test_routes_export_menu.py`
Expected: All checks passed.

- [ ] **Step 8: Commit**

```bash
git add app/templates/macros/export_menu.html app/templates/video_summary_section.html tests/test_routes_export_menu.py
git commit -m "feat(export-menu): macro + summary section uses it"
```

---

## Task 2: Clipboard helpers (export-menu.js)

**Files:**
- Create: `app/static/export-menu.js`
- Modify: `app/templates/video_detail.html` (add the script include near the existing `highlight.js` include at line ~253)

- [ ] **Step 1: Create the JS helpers**

Create `app/static/export-menu.js`:

```javascript
// Clipboard helpers for the export menu. Alpine drives open/close; these
// do the copy actions. Progressive enhancement: if the clipboard API is
// unavailable (old browser / non-secure context), fall back to a prompt
// so the user can still grab the text/URL.
function _ytsFlash(btn, msg) {
  if (!btn) return;
  const original = btn.textContent;
  btn.textContent = msg;
  setTimeout(() => { btn.textContent = original; }, 1500);
}

async function _ytsWrite(text, btn, okMsg) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      _ytsFlash(btn, okMsg);
      return;
    }
    throw new Error("clipboard unavailable");
  } catch (e) {
    window.prompt("Copy manually:", text);
  }
}

async function ytsCopyMarkdown(mdUrl, btn) {
  try {
    const resp = await fetch(mdUrl);
    if (!resp.ok) throw new Error("fetch failed: " + resp.status);
    const text = await resp.text();
    await _ytsWrite(text, btn, "Copied ✓");
  } catch (e) {
    window.prompt("Couldn't fetch the Markdown — here's the link:",
      new URL(mdUrl, location).href);
  }
}

function ytsCopyLink(mdUrl, btn) {
  _ytsWrite(new URL(mdUrl, location).href, btn, "Link copied ✓");
}
```

- [ ] **Step 2: Include the script on the detail page**

In `app/templates/video_detail.html`, find the existing line (~253):

```jinja
  <script src="/static/highlight.js" defer></script>
```

Add immediately after it:

```jinja
  <script src="/static/export-menu.js" defer></script>
```

- [ ] **Step 3: Verify the script is referenced in the rendered page**

Add to `tests/test_routes_export_menu.py`:

```python
def test_export_menu_script_included(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed_video_with_summary(app, "em4")
        resp = client.get("/v/em4")
    assert "/static/export-menu.js" in resp.text
```

- [ ] **Step 4: Run the test**

Run: `.venv/bin/python -m pytest tests/test_routes_export_menu.py::test_export_menu_script_included -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/static/export-menu.js app/templates/video_detail.html tests/test_routes_export_menu.py
git commit -m "feat(export-menu): clipboard helpers + script include"
```

---

## Task 3: Transcript section uses the menu

**Files:**
- Modify: `app/templates/video_detail.html:129-132` (transcript `↓ .md` → macro)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_routes_export_menu.py`:

```python
def test_transcript_renders_export_menu(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        _seed_video_with_summary(app, "tm1")

        async def add_transcript():
            from app.models import TranscriptSource
            from app.repos import videos as videos_repo
            await videos_repo.set_transcript(
                app.state.db, "tm1", "the transcript",
                TranscriptSource.AUTO_SUBS, language="en",
            )
        asyncio.get_event_loop().run_until_complete(add_transcript())
        resp = client.get("/v/tm1")
    assert resp.status_code == 200
    # The transcript section now has an export menu for transcript.md and
    # no longer a bare "↓ .md" link to it.
    assert 'data-md-url="/v/tm1/transcript.md"' in resp.text
    assert '>↓ .md</a>' not in resp.text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_routes_export_menu.py::test_transcript_renders_export_menu -q`
Expected: FAIL — `data-md-url=".../transcript.md"` absent; the bare `↓ .md` link still present.

- [ ] **Step 3: Ensure the macro is imported in video_detail.html**

At the top of `app/templates/video_detail.html` (after `{% extends %}`, before content), add if not already present:

```jinja
{% import "macros/export_menu.html" as exp %}
```

- [ ] **Step 4: Replace the transcript download link**

In `app/templates/video_detail.html`, replace lines 130-132 (the `<a class="section-download" href="/v/{{ video.id }}/transcript.md" …>↓ .md</a>`) with:

```jinja
          {{ exp.export_menu("/v/" ~ video.id ~ "/transcript.md", label="Export") }}
```

Leave the `🎧 Audio` button after it untouched.

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_routes_export_menu.py::test_transcript_renders_export_menu -q`
Expected: PASS.

- [ ] **Step 6: Full export-menu + detail regression**

Run: `.venv/bin/python -m pytest tests/test_routes_export_menu.py tests/test_routes_videos.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/templates/video_detail.html tests/test_routes_export_menu.py
git commit -m "feat(export-menu): transcript section uses the menu"
```

---

## Task 4: Minimal styling

**Files:**
- Modify: `app/static/app.css` (append export-menu styles)

- [ ] **Step 1: Append styles**

Append to `app/static/app.css`:

```css
/* Export menu (unified download / copy / link). */
.export-menu { position: relative; display: inline-block; }
.export-menu-dropdown {
  position: absolute; right: 0; top: 100%; z-index: 50;
  min-width: 200px; margin-top: 4px;
  background: var(--surface, #fff);
  border: 1px solid rgba(127, 127, 127, 0.25);
  border-radius: 8px; padding: 4px;
  display: flex; flex-direction: column;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
}
.export-menu-item {
  display: block; width: 100%; text-align: left;
  background: none; border: 0; cursor: pointer;
  font: inherit; color: inherit;
  padding: 8px 10px; border-radius: 6px;
}
.export-menu-item:hover { background: rgba(127, 127, 127, 0.12); }
```

- [ ] **Step 2: Verify the page still renders (smoke)**

Run: `.venv/bin/python -m pytest tests/test_routes_export_menu.py -q`
Expected: PASS (CSS doesn't affect render assertions, but confirms nothing broke).

- [ ] **Step 3: Commit**

```bash
git add app/static/app.css
git commit -m "style(export-menu): dropdown styling"
```

---

## Task 5: Manual browser verification

Not a pytest task — clipboard behaviour is browser-only. Verify against the running server (`:8210`) and report.

- [ ] **Step 1: Confirm the menu markup served**

Run: `curl -s http://127.0.0.1:8210/v/<some-id> | grep -c data-export-menu`
Expected: ≥ 2 (summary + transcript when both exist).

- [ ] **Step 2: Confirm the script is served**

Run: `curl -s http://127.0.0.1:8210/static/export-menu.js | head -1`
Expected: the first line of the helper file.

- [ ] **Step 3: In the browser**

Open a video detail page, click "Export ▾" on the summary:
- Dropdown opens; "Download .md" downloads the Obsidian export.
- "Copy Markdown" → paste into a text field shows the frontmatter + summary; button briefly shows "Copied ✓".
- "Copy link" → clipboard holds the absolute `/v/<id>/export.md` URL.
- Clicking outside / Escape closes the dropdown.

Report results to the user with the curl outputs as evidence.

---

## Self-Review Notes

- **Spec coverage:** three actions (download/copy-md/copy-link) → Task 1 macro + Task 2 JS; JSON download for videos → Task 1 (`json_url` row); Obsidian export as the Markdown + plain `/summary.md` dropped from UI → Task 1 Step 4; transcript surface → Task 3; progressive enhancement (real `<a href>`) → Task 1 test `test_export_menu_has_nojs_download_link`; testing strategy → Tasks 1-3 render tests + Task 5 manual. Ask-answer surface is explicitly out of scope here (ships with the ask feature).
- **Deviation from spec, noted:** the spec described a vanilla `export-menu.js` building the whole dropdown. The plan instead drives open/close with **Alpine.js** (already loaded; mirrors the `base.html` profile dropdown) and keeps only the clipboard helpers in `export-menu.js`. Same UX, less hand-rolled JS, consistent with the codebase.
- **Type/name consistency:** macro `export_menu(md_url, json_url=None, label="Export")`; JS globals `ytsCopyMarkdown(mdUrl, btn)` / `ytsCopyLink(mdUrl, btn)` — used identically in Tasks 1-3.
