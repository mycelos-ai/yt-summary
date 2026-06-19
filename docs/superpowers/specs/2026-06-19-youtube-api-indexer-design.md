# YouTube Data API Playlist Indexer — Design

**Date:** 2026-06-19
**Status:** Approved (brainstorming complete)

## Problem

yt-dlp's flat playlist extraction silently caps at ~101 of 105 entries for the
user's "AI" playlist, because YouTube's JS challenge ("n-sig") drops entries
the container's yt-dlp can't solve (no JS runtime). Diagnosed 2026-06-19. This
is a moving target (UI / bot-detection / continuation changes); the robust fix
is to index playlists through the official, paginated **YouTube Data API**
(`playlistItems.list`) and keep yt-dlp only for per-video transcript work
(unchanged).

## Solution

A new `playlist_index` service lists a playlist completely via the Data API
(httpx, paginated by `nextPageToken`, 50/page). `playlist_sync` reads a
per-user `youtube_api_key`; when set, it indexes via the API; when absent OR
on any API error, it falls back to the existing yt-dlp `fetch_playlist`. Both
paths return the same `PlaylistMetadata` / `PlaylistEntry` shape (including the
`position` field from the playlist-order feature), so `_process_entries` is
unchanged.

The API key is an upgrade: with it, all entries are indexed in correct order;
without it, behaviour is exactly as today.

## Goals / Non-Goals

**Goals**
- Index playlists fully and in order via the Data API when a key is set.
- Zero regression without a key: fall back to today's yt-dlp path.
- Never make a sync worse than today: any API failure falls back to yt-dlp.
- Reuse the existing `PlaylistMetadata`/`PlaylistEntry` contract so the sync
  pipeline (`_process_entries`, positions, dedup) is untouched.
- No new heavy dependency: use `httpx` (already a dep), not `googleapiclient`.

**Non-Goals**
- NOT replacing yt-dlp for per-video transcript/subtitle/detail fetching.
- NOT OAuth / private playlists / "Watch later" — public playlists via an API
  key only.
- NOT fetching per-video duration (playlistItems lacks it; a second
  `videos.list` call is out of scope — duration stays `None` on the API path,
  which is non-critical for the card).

## Components

### 1. `app/services/playlist_index.py` (new)
- `async def fetch_via_api(url: str, *, api_key: str) -> PlaylistMetadata`
- **Playlist id:** parse the `list=` query param from `url` (helper
  `_playlist_id_from_url`). Raise a clear error if absent.
- **Pagination:** GET `https://www.googleapis.com/youtube/v3/playlistItems`
  with `part=snippet,contentDetails`, `playlistId`, `maxResults=50`, `key`,
  and `pageToken` (omit on first call). Loop while a `nextPageToken` is
  present. Safety cap: stop after 40 pages (2000 items) to prevent an
  infinite loop.
- **Item → PlaylistEntry:** `id` = `contentDetails.videoId`;
  `title` = `snippet.title`; `description` = `snippet.description` or "";
  `thumbnail_url` = highest-resolution entry in `snippet.thumbnails`
  (mirror the `_pick_thumbnail` resolution logic);
  `duration_seconds` = `None`; `position` = `snippet.position + 1`
  (the API is 0-based; +1 makes it 1-based, consistent with the
  playlist-order feature). Skip items whose `contentDetails.videoId` is
  missing (deleted/private placeholders) — remaining positions stay monotone.
- **Playlist title/description:** one `playlists.list`
  (`part=snippet`, `id`, `key`) call for `PlaylistMetadata.title` /
  `description` / `thumbnail_url`. If that call fails, fall back to empty
  strings (the entries are what matter); do NOT fail the whole index over a
  missing playlist title.
- **Errors:** raise a single typed exception (e.g. `PlaylistApiError`) on HTTP
  error / quota / 404 / network / parse failure, so the caller can catch it
  and fall back. Never return a partial entry list — a mid-pagination failure
  discards the whole result and raises.

### 2. Settings: `youtube_api_key`
- `app/templates/settings.html`: a key field mirroring `pexels_api_key`
  (plain `<input name="youtube_api_key" value="{{ settings.get('youtube_api_key','') }}">`
  with a short help line: get a key at console.cloud.google.com, enable
  "YouTube Data API v3").
- `app/routes/settings.py`: add `youtube_api_key: str = Form("")` to
  `save_settings` and an entry `("youtube_api_key", youtube_api_key.strip())`
  in the save tuple loop (which already does set-if-value / delete-if-empty).
- Read via `settings_repo.get_for_user(db, user_id, "youtube_api_key")`.

### 3. Sync branch (`app/services/playlist_sync.py`)
- A small helper `_index_playlist(db, config, playlist) -> PlaylistMetadata`:
  ```
  api_key = await settings_repo.get_for_user(db, playlist.user_id, "youtube_api_key")
  if api_key:
      try:
          return await playlist_index.fetch_via_api(playlist.url, api_key=api_key)
      except PlaylistApiError as e:
          log.warning("YouTube API index failed for %s, falling back to yt-dlp: %s", playlist.id, e)
  cookies = await _resolve_cookies(config)
  return await fetch_playlist(playlist.url, cookies_path=cookies)
  ```
- Both `sync_playlist` and `load_older_videos` call `_index_playlist(...)`
  instead of `fetch_playlist(...)` directly. The rest of those functions
  (slicing, `_process_entries`, `set_last_refreshed`) is unchanged.

## Data Flow

```
sync_playlist / load_older_videos
  → _index_playlist(db, config, playlist)
       key set?  → playlist_index.fetch_via_api(url, api_key)   [all entries, ordered]
                     on PlaylistApiError → fall through
       else / on error → fetch_playlist(url, cookies)           [yt-dlp, ~today]
  → meta.entries (same PlaylistEntry shape, incl. position)
  → _process_entries(...)   # unchanged: dedup, upsert position, enqueue
```

## Error Handling

- The API is an upgrade — it must never make a sync worse than today.
- No key → yt-dlp path (no error).
- API error (invalid/expired key 403, `quotaExceeded` 403, network timeout,
  private/deleted playlist 404, malformed JSON) → log the concrete reason,
  fall back to yt-dlp. Worst case equals today (~101), never worse.
- Pagination: loop ends cleanly on missing `nextPageToken`; 40-page safety
  cap. A later-page failure discards the whole API result and raises (caught →
  fallback), so `_process_entries` never sees a partial/inconsistent list that
  would corrupt positions.
- Item without `videoId` → skipped; remaining entries keep monotone positions.

## Testing

Follow existing conventions (service test with mocked httpx, sync test).
- **`playlist_index`** (mock `httpx`):
  - paginates across `nextPageToken` (2 pages → all entries, contiguous
    positions);
  - maps item → `PlaylistEntry` correctly (videoId/title/thumbnail/position+1,
    duration None);
  - skips items missing `contentDetails.videoId`;
  - raises `PlaylistApiError` on an HTTP error response (caller-catchable);
  - `_playlist_id_from_url` parses `list=` from assorted URL forms and errors
    on a URL with no list param.
- **Sync branch** (mock both index paths, no real network):
  - key set → `fetch_via_api` used;
  - no key → `fetch_playlist` (yt-dlp) used;
  - `fetch_via_api` raises `PlaylistApiError` → falls back to `fetch_playlist`.
- **Settings:** saving `youtube_api_key` round-trips; empty clears it.
- No real network/API calls in tests (mock httpx).

## Open Risks / Notes

- Data API daily quota: `playlistItems.list` costs 1 unit/request; a 105-item
  playlist = 3 requests. The default 10k/day quota is ample for this use.
- `duration_seconds` is `None` on the API path. The card doesn't currently
  show duration, so this is invisible today; if duration is wanted later, a
  `videos.list` batch call is a separate enhancement.
- The yt-dlp fallback path still caps at ~101 — that limitation is unchanged
  for keyless installs and is the explicit reason to configure a key.
