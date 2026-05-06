# Video Tags — Design Spec

**Date:** 2026-05-06
**Status:** Approved for planning
**Owner:** Stefan
**Builds on:** [yt-summary core](2026-05-05-yt-summary-design.md), [playlists](2026-05-06-playlists-design.md)

## Purpose

Surface YouTube's per-video tags in the UI so the user can:
- See at a glance what topics a video covers (on the card and detail page).
- Click a tag to filter the library down to videos that share it.

Tags come from yt-dlp's `tags` field (uploader-set keywords). No LLM
involvement.

## Scope

In:
- Capture `tags` whenever yt-dlp extracts full metadata for a video.
- Persist them in a normalized `tags` + `video_tags` schema.
- Show them as clickable pills on each video card and on the detail page.
- Filter the home library by `?tag=<name>`.

Out:
- LLM-generated tags (potential future feature).
- Tag editing in the UI.
- Tag clouds / popularity browsing pages (just the filter view for V1).
- Multi-tag filter (`?tag=a&tag=b`). The filter accepts a single tag for V1.
- Tag normalization beyond case-insensitivity.

## Data Model

```sql
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE video_tags (
    video_id TEXT NOT NULL REFERENCES videos(id),
    tag_id   INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (video_id, tag_id)
);
CREATE INDEX idx_video_tags_tag ON video_tags(tag_id);
```

`COLLATE NOCASE` means "Python" and "python" collapse into the same row.
The first writer wins on capitalization (whatever case is stored first
is the case shown in the UI).

Migration: pure additive. Both tables live alongside the existing
schema. No changes to `videos`, `playlists`, etc.

## How tags get populated

Two entry points produce tags:

1. **`POST /videos`** — when a user submits a single URL. The route
   already calls `fetch_metadata`. We extend `fetch_metadata` to return
   `tags: list[str]`, and the route writes them via the new repo.

2. **The processing pipeline** — when the worker picks up a video that
   has no tags yet (e.g. a video pulled from a playlist via `extract_flat`,
   which doesn't surface tags). The pipeline runs `fetch_metadata` after
   `obtain_transcript` and writes the tags. Cheap because it's the same
   yt-dlp flow we already use.

Tags are written via a single helper:

```python
async def set_tags_for_video(db, video_id: str, tag_names: list[str]) -> None:
    # Upserts each tag into `tags`, then replaces video_tags links to
    # match the given list (idempotent).
```

If yt-dlp's `tags` field is missing or empty, this is a no-op.

## Filtering

`GET /?tag=<name>` lists all videos that have that tag, ordered by the
existing `created_at DESC, id DESC` rule. The tag name match is
case-insensitive (handled by the `COLLATE NOCASE` on the column).

If `q` and `tag` are both set, search runs first (FTS5), then the
result is filtered down to videos that have the tag. Implemented in
the videos repo as `list_recent(db, *, tag=None, limit=50)` and a new
filter applied in `search`.

When a tag filter is active, the home page shows a small "filter
banner" above the library:

```
🏷️ python · Showing 12 videos · ✕ Clear filter
```

Clicking the ✕ goes back to `/`.

## Components

### `app/repos/tags.py` (new)

```python
async def upsert_tag(db, name: str) -> int                 # → tag_id
async def set_tags_for_video(db, video_id, tag_names) -> None
async def tags_for_videos(db, video_ids) -> dict[str, list[str]]
async def video_ids_with_tag(db, name: str) -> set[str]
```

Same shape as the existing `playlists` repo for the linking parts.

### `app/services/youtube.py` (modify)

`VideoMetadata.tags: list[str]` — add to the dataclass. `fetch_metadata`
populates it from `info.get("tags") or []`.

### `app/repos/videos.py` (modify)

`list_recent` and `search` gain optional `tag: str | None = None`
keyword arg. When set, the SQL adds an `EXISTS`-clause:

```sql
AND EXISTS (
    SELECT 1 FROM video_tags vt
    JOIN tags t ON t.id = vt.tag_id
    WHERE vt.video_id = videos.id AND t.name = ? COLLATE NOCASE
)
```

### `app/pipeline.py` (modify)

After transcript acquisition, before summarization, call
`fetch_metadata` and `set_tags_for_video` if tags aren't set yet.
Cheap and idempotent — set_tags handles "already set" via replace
semantics.

### `app/routes/videos.py` and `app/routes/home.py` (modify)

- `submit_video`: write tags from `meta.tags` after `upsert_metadata`.
- `home`: accept `tag` query param, pass to repo, also fetch
  `tags_for_videos` for the result set so the cards render their pills.
- `video_detail`: fetch the video's tags and pass them to the template.

### Templates

- `video_card.html`: render a `.tag-pills` row above or below the
  status line, mirroring the existing playlist-tags treatment.
- `video_detail.html`: render the tags in the header alongside the
  Watch / Markdown / Re-summarize links.
- `home.html`: render the filter banner when `tag` is set.

## CSS

New small block, reusing `playlist-tag` patterns:

```css
.tag-pills { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 16px 8px; }
.tag-pill {
  display: inline-flex; align-items: center;
  font-size: 12px; font-weight: 500;
  color: var(--steel);
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: var(--rounded-full);
  padding: 3px 10px;
  text-decoration: none;
}
.tag-pill:hover {
  background: rgba(0, 212, 164, 0.08);
  border-color: var(--brand-green);
  color: var(--brand-green-deep);
}
.tag-pill::before { content: "#"; opacity: 0.6; margin-right: 2px; }

.filter-banner {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px;
  background: rgba(0, 212, 164, 0.06);
  border: 1px solid rgba(0, 212, 164, 0.2);
  border-radius: var(--rounded-md);
  margin-bottom: 24px;
  font-size: 14px;
}
.filter-banner .clear { color: var(--steel); margin-left: auto; }
```

## Tests

- `tests/test_repos_tags.py` — upsert, set, batched lookup, video filter
- `tests/test_services_youtube.py` — fetch_metadata exposes `tags`
- `tests/test_repos_videos.py` — `list_recent(tag=...)` and `search(tag=...)`
- `tests/test_routes_home.py` — tag filter, banner rendering
- `tests/test_routes_videos.py` — submit_video persists tags

Total ~12 new tests on top of the current 154.

## Out of Scope (future)

- LLM-generated tags
- Editing tags in the UI
- Tag rename / merge
- Tag-only browse view (`/tags`)
- Multi-tag filter (`?tag=a&tag=b`)
- Sorting tags by frequency on cards (truncate at N if too many)
