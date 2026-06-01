# Latest-Video Thumbnail in Playlist Overviews

**Date:** 2026-06-01
**Status:** Approved (design)

## Problem

Playlist overviews currently render the playlist's own image
(`/thumbnails/playlist_<id>.jpg`). That image rarely changes, so the
overview looks static and gives the impression "nothing new here."

We want each playlist's overview tile to show the thumbnail of its
**newest video** instead, so the overview visibly reflects fresh activity.

## Scope

Two pages, both of which display a per-playlist image:

1. **Playlists page** — `/playlists`, template `app/templates/playlists.html`
   (the `playlist-row` list, 120×70 tile).
2. **Home page** — `/`, template `app/templates/playlist_card.html`
   (included from `home.html`, the playlist cards).

The `/playlists` page does **not** use `playlist_card.html`; it has its own
inline `playlist-row` markup. Both must be updated.

## Definition of "newest video"

"Newest" = the video with the greatest `playlist_videos.added_at` for that
playlist. yt-dlp returns playlist entries newest-first, and the sync
(`app/services/playlist_sync.py`) links them in that order, so the
most-recently-added link is the most recently published video. There is no
separate publish-date column; `added_at DESC` is the best available proxy
and matches the user's intent.

## Data flow

No new fetching from YouTube. Video thumbnails are already downloaded during
sync and served at `/thumbnails/{video_id}.jpg`. The only missing piece at
render time is *which video is newest per playlist*.

### Repo change — `app/repos/playlists.py`

Add one helper:

```python
async def latest_video_ids(
    db: aiosqlite.Connection, playlist_ids: list[str]
) -> dict[str, str]:
    """Map each playlist id to the id of its most-recently-added video.

    Newest = greatest playlist_videos.added_at (tie-break video_id DESC).
    Playlists with no linked videos are absent from the result dict.
    Single query, no N+1.
    """
```

Implementation: one query over `playlist_videos` filtered to the given ids,
selecting per playlist the `video_id` with the max `added_at`. A correlated
subquery or window function (`ROW_NUMBER() OVER (PARTITION BY playlist_id
ORDER BY added_at DESC, video_id DESC)`) — SQLite ≥ 3.25 supports window
functions; pick whichever is simplest and verified against the schema.

Empty `playlist_ids` → return `{}` without querying.

A standalone helper keyed by playlist id is preferred over extending
`list_with_stats`/`list_for_user`, because the two routes return different
shapes (`list[tuple[Playlist, int]]` vs `list[Playlist]`). One helper feeds
both without changing either return type.

### Route changes

**`app/routes/playlists.py` — `list_playlists()`** (around line 98):
after `rows = await playlists_repo.list_with_stats(...)`, collect
`[p.id for p, _ in rows]`, call `latest_video_ids(...)`, pass the resulting
dict to the template as `latest_video_ids`.

**`app/routes/home.py` — `home()`** (around lines 109–138):
after building `playlists`, collect `[p.id for p in playlists]`, call
`latest_video_ids(...)`, pass the dict to the template as `latest_video_ids`.

## View logic (both templates, identical precedence)

For each playlist, resolve the image in this order:

1. **Newest video thumbnail** — if `latest_video_ids.get(playlist.id)` is set
   → `/thumbnails/{latest_video_id}.jpg`
2. **Playlist image** — else if `playlist.thumbnail_path`
   → `/thumbnails/playlist_{playlist.id}.jpg`
3. **Placeholder** — else the existing `▣` placeholder.

In Jinja, `latest_video_ids` is a dict passed to both templates. In
`playlist_card.html` (included per-iteration from `home.html`), the dict is
available from the parent context.

Layout and CSS are unchanged. The 120×70 / card tiles already use
`object-fit: cover`, so a 16:9 video thumbnail fits at least as well as the
playlist image.

## Testing

- `latest_video_ids` returns the video with the greatest `added_at` for a
  playlist; tie-break by `video_id DESC`.
- A playlist with no linked videos is absent from the returned dict.
- After linking a newer video, the returned id changes to the new video.
- Empty `playlist_ids` input returns `{}`.

## Out of scope

- The single-playlist detail page (`/p/<id>`).
- Any change to how thumbnails are fetched or stored.
- Adding a real publish-date column (the `added_at` proxy is sufficient).
