# Diagnostics: Last Video Added / Processed — Design

**Date:** 2026-06-19
**Status:** Approved (brainstorming complete)

## Summary

Add a small section to the diagnostics page (`/settings/diagnostics`) showing
two at-a-glance facts:
- **Last added** — the most recently added video (greatest `created_at`), with
  its title (linked to `/v/{id}`) and a relative time.
- **Last processed** — the most recently summarized video (greatest
  `updated_at` among videos that have a summary), same title-link + relative
  time.

Together these answer "is the crawl actually pulling new things in, and is the
pipeline working through them?" at a glance, next to the existing "last
scheduler tick" / per-playlist "last refreshed".

## Goals / Non-Goals

**Goals**
- Two repo queries returning a single `Video | None` each (most recent by
  `created_at`; most recently summarized by `updated_at`).
- A diagnostics section rendering each as `title (link) + relative time`,
  reusing the existing `relative_time` Jinja filter.
- Graceful empty state when a query returns `None`.

**Non-Goals**
- No new timestamp columns — uses existing `created_at` / `updated_at`.
- No auto-refresh / live polling — it renders on page load like the rest of
  the diagnostics page.
- No change to home/library or to the scheduler.

## Components

### 1. Repo (`app/repos/videos.py`)
- `async def get_most_recent(db, *, user_id: int = 1) -> Video | None`
  — `SELECT * FROM videos WHERE user_id=? AND archived_at IS NULL
  ORDER BY created_at DESC, id DESC LIMIT 1` → `_row_to_video` or None.
- `async def get_most_recently_summarized(db, *, user_id: int = 1) -> Video | None`
  — `SELECT * FROM videos WHERE user_id=? AND archived_at IS NULL
  AND summary IS NOT NULL ORDER BY updated_at DESC, id DESC LIMIT 1` →
  `_row_to_video` or None.
- Both mirror the existing `list_recent` style (same WHERE/ORDER conventions,
  `_row_to_video` mapping).

### 2. Route (`app/routes/settings.py`, `diagnostics_page`)
- Call both repo functions and add `last_added` and `last_processed` to the
  existing `TemplateResponse` context dict (which already has `db`).

### 3. Template (`app/templates/diagnostics.html`)
- A new section placed after the Scheduler block and before the Log tail
  (lifecycle info grouping). Two rows:
  - "Last added": if `last_added`, `<a href="/v/{{ last_added.id }}">{{ last_added.title }}</a>`
    + `{{ last_added.created_at | relative_time }}`; else a muted "—" / "no
    videos yet".
  - "Last processed": if `last_processed`,
    `<a href="/v/{{ last_processed.id }}">{{ last_processed.title }}</a>`
    + `{{ last_processed.updated_at | relative_time }}`; else muted empty
    state.
- `relative_time` is already a registered global filter (used elsewhere on the
  page / cards): today → "X ago", older → ISO date.

## Data Flow

```
GET /settings/diagnostics
  → diagnostics_page:
      last_added     = videos_repo.get_most_recent(db)
      last_processed = videos_repo.get_most_recently_summarized(db)
  → context {... last_added, last_processed}
  → diagnostics.html renders the new section
```

## Error Handling

- Both queries return `None` on an empty/all-archived library → template shows
  a muted empty state, never errors.
- `relative_time(None)` already returns "" (defensive), though `created_at` /
  `updated_at` are `NOT NULL` so this won't trigger in practice.

## Testing

Follow existing conventions (repo test, route render test).
- **Repo:** `get_most_recent` returns the greatest-`created_at` active video,
  ignores archived, returns None when empty. `get_most_recently_summarized`
  returns the greatest-`updated_at` video that HAS a summary, ignores ones
  without a summary, returns None when none qualify.
- **Route/render:** the diagnostics page renders the new section with a linked
  title and a time string for a seeded video; empty library renders the empty
  state without error.

## Open Risks / Notes

- "Last processed" uses `updated_at` among summarized videos. `updated_at` is
  bumped on several writes (set_summary, set_transcript, etc.), but since the
  filter requires `summary IS NOT NULL`, the most-recently-touched summarized
  video is a good proxy for "last processed". Acceptable for a diagnostics
  glance.
