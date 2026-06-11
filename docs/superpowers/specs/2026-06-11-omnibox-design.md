# Omnibox — unified search / ask, separated add

**Status:** Draft — design phase
**Date:** 2026-06-11

## Goal

The home page has three scattered, related inputs: a top "paste a URL"
add field (`POST /videos`), a mid-page "search past videos" field
(`GET /?q=`), and an "Ask my library" link further down (`/ask`). Users
can't tell which field does what, and to search or ask they must scroll.

Reshape into two clear concepts:

1. **One header input with a Search ⇄ Ask toggle.** "Search" is the
   default (free, instant, no LLM); "Ask" switches to starting an ask
   thread.
2. **A separate "+ Add" button** that opens an overlay with an
   auto-expanding field accepting a single URL, a playlist URL, a
   website, or a `curl` command (one input for the first cut; multi-link
   is a later follow-up). This gives the `curl` case room to breathe.

## Decisions (settled)

- **Search default, Ask via toggle.** Search is cheap/instant; Ask costs
  an LLM call, so it's the deliberate switch — not a guess from the text.
- **Ask starts a THREAD.** Ask my library is now multi-turn; the Ask
  toggle + submit starts a new thread (`POST /ask`) and navigates to
  `/ask/{id}`, where follow-ups continue.
- **Add is an overlay, single input for now.** Auto-expanding textarea;
  submit posts one URL/curl to the existing `POST /videos` (unchanged
  backend). Multi-link batch import is explicitly a later follow-up.
- **Progressive enhancement.** Without JS: the input is a plain search
  GET form, "Add" is a link to a `/submit`-style page (or the existing
  add field stays as a no-JS fallback), Ask is the `/ask` link. JS
  upgrades into the toggle + overlay.
- **Home now; compact header variant on sub-pages later.** This spec
  covers the home-page omnibox. (The user wants a slim header variant on
  sub-pages too; that's a follow-up once the home shape is settled.)

## Behaviour

The omnibox is one text input with a small segmented Search/Ask toggle.

- **Toggle = Search (default):** submitting does `GET /?q=<text>`
  (today's search), re-rendering the home grid filtered. No LLM.
- **Toggle = Ask:** submitting does `POST /ask` with the text as the
  first question → 303 redirect to `/ask/{id}` (the thread page). This
  reuses the ask thread feature as-is.
- **+ Add button:** opens the overlay; the overlay form posts to
  `POST /videos` exactly as the current add field does (URL or curl).
  On success the existing handler redirects to the new item's detail
  page (browser) or returns the card fragment (HTMX) — unchanged.

The toggle state is remembered for the session (a small JS/localStorage
nicety) so a user who mostly asks doesn't re-toggle every visit.

## Architecture

### Templates
- `app/templates/home.html`: replace the three current affordances
  (the `submit-form` add field, the `search-form`, the `💬 Ask my
  library →` link) with:
  - one omnibox partial `app/templates/_omnibox.html` (the toggle +
    input, an Alpine component for toggle state, posting to `/` or
    `/ask` depending on the mode), and
  - a `+ Add` button that triggers the add overlay.
- `app/templates/_add_overlay.html`: the overlay (Alpine `x-data` open
  state, like the profile dropdown / export menu pattern) with an
  auto-expanding `<textarea name="url">` posting to `/videos`.
- Keep `/ask` reachable directly (the archive list page) — the omnibox
  is the entry point, not a replacement for the `/ask` index.

### JS
- Alpine drives the toggle (`mode: 'search' | 'ask'`) and the overlay
  open/close (already loaded globally; mirrors existing dropdowns).
- The form's `action`/`method` switch with the mode: Search →
  `method=get action=/`; Ask → `method=post action=/ask`. Implement by
  binding `action`/`method` via Alpine, OR by two forms toggled with
  `x-show` (simpler, no dynamic method binding). Auto-expand the overlay
  textarea with a tiny input handler (scrollHeight).
- Persist mode in `localStorage` ("yts-omnibox-mode").

### Routes
No new routes. Reuses `GET /?q=` (search), `POST /ask` (start thread),
`POST /videos` (add). The CSRF/posture is unchanged — Ask and Add are
POSTs from real forms, same as today.

## Testing strategy

House style: render assertions via TestClient; no browser in pytest.

- **Render:** the home page contains the omnibox (an input + a
  Search/Ask toggle), an Add trigger, and the add overlay markup
  (`name="url"` textarea posting to `/videos`). The old standalone
  `search-form` and the `💬 Ask my library →` link are gone (their
  function moved into the omnibox).
- **Search still works:** `GET /?q=foo` returns 200 and filters (existing
  behaviour — guard it).
- **Ask from omnibox:** the Ask-mode form posts to `/ask` (assert the
  form action is `/ask` with method post in the rendered page).
- **Add overlay:** the overlay form posts to `/videos` with `name="url"`.
- **No-JS fallback:** a plain search GET form is present (the input has a
  name the `/` route reads as `q`).
- **Regression:** existing home route tests stay green.
- **Manual browser pass** (`:8210`): toggle Search/Ask; Search filters
  inline; Ask starts a thread and lands on `/ask/{id}`; "+ Add" opens
  the overlay, a pasted curl fits, submit queues the item.

## Out of scope

- Multi-link batch add (later follow-up — the overlay textarea is built
  for it, but the backend stays single-input).
- The compact header variant on sub-pages (later).
- Intent auto-detection from text (we use an explicit toggle, not a
  guess) — except a URL pasted into the search field could hint "did you
  mean Add?", which is a nice-to-have, not in this cut.

## Rollout

Single PR: `_omnibox.html` + `_add_overlay.html` + home.html rewrite +
CSS + render tests. No schema/route/behaviour changes to the backend.
