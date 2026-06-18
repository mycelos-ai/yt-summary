# Clickable section headlines, single-row strips & a Library page

**Date:** 2026-06-18
**Status:** Approved

## Problem

On the home page the three content sections — **Queues & playlists**,
**Digests**, and **Library** — have plain-text headlines that aren't
navigable. The Queues and Digests strips also wrap to show *every*
card, which makes the home page long and buries the Library beneath a
full dump of playlists/digests. There is no dedicated full-library
listing page (the Library section *is* the home page).

## Goals

1. Make the three section headlines clickable, each leading to a page
   that lists everything in that section.
2. Cap the Queues and Digests strips to a single row: **8 cells on
   desktop, 4 on mobile**, where the last cell is the "+" add tile.
3. Add a dedicated `/library` page reachable from the Library headline.

## Non-goals

- No change to the individual card markup or their existing links
  (playlist cards already link to `/p/{id}`, digest cards to
  `/digest/{id}`).
- No change to search, tagging, or the load-more mechanism beyond
  reusing it on the new page.

## Design

### 1. Clickable headlines

Convert the three `<p class="section-title">` headlines in
`home.html` into links:

| Headline            | Target       | Page status |
|---------------------|--------------|-------------|
| Queues & playlists  | `/playlists` | exists      |
| Digests             | `/digest`    | exists      |
| Library             | `/library`   | **new**     |

Add a `.section-title-link` rule in `app.css`: inherits the
`.section-title` typography, renders a trailing `→` that brightens on
hover so the headline reads as navigable without looking like a body
link.

The standalone text links currently sitting **below** the strips
(`More →`, `All digests →`, `All questions →`) become redundant for
the two strips that now have clickable headlines. Remove the
`More →` (playlists) and `All digests →` links. Keep
`All questions →` — Ask has no headline of its own on the home page.

### 2. Single-row strips (8 desktop / 4 mobile, last = +)

The strips today use `grid-template-columns: repeat(auto-fill,
minmax(220px, 1fr))`, which wraps to multiple rows and renders all
cards. Change to a fixed single-row layout:

- **Server limits.** Send up to **7** cards per strip so 7 cards +
  the add tile = 8 cells on desktop:
  - `HOME_PLAYLIST_LIMIT` in `home.py`: `5 → 7`.
  - `recent_digests` limit in `home.py`: `4 → 7`.
  - The existing `+1` over-fetch / `has_more_playlists` logic stays;
    it now drives nothing visible on the strip (the headline replaces
    the "More →" link) but is harmless and may be reused. Leave it.
- **Desktop CSS.** `.playlist-strip` becomes a fixed
  `grid-template-columns: repeat(8, 1fr)` single-row grid (no
  wrapping). Cards keep their look; columns get narrower than 220px
  on smaller desktop widths, which is acceptable for an overview
  strip.
- **Mobile CSS.** At `max-width: 640px`, switch to
  `repeat(4, 1fr)` and hide every `.playlist-card-wrap` /
  `.digest-card-add-wrap` / card past the 3rd via `nth-child` so the
  row shows 3 cards + the add tile = 4 cells. The add tile is always
  last in source order, so it survives the trim.

  Implementation note: the add tile for playlists is a bare
  `<a class="playlist-card playlist-card-add">` and for digests a
  `<form class="digest-card-add-wrap">`; the real cards are wrapped in
  `.playlist-card-wrap` / `.digest-card-wrap`. The mobile trim targets
  the wrappers (real cards) by `nth-child`, leaving the add tile
  untouched regardless of how many real cards rendered.

### 3. New `/library` page

- **Route:** `GET /library` in `app/routes/home.py` (shares the
  video-card plumbing — `videos_repo.list_recent`, `playlist_links`,
  `video_tags`, the `+1` has-more trick, and the same
  `HOME_VIDEO_PAGE_SIZE`). Honors the `?tag=` filter exactly as home
  does. Renders a new `library.html`.
- **Template:** `library.html` extends `base.html`, mirrors the home
  Library block — an `<h1>Library</h1>` header, the tag filter banner,
  the `#video-list` grid of `video_card.html`, and the same load-more
  button pointing at `/videos/load-more`. No hero, no strips.
- The load-more fragment route (`/videos/load-more`) is reused
  unchanged.

## Testing

Verify against the preview server:

1. Home page: each of the three headlines navigates to its target.
2. Desktop width: Queues and Digests strips render as a single row
   ending in the "+" tile.
3. Mobile width (`preview_resize`): strips show 4 cells (3 cards +
   add).
4. `/library`: lists videos as cards, tag filter banner appears when
   `?tag=` is set, load-more button fetches the next page.
