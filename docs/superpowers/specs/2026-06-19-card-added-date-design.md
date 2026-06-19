# Added-Date on Video Cards — Design

**Date:** 2026-06-19
**Status:** Approved (brainstorming complete)

## Summary

Show the library-added date on each video card, rendered through the existing
`relative_time` Jinja filter — "X minutes/hours ago" for items added today,
an ISO date (`YYYY-MM-DD`) for anything older. This helps the user tell new
from old at a glance (the trigger was confusion over where newly-synced
playlist videos were).

This is deliberately minimal: `videos.created_at` already exists and is
already carried on the `Video` model into every card render, and the
`relative_time` filter is already registered and tested. No DB change, no sync
change, no new filter.

## Goals / Non-Goals

**Goals**
- Display `video.created_at` on the video card via the existing
  `relative_time` filter.
- Visually consistent with the muted small-text style already used elsewhere
  (e.g. `.related-summary-reason`, playlist "Refreshed …" lines).

**Non-Goals**
- NOT the YouTube publish date (`upload_date`) — that would require sync-time
  data capture and a new column; explicitly out of scope (user chose
  added-date).
- NOT a new "X days/weeks ago" relative format — the project's `relative_time`
  intentionally falls back to an ISO date beyond today, and we reuse it as-is
  for consistency.
- No change to `relative_time` itself (leaving playlist headers untouched).

## Component

### `app/templates/video_card.html`
- Add one line rendering the added date. Place it with the status line / under
  the title, e.g.:
  ```html
  <p class="video-card-date">{{ video.created_at | relative_time }}</p>
  ```
- The card already receives the full `Video` object, so `video.created_at` is
  in scope with no route changes.

### Styling (`app/static/app.css`)
- Add a small, muted `.video-card-date` rule, mirroring the existing
  muted-text convention (small font-size, `var(--muted)` color). Reuse the
  same muted variable the stylesheet already defines.

## Data Flow

`videos.created_at` (already populated on every row, set at
`upsert_metadata`) → `Video.created_at` (already mapped) → template →
`relative_time` filter → rendered string. No new data path.

## Error Handling

`relative_time(None)` already returns `""` (guarded in the filter), so a row
with a missing timestamp renders an empty date rather than erroring. In
practice `created_at` is `NOT NULL` in the schema, so this is just defensive.

## Testing

Follow existing route-test conventions (e.g. `tests/test_routes_home.py` /
`tests/test_routes_videos.py`).
- A render test asserting the card shows a date string for a seeded video
  (e.g. the rendered HTML for a video added "today" contains an "ago" string,
  or for an older `created_at` contains the ISO date). The `relative_time`
  filter itself is already unit-tested, so this only verifies the card wires
  it in.

## Open Risks / Notes

- Card vertical space grows by one line. Acceptable; the date is short and
  muted. If the single-row strip layout (home page) looks cramped, the date
  can be hidden in that context later — out of scope for this change.
