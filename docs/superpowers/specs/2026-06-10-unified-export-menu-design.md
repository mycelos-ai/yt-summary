# Unified Export Menu

**Status:** Draft — design phase
**Date:** 2026-06-10

## Goal

Today the "get this item out as a file" affordance is scattered and
inconsistent. The video detail page alone shows three separate buttons
next to the summary (`↓ .md` for the plain summary, `⬇ Export .md` for
the Obsidian-flavoured export, `⬇ Export .json`) plus a separate `↓ .md`
on the transcript section. None of them offer **copy to clipboard** —
which is what the user actually wants: copy the Markdown and paste it
into a normal chat tool to keep working with it.

Replace the scattered single-purpose buttons with **one reusable export
element**: an "Export ▾" trigger that opens a small menu with a
consistent set of actions. One implementation, used everywhere (summary,
transcript, and — in a later feature — the ask answer).

## Terminology

As in earlier specs: every "user" is a Netflix-style **Profile**.

## Actions in the menu

For any item that has a Markdown URL, the menu offers:

1. **Download .md** — a normal link to the `.md` URL (today's download
   behaviour). Works without JS.
2. **Copy Markdown** — JS `fetch`es the `.md` URL and writes the text to
   the clipboard, with a brief "Copied ✓" confirmation.
3. **Copy link to Markdown** — copies the absolute `.md` URL to the
   clipboard.

When a JSON URL is also provided (videos only), a fourth row:

4. **Download .json** — link to the `.json` URL.

## Markdown variant decision

A video summary currently has **three** export flavours: the plain
`/v/{id}/summary.md`, the richer Obsidian export `/v/{id}/export.md`
(YAML frontmatter + tags + playlists + rewritten timestamp links), and
`/v/{id}/export.json`.

**The Obsidian export (`export.md`) becomes _the_ Markdown** used by the
menu — one clear Markdown per item. The plain `/summary.md` endpoint
stays for scripts/back-compat but is **dropped from the UI**. This also
cleans up the three-button clutter that the Part A export feature
introduced.

Per-section URLs:

| Surface    | md_url                     | json_url                    |
|------------|----------------------------|-----------------------------|
| Summary    | `/v/{id}/export.md`        | `/v/{id}/export.json`       |
| Transcript | `/v/{id}/transcript.md`    | (none)                      |
| Ask answer | (added in the ask feature) | (none)                      |

## Architecture

### Jinja macro

`app/templates/macros/export_menu.html`:

```jinja
{% macro export_menu(md_url, json_url=None, label="Export") %}
<div class="export-menu" data-export-menu
     data-md-url="{{ md_url }}"
     {% if json_url %}data-json-url="{{ json_url }}"{% endif %}>
  {# No-JS fallback: a real download link. JS upgrades this into the
     dropdown with the copy actions. #}
  <a class="section-download export-menu-trigger" href="{{ md_url }}"
     onclick="event.stopPropagation()">⬇ {{ label }}</a>
</div>
{% endmacro %}
```

Used via `{% import "macros/export_menu.html" as exp %}` then
`{{ exp.export_menu("/v/" ~ video.id ~ "/export.md",
"/v/" ~ video.id ~ "/export.json") }}`.

### Enhancement script

`app/static/export-menu.js` (loaded `defer`):

- Finds every `[data-export-menu]`.
- Builds a dropdown per element from the `data-md-url` / `data-json-url`
  attributes.
- **Copy Markdown:** `fetch(mdUrl)` → `navigator.clipboard.writeText`,
  show "Copied ✓" for ~1.5s. On failure (no clipboard API / non-secure
  context) fall back to a visible "Couldn't copy — here's the link"
  message exposing the URL.
- **Copy link:** `new URL(mdUrl, location).href` → clipboard.
- **Download / JSON:** ordinary links (browser download).
- Closes on outside-click and Escape.

### Replaced markup

- `video_summary_section.html`: the three summary buttons
  (`↓ .md` / `⬇ Export .md` / `⬇ Export .json`) → one `export_menu`
  call. The `🎧 Audio` button stays.
- `video_detail.html`: the transcript `↓ .md` → one `export_menu` call.

No route changes — every endpoint the menu points at already exists.

## Progressive enhancement

The whole project works without JS. The export trigger is, at its core,
a plain `<a href>` to the `.md` URL — download works with JS disabled.
The copy actions require JS and simply aren't present without it. The
script upgrades the existing link into the dropdown; it doesn't replace
server-rendered behaviour.

## Testing strategy

House style: no live network/browser in the pytest suite.

- **Macro render (TestClient + real Jinja):** the video detail page
  renders exactly one `data-export-menu` for the summary
  (`data-md-url=/v/{id}/export.md`, `data-json-url=…/export.json`) and
  one for the transcript (`data-md-url=…/transcript.md`).
- **De-cluttered:** the old three separate summary buttons and the plain
  `/summary.md` link are gone from the detail page.
- **No-JS fallback:** the rendered trigger is an `<a href>` to the `.md`
  URL.
- **Endpoints:** already covered by the Part A export tests; the menu
  only references existing URLs.
- **Clipboard JS:** not unit-tested in the Python suite (pure
  browser-only clipboard behaviour). Verified manually against the
  running server and reported — documented as such.

## Out of scope

Bulk/library-wide copy (the `/export.zip` flow stays as is), copying
rendered HTML, a generic "share" surface. The ask-answer integration is
this menu's first reuse but ships with the ask follow-up feature.

## Rollout

Single PR: macro + JS + the two template swaps + render tests. No schema
changes, no route changes.
